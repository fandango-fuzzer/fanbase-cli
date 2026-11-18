"""`fanbase fuzz`: a project's own parsers, fuzzed with a spec's inputs, with what is found kept private."""

import io
import json
import shutil
import subprocess
import sys
import tarfile

import pytest
import yaml
from conftest import build_registry
from helpers import ACCEPT, CRASH_SIGNAL, REAL_RUN, STRICT, FakeAge, FakeFandango, settle, target

from fanbase import contrib, fuzz, incidents
from fanbase.cli import main
from fanbase.fuzz import FUZZ_BUNDLE

posix = pytest.mark.skipif(sys.platform == "win32", reason="signals are POSIX")

SECRET = b"SECRET-INPUT-MARKER"
CRASH = f"import os, signal\nos.kill(os.getpid(), signal.{CRASH_SIGNAL})\n"


@pytest.fixture(autouse=True)
def real_subprocess(monkeypatch):
    monkeypatch.setattr(subprocess, "run", REAL_RUN)


@pytest.fixture
def fandango(monkeypatch):
    # every fourth input starts with "bad", which STRICT rejects; each carries a marker that must never be printed
    made = FakeFandango(content=lambda i: (b"bad " if i % 4 == 0 else b"good ") + SECRET + b" %d\nsecond line\n" % i)
    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(fuzz, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", made)
    return made


@pytest.fixture
def age(monkeypatch):
    fake = FakeAge()
    real_which = shutil.which
    monkeypatch.setattr(incidents, "_run", fake)
    monkeypatch.setattr(incidents.shutil, "which", lambda name, *a, **k: "/fake/age" if name == "age" else real_which(name, *a, **k))
    return fake


@pytest.fixture
def recipients(tmp_path):
    path = tmp_path / "recipients.txt"
    path.write_text("age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq\n")
    return path


def make_registry(tmp_path, **meta):
    root = build_registry(tmp_path / "reg", {("png", "png"): dict(version="1.0", description="PNG", **meta)})
    target(root, "strict", STRICT)
    target(root, "lenient", ACCEPT)
    target(root, "crasher", CRASH)
    settle(root)
    return root


@pytest.fixture
def reg(tmp_path):
    return make_registry(tmp_path)


def fuzz_it(capsys, reg, *args):
    code = main(["--registry", str(reg), "fuzz", "png", "-n", "8", "--budget", "0", "--reduce-budget", "20", *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def tar_of(plaintext):
    return tarfile.open(fileobj=io.BytesIO(plaintext))


def opened(record_dir):
    folder, = record_dir.glob("fuzz-*")
    return folder, json.loads((folder / "manifest.json").read_text())


# --- what is found

def test_where_two_parsers_disagree_is_found_and_reduced(capsys, reg, fandango, tmp_path):
    code, out, err = fuzz_it(capsys, reg, "--targets", "strict,lenient", "--record", str(tmp_path / "private"))
    assert code == 0
    folder, manifest = opened(tmp_path / "private")
    finding, = manifest["findings"]
    assert finding["kind"] == "disagreement" and finding["inputs"] == 2  # inputs 0 and 4 of 8
    assert "strict=invalid (invalid header in <file>)" in finding["signature"] and "lenient=accepted" in finding["signature"]
    base = folder / "findings" / finding["id"]
    assert (base / "reduced.bin").read_bytes() == b"bad"  # all that STRICT looks at
    assert (base / "original.bin").read_bytes().startswith(b"bad " + SECRET)
    report = (base / "REPORT.md").read_text()
    assert "| strict | invalid: invalid header in <file> |" in report and "| lenient | accepted |" in report
    assert "fanbase fuzz png --targets strict,lenient -n 8 --seed 1" in report


def test_what_is_printed_is_counts_only(capsys, reg, fandango, tmp_path):
    _, out, err = fuzz_it(capsys, reg, "--targets", "strict,lenient", "--record", str(tmp_path / "private"),
                          "--json-file", str(tmp_path / "f.json"))
    public = out + err + (tmp_path / "f.json").read_text()
    assert SECRET.decode() not in public and "invalid header" not in public
    assert "strict   accepted 6  rejected 2  error 0" in out
    assert "1 kind(s) of finding (1 of disagreement, 0 of rejected valid input)" in out
    counts = json.loads((tmp_path / "f.json").read_text())
    assert counts["findings"] == {"kinds": 1, "disagreement": 1, "rejected": 0}


def test_one_parser_finds_nothing_unless_the_spec_says_its_files_always_decode(capsys, tmp_path, fandango):
    reg = make_registry(tmp_path)
    fuzz_it(capsys, reg, "--targets", "strict", "--record", str(tmp_path / "a"))
    assert not list((tmp_path / "a").glob("fuzz-*"))  # nothing found, nothing written in the clear

    strict_spec = make_registry(tmp_path / "always", decodes="always")
    _, out, _ = fuzz_it(capsys, strict_spec, "--targets", "strict", "--record", str(tmp_path / "b"))
    _, manifest = opened(tmp_path / "b")
    assert [f["kind"] for f in manifest["findings"]] == ["rejected"]
    assert "0 of disagreement, 1 of rejected valid input" in out


@posix
def test_a_parser_that_breaks_is_an_incident_not_a_finding(capsys, reg, fandango, tmp_path):
    _, out, err = fuzz_it(capsys, reg, "--targets", "lenient,crasher", "--record", str(tmp_path / "private"))
    _, manifest = opened(tmp_path / "private")
    assert manifest["findings"] == [] and manifest["incidents"][0]["target"] == "crasher"
    assert "crasher  accepted 0  rejected 0  error 8" in out  # counted as an error, as a failing harness is
    for secret in (CRASH_SIGNAL, "killed by", "crashed"):
        assert secret not in out + err, secret


def test_fail_says_that_something_was_found(capsys, reg, fandango, tmp_path):
    assert fuzz_it(capsys, reg, "--targets", "strict,lenient", "--record", str(tmp_path / "a"), "--fail")[0] == 1
    assert fuzz_it(capsys, reg, "--targets", "lenient", "--record", str(tmp_path / "b"), "--fail")[0] == 0


# --- for CI: the record is encrypted, and nothing about it is said

def test_with_a_key_the_record_is_encrypted_and_holds_the_findings(capsys, reg, fandango, age, recipients, tmp_path):
    code, out, err = fuzz_it(capsys, reg, "--targets", "strict,lenient", "--record", str(tmp_path / "private"),
                             "--recipients", str(recipients))
    assert code == 0
    assert sorted(p.name for p in (tmp_path / "private").iterdir()) == [FUZZ_BUNDLE]
    assert "reduced from" not in err and SECRET.decode() not in out + err
    names = tar_of(age.plaintexts[-1]).getnames()
    assert "manifest.json" in names and "REPORT.md" in names
    assert any(n.startswith("findings/") and n.endswith("/reduced.bin") for n in names)
    index = tar_of(age.plaintexts[-1]).extractfile("REPORT.md").read().decode()
    assert "1 kind(s) of finding: 1 of disagreement" in index


def test_incidents_open_reads_it(capsys, reg, fandango, age, recipients, tmp_path, monkeypatch):
    from fanbase import incident_cmds

    fuzz_it(capsys, reg, "--targets", "strict,lenient", "--record", str(tmp_path / "private"), "--recipients", str(recipients))
    monkeypatch.setattr(incident_cmds, "_run", age)
    monkeypatch.setattr(incident_cmds.shutil, "which", lambda name, *a, **k: "/fake/age")
    key = tmp_path / "key.txt"
    key.write_text("AGE-SECRET-KEY-1QQQQ\n")
    assert main(["incidents", "open", str(tmp_path / "private" / FUZZ_BUNDLE), "--identity", str(key),
                 "--into", str(tmp_path / "opened")]) == 0
    assert list((tmp_path / "opened" / "findings").glob("*/reduced.bin"))


# --- the parsers

def test_a_project_brings_its_own_parser(capsys, reg, fandango, tmp_path):
    own = tmp_path / "project" / ".fandango" / "targets"
    (own / "mine").mkdir(parents=True)
    (own / "mine" / "harness.py").write_text(ACCEPT)
    (own / "mine" / "target.yml").write_text(yaml.safe_dump(
        {"title": "Mine", "formats": ["png"], "run": ["{python}", "{dir}/harness.py", "{file}"]}))
    code, out, _ = fuzz_it(capsys, reg, "--targets", "strict,mine", "--target-dir", str(own), "--record", str(tmp_path / "p"))
    assert code == 0 and "mine    accepted 8" in out


def own_target(folder, name, harness, formats=("png",)):
    (folder / name).mkdir(parents=True)
    (folder / name / "harness.py").write_text(harness)
    (folder / name / "target.yml").write_text(yaml.safe_dump(
        {"title": name.title(), "formats": list(formats), "run": ["{python}", "{dir}/harness.py", "{file}"]}))


def test_without_targets_it_fuzzes_the_projects_own_programs_of_that_format(capsys, reg, fandango, tmp_path):
    own = tmp_path / "project" / ".fandango" / "targets"
    own_target(own, "mine", STRICT)
    own_target(own, "other", ACCEPT, formats=("gif",))  # reads another format: left out
    code, out, _ = fuzz_it(capsys, reg, "--target-dir", str(own), "--record", str(tmp_path / "p"))
    assert code == 0 and "mine  accepted 6  rejected 2" in out and "other" not in out and "strict" not in out


def test_and_says_so_when_none_of_them_reads_it(capsys, reg, fandango, tmp_path):
    own = tmp_path / "own"
    own_target(own, "gifs", ACCEPT, formats=("gif",))
    code, _, err = fuzz_it(capsys, reg, "--target-dir", str(own), "--record", str(tmp_path / "p"))
    assert code == 2 and "none of the targets in" in err and "reads png" in err


def test_its_own_parser_comes_before_the_registrys_of_that_name(capsys, reg, fandango, tmp_path):
    own = tmp_path / "own"
    (own / "strict").mkdir(parents=True)
    (own / "strict" / "harness.py").write_text(ACCEPT)
    (own / "strict" / "target.yml").write_text(yaml.safe_dump(
        {"title": "Mine", "formats": ["png"], "run": ["{python}", "{dir}/harness.py", "{file}"]}))
    _, out, _ = fuzz_it(capsys, reg, "--targets", "strict", "--target-dir", str(own), "--record", str(tmp_path / "p"))
    assert "strict  accepted 8  rejected 0" in out


# --- only the project's own program

def test_only_own_targets_never_takes_the_registrys(capsys, reg, fandango, tmp_path):
    own = tmp_path / "own"
    own_target(own, "mine", ACCEPT)
    code, _, err = fuzz_it(capsys, reg, "--targets", "mine,strict", "--target-dir", str(own), "--only-own-targets",
                           "--record", str(tmp_path / "p"))
    assert code == 2 and "no target called strict" in err and fandango.calls == []


def test_only_own_targets_needs_a_harness_of_their_own(capsys, reg, fandango, tmp_path):
    own = tmp_path / "own"
    (own / "system").mkdir(parents=True)
    (own / "system" / "target.yml").write_text(yaml.safe_dump(
        {"title": "A program of the machine", "formats": ["png"], "run": ["python3", "-c", "pass", "{file}"]}))
    code, _, err = fuzz_it(capsys, reg, "--target-dir", str(own), "--only-own-targets", "--record", str(tmp_path / "p"))
    assert code == 2 and "not a harness of its own folder" in err


def test_only_own_targets_does_not_follow_a_link_out_of_the_folder(capsys, reg, fandango, tmp_path):
    own = tmp_path / "own"
    own.mkdir()
    (own / "strict").symlink_to(reg / "targets" / "strict")  # the registry's, dressed as one's own
    code, _, err = fuzz_it(capsys, reg, "--target-dir", str(own), "--only-own-targets", "--record", str(tmp_path / "p"))
    assert code == 2 and "is not in" in err


def test_only_own_targets_needs_the_folder(capsys, reg, fandango, tmp_path):
    code, _, err = fuzz_it(capsys, reg, "--only-own-targets", "--record", str(tmp_path / "p"))
    assert code == 2 and "name that folder" in err


def test_and_with_all_that_it_fuzzes_ones_own(capsys, reg, fandango, tmp_path):
    own = tmp_path / "own"
    own_target(own, "mine", STRICT)
    code, out, _ = fuzz_it(capsys, reg, "--target-dir", str(own), "--only-own-targets", "--record", str(tmp_path / "p"))
    assert code == 0 and "mine  accepted 6  rejected 2" in out


def test_a_parser_that_cannot_run_stops_it_before_anything_is_made(capsys, reg, fandango, tmp_path):
    (reg / "targets" / "lenient" / "target.yml").write_text(yaml.safe_dump(
        {"title": "L", "formats": ["png"], "run": ["{python}", "{dir}/harness.py", "{file}"], "needs": {"commands": ["no-such-parser-here"]}}))
    code, _, err = fuzz_it(capsys, reg, "--targets", "lenient", "--record", str(tmp_path / "p"))
    assert code == 2 and "cannot run here" in err and fandango.calls == []


def test_a_parser_of_another_format_is_refused(capsys, reg, fandango, tmp_path):
    target(reg, "gifonly", ACCEPT, formats=("gif",))
    code, _, err = fuzz_it(capsys, reg, "--targets", "gifonly", "--record", str(tmp_path / "p"))
    assert code == 2 and "does not judge png" in err


# --- the pieces

def test_reduce_keeps_what_keeps_the_answers():
    assert fuzz.reduce(b"xx\nkeep me\nyy\n", lambda d: b"keep" in d, 10) == b"keep"


def test_gist_leaves_out_where_and_what():
    assert fuzz.gist("Line 3:4 extraneous input ' ' expecting X") == "extraneous input"
    assert fuzz.gist("UnicodeDecodeError: 'ascii' codec can't decode") == "UnicodeDecodeError:"
    # what comes before the position is the same for every rejection, so what comes after tells them apart
    assert fuzz.gist("Exception: The UVL has the following error that prevents reading it :Line 4:26 - "
                     "extraneous input '[1]' expecting {'}', ','}") == "extraneous input"
    assert fuzz.gist("line 1, col 3: no viable alternative at input '\\t'") == "no viable alternative at input"


def test_reducing_asks_the_fastest_parser_first_and_stops_at_the_first_new_answer(capsys, reg, fandango, tmp_path, monkeypatch):
    asked = []
    real_ask = fuzz.ask

    def counting(target, file, **kw):
        asked.append((target.name, file.name.startswith("probe")))
        return real_ask(target, file, **kw)

    monkeypatch.setattr(fuzz, "ask", counting)
    target(reg, "slow", "import time\ntime.sleep(0.05)\n")  # accepts everything, slowly
    fuzz_it(capsys, reg, "--targets", "slow,strict", "--record", str(tmp_path / "p"))
    probes = [name for name, probe in asked if probe]
    assert probes and probes[0] == "strict"  # the fast one first
    # a smaller input that strict accepts (it no longer starts with "bad") is not a finding: slow is not asked about it
    assert probes.count("slow") < probes.count("strict")
