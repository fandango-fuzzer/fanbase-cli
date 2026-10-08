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


# --- the file of every incident, whichever record it was in

def another_record(capsys, reg, tmp_path, name="again"):
    """The same bug, found again next week: a new record in a new folder."""
    code, _, _ = run(capsys, "--registry", str(reg), "evaluate", "png", "-n", "4", "--incidents", str(tmp_path / name))
    assert code == 0
    folder, = (tmp_path / name).glob("incidents-*")
    return folder


def global_file():
    return incident_track.global_path()


@posix
def test_the_global_file_is_the_users_own_and_never_the_real_one(capsys, record):
    import os

    assert "FANBASE_INCIDENTS" in os.environ and "incidents.yml" in str(global_file())
    assert not str(global_file()).startswith(str(incident_track.Path.home() / ".local"))  # (the tests do not touch yours)


@posix
def test_track_notes_it_in_the_record_and_in_the_users_file(capsys, record):
    run(capsys, "incidents", "track", str(record), the_id(record), "--reported", "2026-09-28", "--vendor", "ImageMagick")
    here = yaml.safe_load((record / "tracking.yml").read_text())["incidents"][the_id(record)]
    everywhere = yaml.safe_load(global_file().read_text())["incidents"][the_id(record)]
    assert here == {"reported": "2026-09-28", "vendor": "ImageMagick"}
    assert everywhere == {**here, "spec": "png/png", "target": "crasher", "kind": "crash"}  # (with what it is: it can be read without the record)
    assert global_file().stat().st_mode & 0o777 == 0o600


@posix
def test_the_same_bug_found_again_is_already_reported(capsys, reg, record, tmp_path):
    run(capsys, "incidents", "track", str(record), the_id(record), "--reported", "2026-09-28", "--vendor", "ImageMagick", "--reference", "issue 4711")
    later = another_record(capsys, reg, tmp_path)
    assert the_id(later) == the_id(record)  # (same spec, same target, same cause: the same incident)
    _, out, _ = run(capsys, "incidents", "list", str(later))
    assert "reported 2026-09-28; may be public from 2026-12-27 (80 days) (tracked in an earlier record)" in out
    _, shown, _ = run(capsys, "incidents", "show", str(later), the_id(later))
    assert "vendor: ImageMagick" in shown and "reference: issue 4711" in shown and "spec:" not in shown.split("## Tracked")[1]


@posix
def test_what_is_added_in_a_later_record_is_added_to_what_was_known(capsys, reg, record, tmp_path):
    run(capsys, "incidents", "track", str(record), the_id(record), "--reported", "2026-08-01", "--vendor", "ImageMagick")
    later = another_record(capsys, reg, tmp_path)
    code, out, _ = run(capsys, "incidents", "track", str(later), the_id(later), "--fixed-in", "7.1.2", "--fixed-on", "2026-09-20")
    assert code == 0 and "reported 2026-08-01, fixed 2026-09-20; may be public from 2026-10-20 (12 days)" in out  # (it was reported in the other record)
    assert yaml.safe_load(global_file().read_text())["incidents"][the_id(record)]["fixed_in"] == "7.1.2"
    assert "vendor" in yaml.safe_load((later / "tracking.yml").read_text())["incidents"][the_id(later)]  # (this record is complete by itself)


@posix
def test_a_fix_cannot_come_before_a_report_made_in_another_record(capsys, reg, record, tmp_path):
    run(capsys, "incidents", "track", str(record), the_id(record), "--reported", "2026-09-20")
    later = another_record(capsys, reg, tmp_path)
    code, _, err = run(capsys, "incidents", "track", str(later), the_id(later), "--fixed-in", "1", "--fixed-on", "2026-09-01")
    assert code == 2 and "before it was reported" in err


@posix
def test_this_record_has_the_last_word_over_the_file(capsys, record):
    run(capsys, "incidents", "track", str(record), the_id(record), "--reported", "2026-09-01")
    folder_file = record / "tracking.yml"
    data = yaml.safe_load(folder_file.read_text())
    data["incidents"][the_id(record)]["reported"] = "2026-09-10"  # (changed by hand here)
    folder_file.write_text(yaml.safe_dump(data))
    _, out, _ = run(capsys, "incidents", "list", str(record))
    assert "reported 2026-09-10" in out


@posix
def test_not_noted_in_the_users_file_if_not_wanted(capsys, record):
    run(capsys, "incidents", "track", str(record), the_id(record), "--reported", "2026-09-28", "--no-global")
    assert (record / "tracking.yml").is_file() and not global_file().exists()


@posix
def test_forgetting_takes_it_out_of_both(capsys, record):
    run(capsys, "incidents", "track", str(record), the_id(record), "--reported", "2026-09-28")
    code, out, _ = run(capsys, "incidents", "track", str(record), the_id(record), "--forget")
    assert code == 0 and "forgotten" in out
    assert the_id(record) not in yaml.safe_load((record / "tracking.yml").read_text())["incidents"]
    assert the_id(record) not in yaml.safe_load(global_file().read_text())["incidents"]
    assert "not reported yet" in run(capsys, "incidents", "list", str(record))[1]


@posix
def test_tracked_lists_everything_the_soonest_first(capsys, reg, record, tmp_path):
    run(capsys, "incidents", "track", str(record), the_id(record), "--reported", "2026-09-28", "--vendor", "ImageMagick")
    # a second incident, from another target, in another record
    target(reg, "second", CRASH.replace("the parser said this", "something else entirely"))
    (reg / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["second"]}))
    settle(reg)
    other = another_record(capsys, reg, tmp_path, "other")
    run(capsys, "incidents", "track", str(other), the_id(other), "--reported", "2026-07-15", "--vendor", "Libfoo")
    _, out, _ = run(capsys, "incidents", "tracked")
    lines = out.strip().splitlines()
    assert len(lines) == 2 and "crash of second on png/png to Libfoo: reported 2026-07-15; may be public from 2026-10-13 (5 days)" in lines[0]
    assert "crash of crasher on png/png to ImageMagick: reported 2026-09-28" in lines[1]
    _, soon, _ = run(capsys, "incidents", "tracked", "--within", "10")
    assert len(soon.strip().splitlines()) == 1 and "Libfoo" in soon


@posix
def test_nothing_tracked_and_nothing_soon(capsys, record):
    code, out, _ = run(capsys, "incidents", "tracked")
    assert code == 0 and "nothing tracked yet" in out
    run(capsys, "incidents", "track", str(record), the_id(record), "--reported", "2026-09-28")
    assert "nothing is to be public within 5 days" in run(capsys, "incidents", "tracked", "--within", "5")[1]


@posix
def test_a_users_file_that_is_not_one_is_refused_and_not_overwritten(capsys, record):
    global_file().parent.mkdir(parents=True, exist_ok=True)
    global_file().write_text("- not\n- a mapping\n")
    code, _, err = run(capsys, "incidents", "track", str(record), the_id(record), "--reported", "2026-09-28")
    assert code == 2 and "is not a tracking file" in err and global_file().read_text() == "- not\n- a mapping\n"


def test_where_the_file_is(monkeypatch, tmp_path):
    monkeypatch.delenv("FANBASE_INCIDENTS")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert incident_track.global_path() == tmp_path / "data" / "fanbase" / "incidents.yml"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert incident_track.global_path() == incident_track.Path.home() / ".local" / "share" / "fanbase" / "incidents.yml"
    monkeypatch.setenv("FANBASE_INCIDENTS", str(tmp_path / "mine.yml"))
    assert incident_track.global_path() == tmp_path / "mine.yml"


@posix
def test_the_folder_of_the_file_is_private_and_what_is_written_is_cleaned(capsys, record, tmp_path, monkeypatch):
    monkeypatch.setenv("FANBASE_INCIDENTS", str(tmp_path / "data" / "fanbase" / "incidents.yml"))
    run(capsys, "incidents", "track", str(record), the_id(record), "--vendor", "Evil\x1b[31m Corp\x07")
    assert (tmp_path / "data" / "fanbase").stat().st_mode & 0o777 == 0o700
    saved = yaml.safe_load((tmp_path / "data" / "fanbase" / "incidents.yml").read_text())["incidents"][the_id(record)]
    assert "\x1b" not in saved["vendor"] and "\x07" not in saved["vendor"]
