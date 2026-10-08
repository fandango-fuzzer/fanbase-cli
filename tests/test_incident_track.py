"""`fanbase incidents list|show|track|verify`: what to do with a private record once it is open."""

import datetime
import json
import subprocess
import sys

import pytest
import yaml
from conftest import build_registry
from helpers import CRASH_SIGNAL, REAL_RUN, STRICT, FakeFandango, settle, target

from fanbase import contrib, incident_track
from fanbase.cli import main

posix = pytest.mark.skipif(sys.platform == "win32", reason="signals are POSIX")
CRASH = f"import os, signal, sys\nprint('the parser said this', file=sys.stderr, flush=True)\nos.kill(os.getpid(), signal.{CRASH_SIGNAL})\n"
FINE = "import sys\nsys.exit(0)\n"
TODAY = datetime.date(2026, 10, 8)


@pytest.fixture(autouse=True)
def world(monkeypatch):
    monkeypatch.setattr(subprocess, "run", REAL_RUN)
    monkeypatch.setattr(incident_track, "today", lambda: TODAY)


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def reg(tmp_path):
    root = build_registry(tmp_path / "reg", {("png", "png"): dict(version="1.0", description="PNG")})
    target(root, "strict", STRICT)
    target(root, "crasher", CRASH, version=["{python}", "-c", "print('crasher 1.0')"])
    (root / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["strict", "crasher"]}))
    settle(root)
    return root


@pytest.fixture
def record(tmp_path, reg, monkeypatch, capsys):
    """A record with one incident, as `evaluate --incidents` writes it without a key."""
    made = FakeFandango(content=lambda i: b"input %d" % i)
    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr("fanbase.evaluate.find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", made)
    code, _, _ = run(capsys, "--registry", str(reg), "evaluate", "png", "-n", "4", "--incidents", str(tmp_path / "private"))
    assert code == 0
    folder, = (tmp_path / "private").glob("incidents-*")
    return folder


def the_id(record):
    return json.loads((record / "manifest.json").read_text())["incidents"][0]["id"]


# --- list and show

@posix
def test_list_says_what_broke_and_where_it_stands(capsys, record):
    code, out, _ = run(capsys, "incidents", "list", str(record))
    assert code == 0 and the_id(record) in out and "crash of crasher crasher 1.0 on png/png (4x, 4 again): not reported yet" in out


def test_a_record_where_nothing_broke(capsys, tmp_path):
    folder = tmp_path / "quiet"
    folder.mkdir()
    (folder / "manifest.json").write_text(json.dumps({"incidents": []}))
    assert "nothing broke" in run(capsys, "incidents", "list", str(folder))[1]


def test_what_is_not_a_record(capsys, tmp_path):
    (tmp_path / "x").mkdir()
    code, _, err = run(capsys, "incidents", "list", str(tmp_path / "x"))
    assert code == 2 and "is not an opened record" in err
    (tmp_path / "x/manifest.json").write_text("[1]")
    assert run(capsys, "incidents", "list", str(tmp_path / "x"))[0] == 2
    (tmp_path / "x/manifest.json").write_text(json.dumps({"incidents": [{"id": "../../x"}]}))
    assert "no usable id" in run(capsys, "incidents", "list", str(tmp_path / "x"))[2]


@posix
def test_show_prints_the_note_to_the_vendor(capsys, record):
    code, out, _ = run(capsys, "incidents", "show", str(record), the_id(record)[:12])  # (the start of the id will do)
    assert code == 0 and "Before you report it" in out and f"killed by {CRASH_SIGNAL}" in out and "the parser said this" in out


@posix
def test_an_id_that_is_not_there_or_not_one_thing(capsys, record):
    assert "no incident called" in run(capsys, "incidents", "show", str(record), "nothing")[2]
    manifest = json.loads((record / "manifest.json").read_text())
    manifest["incidents"].append({**manifest["incidents"][0], "id": the_id(record) + "-two"})
    (record / "manifest.json").write_text(json.dumps(manifest))
    code, _, err = run(capsys, "incidents", "show", str(record), the_id(record)[:10])
    assert code == 2 and "could be any of" in err
    assert run(capsys, "incidents", "show", str(record), the_id(record))[0] == 0  # (the exact one is not ambiguous)


# --- track, and the clock

@posix
def test_reporting_starts_the_clock(capsys, record):
    code, out, _ = run(capsys, "incidents", "track", str(record), the_id(record), "--reported", "2026-09-28", "--vendor", "ImageMagick", "--reference", "ticket 4711")
    assert code == 0 and "reported 2026-09-28; may be public from 2026-12-27 (80 days)" in out
    _, listing, _ = run(capsys, "incidents", "list", str(record))
    assert "reported 2026-09-28; may be public from 2026-12-27 (80 days)" in listing
    saved = yaml.safe_load((record / "tracking.yml").read_text())["incidents"][the_id(record)]
    assert saved == {"reported": "2026-09-28", "vendor": "ImageMagick", "reference": "ticket 4711"}
    assert (record / "tracking.yml").stat().st_mode & 0o777 == 0o600


@posix
def test_a_fix_shortens_the_wait(capsys, record):
    run(capsys, "incidents", "track", str(record), the_id(record), "--reported", "2026-08-01")
    code, out, _ = run(capsys, "incidents", "track", str(record), the_id(record), "--fixed-in", "7.1.2", "--fixed-on", "2026-09-20")
    assert "fixed 2026-09-20; may be public from 2026-10-20 (12 days)" in out  # 30 days after the fix, not 90 after the report
    _, show, _ = run(capsys, "incidents", "show", str(record), the_id(record))
    assert "fixed_in: 7.1.2" in show and "reported: '2026-08-01'" in show


@posix
def test_a_fix_that_is_slower_than_ninety_days_does_not_extend_it(capsys, record):
    run(capsys, "incidents", "track", str(record), the_id(record), "--reported", "2026-06-01")
    _, out, _ = run(capsys, "incidents", "track", str(record), the_id(record), "--fixed-in", "7.1.2", "--fixed-on", "2026-09-30")
    assert "may be public from 2026-08-30 (39 days ago)" in out  # (ninety days after the report came first)


@posix
def test_the_day_itself_and_after(capsys, record):
    run(capsys, "incidents", "track", str(record), the_id(record), "--reported", "2026-07-10")
    _, out, _ = run(capsys, "incidents", "list", str(record))
    assert "from 2026-10-08 (that is today)" in out


@posix
def test_today_will_do_as_a_date(capsys, record):
    _, out, _ = run(capsys, "incidents", "track", str(record), the_id(record), "--reported", "today")
    assert "reported 2026-10-08; may be public from 2027-01-06 (90 days)" in out


@posix
@pytest.mark.parametrize("args, complaint", [
    (["--reported", "yesterday"], "is not a date like"),
    (["--reported", "2027-01-01"], "is in the future"),
    (["--fixed-on", "2026-09-01"], "has it been reported"),
    ([], "say what happened"),
])
def test_what_makes_no_sense_is_refused(capsys, record, args, complaint):
    code, _, err = run(capsys, "incidents", "track", str(record), the_id(record), *args)
    assert code == 2 and complaint in err and not (record / "tracking.yml").exists()


@posix
def test_it_cannot_be_fixed_before_it_was_reported(capsys, record):
    run(capsys, "incidents", "track", str(record), the_id(record), "--reported", "2026-09-20")
    code, _, err = run(capsys, "incidents", "track", str(record), the_id(record), "--fixed-in", "1", "--fixed-on", "2026-09-01")
    assert code == 2 and "before it was reported" in err


@posix
def test_what_is_tracked_is_cleaned_and_cut(capsys, record):
    run(capsys, "incidents", "track", str(record), the_id(record), "--vendor", "Evil\x1b[31m Corp\x07", "--note", "x" * 1000)
    saved = yaml.safe_load((record / "tracking.yml").read_text())["incidents"][the_id(record)]
    assert "\x1b" not in saved["vendor"] and "\x07" not in saved["vendor"] and len(saved["note"]) == 300


@posix
def test_a_tracking_file_that_is_not_one_is_refused(capsys, record):
    (record / "tracking.yml").write_text("- not\n- a mapping\n")
    assert run(capsys, "incidents", "list", str(record))[0] == 2


def test_the_clock_by_itself():
    assert incident_track.clock(None) == ("not reported yet", None)
    text, public = incident_track.clock({"reported": datetime.date(2026, 1, 1)}, datetime.date(2026, 1, 31))
    assert public == datetime.date(2026, 4, 1) and "(60 days)" in text


# --- verify

@posix
def test_it_still_breaks_the_parser(capsys, reg, record):
    code, out, _ = run(capsys, "--registry", str(reg), "incidents", "verify", str(record), the_id(record))
    assert code == 1 and "crasher 1.0; now crasher 1.0" in out and f"still crash: killed by {CRASH_SIGNAL}" in out and "it is not fixed in this version" in out


@posix
def test_it_no_longer_does_and_says_how_to_note_the_fix(capsys, reg, record):
    (reg / "targets/crasher/harness.py").write_text(FINE)
    (reg / "targets/crasher/target.yml").write_text((reg / "targets/crasher/target.yml").read_text().replace("crasher 1.0", "crasher 1.1"))
    code, out, _ = run(capsys, "--registry", str(reg), "incidents", "verify", str(record), the_id(record))
    assert code == 0 and "now crasher 1.1" in out and "no longer breaks it (accepted)" in out
    assert "it no longer breaks crasher in crasher 1.1" in out and "incidents track" in out and "--fixed-in VERSION --fixed-on DATE" in out


@posix
def test_it_does_not_note_the_fix_for_you(capsys, reg, record):
    (reg / "targets/crasher/harness.py").write_text(FINE)
    run(capsys, "--registry", str(reg), "incidents", "verify", str(record), the_id(record))
    assert not (record / "tracking.yml").exists()


@posix
def test_a_hang_is_run_again_too(capsys, tmp_path, monkeypatch):
    root = build_registry(tmp_path / "reg2", {("png", "png"): dict(version="1.0")})
    target(root, "sleeper", "import time\ntime.sleep(60)\n")
    (root / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["sleeper"]}))
    settle(root)
    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr("fanbase.evaluate.find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", FakeFandango(content=lambda i: b"input %d" % i))
    run(capsys, "--registry", str(root), "evaluate", "png", "-n", "1", "--timeout", "1", "--incidents", str(tmp_path / "p"))
    folder, = (tmp_path / "p").glob("incidents-*")
    code, out, _ = run(capsys, "--registry", str(root), "incidents", "verify", str(folder), the_id(folder), "--timeout", "1")
    assert code == 1 and "still hang: no answer in 1 seconds" in out


@posix
def test_a_target_the_registry_does_not_have(capsys, tmp_path, record):
    other = build_registry(tmp_path / "other", {("png", "png"): dict(version="1.0")})
    code, _, err = run(capsys, "--registry", str(other), "incidents", "verify", str(record), the_id(record))
    assert code == 2 and "crasher" in err


@posix
def test_an_incident_that_kept_no_input(capsys, reg, record):
    manifest = json.loads((record / "manifest.json").read_text())
    for item in manifest["incidents"][0]["inputs"]:
        item["file"] = None
    (record / "manifest.json").write_text(json.dumps(manifest))
    code, _, err = run(capsys, "--registry", str(reg), "incidents", "verify", str(record), the_id(record))
    assert code == 2 and "kept no input that can be run again" in err


@posix
def test_a_manifest_cannot_point_outside_the_record(capsys, reg, record, tmp_path):
    (tmp_path / "outside.bin").write_bytes(b"x")
    manifest = json.loads((record / "manifest.json").read_text())
    manifest["incidents"][0]["inputs"] = [{"file": "../../outside.bin", "sha256": "0" * 64, "size": 1}]
    (record / "manifest.json").write_text(json.dumps(manifest))
    code, out, _ = run(capsys, "--registry", str(reg), "incidents", "verify", str(record), the_id(record))
    assert "../../outside.bin: not there" in out and "still crash" not in out
