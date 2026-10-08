import io
import json
import shutil
import subprocess
import sys
import tarfile

import pytest
import yaml
from conftest import build_registry
from helpers import REAL_RUN, STRICT, FakeAge, FakeFandango, settle, target

from fanbase import contrib, incidents
from fanbase.cli import main
from fanbase.incidents import BUNDLE_NAME, PAD

posix = pytest.mark.skipif(sys.platform == "win32", reason="signals are POSIX")
needs_age = pytest.mark.skipif(shutil.which("age") is None or shutil.which("age-keygen") is None, reason="age is not installed")

SECRET_STDERR = "SECRET-STDERR-MARKER the parser said this"
SECRET_INPUT = b"SECRET-INPUT-MARKER"
CRASH = f"import os, signal, sys\nprint({SECRET_STDERR!r}, file=sys.stderr, flush=True)\nos.kill(os.getpid(), signal.SIGSEGV)\n"


@pytest.fixture(autouse=True)
def real_subprocess(monkeypatch):
    monkeypatch.setattr(subprocess, "run", REAL_RUN)


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def age(monkeypatch):
    fake = FakeAge()
    real_which = shutil.which
    monkeypatch.setattr(incidents, "_run", fake)
    monkeypatch.setattr(incidents.shutil, "which", lambda name, *a, **k: "/fake/age" if name == "age" else real_which(name, *a, **k))
    return fake


@pytest.fixture
def fandango(monkeypatch):
    made = FakeFandango(content=lambda i: SECRET_INPUT + b" %d" % i)
    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr("fanbase.evaluate.find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", made)
    return made


@pytest.fixture
def reg(tmp_path):
    root = build_registry(tmp_path / "reg", {("png", "png"): dict(version="1.0", description="PNG")})
    target(root, "strict", STRICT)
    (root / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["strict"]}))
    settle(root)
    return root


@pytest.fixture
def crashing(reg):
    target(reg, "crasher", CRASH)
    (reg / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["strict", "crasher"]}))
    settle(reg)
    return reg


@pytest.fixture
def recipients(tmp_path):
    path = tmp_path / "recipients.txt"
    path.write_text("# the public key of whoever reads the record\nage1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq\n")
    return path


def evaluate(capsys, reg, *args):
    return run(capsys, "--registry", str(reg), "evaluate", "png", *args)


def tar_of(plaintext):
    return tarfile.open(fileobj=io.BytesIO(plaintext))


# --- the details: a plain, private record on your own machine

@posix
def test_a_crash_is_written_down_with_everything_needed_to_report_it(capsys, crashing, fandango, tmp_path):
    code, out, _ = evaluate(capsys, crashing, "-n", "4", "--incidents", str(tmp_path / "private"))
    assert code == 0
    folder, = (tmp_path / "private").glob("incidents-*")
    manifest = json.loads((folder / "manifest.json").read_text())
    incident, = manifest["incidents"]
    assert (incident["kind"], incident["target"], incident["signal"], incident["spec"]) == ("crash", "crasher", "SIGSEGV", "png/png")
    assert incident["occurrences"] == 4 and incident["confirmed"] == 4  # it happens again when tried once more
    assert incident["spec_version"] == "1.0" and len(incident["spec_sha256"]) == 64 and incident["command"][-1] == "INPUT"
    assert manifest["seed"] == 1 and manifest["count"] == 4 and manifest["fandango"]
    inputs = sorted((folder / incident["id"]).glob("input-*.bin"))
    assert 1 <= len(inputs) <= 3 and all(p.read_bytes().startswith(SECRET_INPUT) for p in inputs)
    assert SECRET_STDERR in (folder / incident["id"] / "stderr.txt").read_text()
    report = (folder / incident["id"] / "REPORT.md").read_text()
    assert "killed by SIGSEGV" in report and "Before you report it" in report and "ETHICS.md" in report and "90 days" in report
    assert "4 time(s)" in report


@pytest.fixture
def two_specs(tmp_path):
    root = build_registry(tmp_path / "reg2", {("png", "png"): dict(version="1.0"), ("png", "png-apng"): dict(version="1.0")})
    target(root, "crasher", CRASH)
    (root / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["crasher"]}))
    settle(root)
    return root


@posix
def test_the_same_cause_in_two_specs_is_two_entries_that_do_not_collide(capsys, two_specs, fandango, tmp_path):
    code, _, _ = run(capsys, "--registry", str(two_specs), "evaluate", "png", "png-apng", "-n", "4", "--incidents", str(tmp_path / "private"))
    assert code == 0
    folder, = (tmp_path / "private").glob("incidents-*")
    found = json.loads((folder / "manifest.json").read_text())["incidents"]
    assert sorted(i["spec"] for i in found) == ["png/png", "png/png-apng"]
    assert len({i["id"] for i in found}) == 2 and found[0]["signature"] == found[1]["signature"]  # one cause, in two specs
    for i in found:
        assert (folder / i["id"] / "REPORT.md").is_file() and (folder / i["id"] / "stderr.txt").is_file()
        assert i["spec"].replace("/", "_") in i["id"]


@posix
def test_and_the_encrypted_record_names_nothing_twice(capsys, two_specs, fandango, age, recipients, tmp_path):
    run(capsys, "--registry", str(two_specs), "evaluate", "png", "png-apng", "-n", "4", "--incidents", str(tmp_path / "p"),
        "--incident-recipients", str(recipients))
    names = tar_of(age.plaintexts[-1]).getnames()
    assert len(names) == len(set(names)) and sum(n.endswith("/REPORT.md") for n in names) == 2


@posix
def test_the_record_is_for_your_eyes_only(capsys, crashing, fandango, tmp_path):
    evaluate(capsys, crashing, "-n", "4", "--incidents", str(tmp_path / "private"))
    folder, = (tmp_path / "private").glob("incidents-*")
    modes = {p: p.stat().st_mode & 0o777 for p in [tmp_path / "private", folder, *folder.rglob("*")]}
    assert all(mode == (0o700 if p.is_dir() else 0o600) for p, mode in modes.items())


@posix
def test_one_bug_found_forty_times_is_one_entry(capsys, crashing, fandango, tmp_path):
    evaluate(capsys, crashing, "-n", "40", "-j", "8", "--incidents", str(tmp_path / "private"))
    folder, = (tmp_path / "private").glob("incidents-*")
    incident, = json.loads((folder / "manifest.json").read_text())["incidents"]
    assert incident["occurrences"] == 40 and len(incident["inputs"]) == 3  # three examples are kept, not forty


def test_nothing_breaking_leaves_nothing_in_the_clear(capsys, reg, fandango, tmp_path):
    code, out, err = evaluate(capsys, reg, "-n", "4", "--incidents", str(tmp_path / "private"))
    assert code == 0 and list((tmp_path / "private").glob("*")) == []
    assert "incident" not in out + err


@posix
def test_what_a_target_says_about_itself_is_not_a_crash(capsys, reg, fandango, tmp_path):
    target(reg, "buggy", "raise ValueError('the harness is broken')\n")  # a traceback: the harness, not the parser
    target(reg, "bomb", "import sys\nprint('Image size exceeds limit', file=sys.stderr)\nsys.exit(1)\n",
           classify=[{"match": "exceeds limit", "as": "resource-limit"}])  # a limit the library sets itself
    (reg / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["buggy", "bomb"]}))
    settle(reg)
    evaluate(capsys, reg, "-n", "4", "--incidents", str(tmp_path / "private"))
    assert list((tmp_path / "private").glob("*")) == []


# --- hangs and kills

@posix
def test_a_hang_that_happens_again_is_confirmed(capsys, reg, fandango, tmp_path):
    target(reg, "sleeper", "import time\ntime.sleep(60)\n")
    (reg / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["sleeper"]}))
    settle(reg)
    evaluate(capsys, reg, "-n", "1", "--timeout", "1", "--incidents", str(tmp_path / "private"))
    folder, = (tmp_path / "private").glob("incidents-*")
    incident, = json.loads((folder / "manifest.json").read_text())["incidents"]
    assert incident["kind"] == "hang" and incident["confirmed"] == 1 and incident["signal"] is None


@posix
def test_a_hang_that_was_only_a_slow_start_is_said_so(capsys, reg, fandango, tmp_path):
    # hangs the first time it is asked about a file, and answers the second time
    target(reg, "slowstart", "import pathlib, sys, time\nseen = pathlib.Path(sys.argv[1] + '.seen')\n"
                             "if not seen.exists():\n    seen.write_text('x'); time.sleep(60)\n")
    (reg / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["slowstart"]}))
    settle(reg)
    evaluate(capsys, reg, "-n", "1", "--timeout", "1", "--incidents", str(tmp_path / "private"))
    folder, = (tmp_path / "private").glob("incidents-*")
    incident, = json.loads((folder / "manifest.json").read_text())["incidents"]
    assert incident["kind"] == "hang" and incident["confirmed"] == 0
    assert "try it yourself" in (folder / incident["id"] / "REPORT.md").read_text()


@posix
def test_being_killed_for_what_it_used_is_an_incident_too(capsys, reg, fandango, tmp_path):
    target(reg, "oom", "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
    (reg / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["oom"]}))
    settle(reg)
    evaluate(capsys, reg, "-n", "2", "--incidents", str(tmp_path / "private"))
    folder, = (tmp_path / "private").glob("incidents-*")
    assert json.loads((folder / "manifest.json").read_text())["incidents"][0]["kind"] == "killed"


# --- the encrypted record, for CI

@posix
def test_with_a_key_nothing_is_printed_and_the_record_is_encrypted(capsys, crashing, fandango, age, recipients, tmp_path):
    out_dir = tmp_path / "private"
    code, out, err = evaluate(capsys, crashing, "-n", "4", "--incidents", str(out_dir), "--incident-recipients", str(recipients),
                              "--json-file", str(tmp_path / "e.json"))
    public = out + err + (tmp_path / "e.json").read_text()
    assert code == 0
    for secret in (SECRET_STDERR, SECRET_INPUT.decode(), "SIGSEGV", "killed by", "fanbase-check-", "fanbase-targets-", "hang"):
        assert secret not in public, secret
    assert "private record:" in err
    assert sorted(p.name for p in out_dir.iterdir()) == [BUNDLE_NAME]  # only the encrypted file; nothing in the clear
    assert (out_dir / BUNDLE_NAME).stat().st_mode & 0o777 == 0o600


@posix
def test_the_public_numbers_still_add_up_without_saying_why(capsys, crashing, fandango, age, recipients, tmp_path):
    _, out, _ = evaluate(capsys, crashing, "-n", "4", "--incidents", str(tmp_path / "p"), "--incident-recipients", str(recipients), "--json")
    crasher = next(t for t in json.loads(out)["specs"][0]["targets"] if t["name"] == "crasher")
    assert crasher["total"] == 4 and crasher["categories"]["error"] == 4 and crasher["categories"]["crash"] == 0 and crasher["reasons"] == []


@posix
def test_what_is_encrypted_is_the_record(capsys, crashing, fandango, age, recipients, tmp_path):
    evaluate(capsys, crashing, "-n", "4", "--incidents", str(tmp_path / "p"), "--incident-recipients", str(recipients))
    names = tar_of(age.plaintexts[-1]).getnames()
    assert "manifest.json" in names and "REPORT.md" in names
    assert any(n.endswith("/stderr.txt") for n in names) and any("/input-" in n for n in names)
    member = tar_of(age.plaintexts[-1]).extractfile("manifest.json")
    assert json.loads(member.read())["incidents"][0]["signal"] == "SIGSEGV"


@posix
def test_the_record_is_the_same_size_whether_or_not_something_broke(capsys, reg, crashing, fandango, age, recipients, tmp_path):
    evaluate(capsys, reg, "-n", "4", "--incidents", str(tmp_path / "quiet"), "--incident-recipients", str(recipients),
             "--targets", "strict")
    evaluate(capsys, crashing, "-n", "4", "--incidents", str(tmp_path / "loud"), "--incident-recipients", str(recipients))
    quiet, loud = (len(p) for p in age.plaintexts if p)  # (the empty ones are age trying the key before the run)
    assert quiet == loud and quiet % PAD == 0  # a whole number of megabytes
    assert (tmp_path / "quiet" / BUNDLE_NAME).stat().st_size == (tmp_path / "loud" / BUNDLE_NAME).stat().st_size


def test_and_it_is_always_written(capsys, reg, fandango, age, recipients, tmp_path):
    evaluate(capsys, reg, "-n", "4", "--incidents", str(tmp_path / "private"), "--incident-recipients", str(recipients))
    assert (tmp_path / "private" / BUNDLE_NAME).is_file()
    manifest = json.loads(tar_of(age.plaintexts[-1]).extractfile("manifest.json").read())
    assert manifest["incidents"] == []


@posix
def test_a_large_input_is_described_not_kept(capsys, crashing, fandango, tmp_path, monkeypatch):
    monkeypatch.setattr(incidents, "MAX_INPUT", 5)
    evaluate(capsys, crashing, "-n", "2", "--incidents", str(tmp_path / "private"))
    folder, = (tmp_path / "private").glob("incidents-*")
    incident, = json.loads((folder / "manifest.json").read_text())["incidents"]
    assert all(i["file"] is None and len(i["sha256"]) == 64 and i["size"] > 5 for i in incident["inputs"])
    assert list(folder.rglob("input-*")) == [] and "too large to keep" in (folder / incident["id"] / "REPORT.md").read_text()


# --- what is refused

def test_without_age_nothing_runs(capsys, reg, fandango, recipients, tmp_path, monkeypatch):
    monkeypatch.setattr(incidents.shutil, "which", lambda name, *a, **k: None)
    code, _, err = evaluate(capsys, reg, "--incidents", str(tmp_path / "p"), "--incident-recipients", str(recipients))
    assert code == 2 and "not installed" in err and fandango.calls == []


def test_a_key_that_age_cannot_use(capsys, reg, fandango, recipients, tmp_path, monkeypatch):
    monkeypatch.setattr(incidents, "_run", FakeAge(fail=True))
    real_which = shutil.which
    monkeypatch.setattr(incidents.shutil, "which", lambda name, *a, **k: "/fake/age" if name == "age" else real_which(name, *a, **k))
    code, _, err = evaluate(capsys, reg, "--incidents", str(tmp_path / "p"), "--incident-recipients", str(recipients))
    assert code == 2 and "age cannot use it" in err and fandango.calls == []


def test_a_key_file_that_is_not_there(capsys, reg, fandango, age, tmp_path):
    code, _, err = evaluate(capsys, reg, "--incidents", str(tmp_path / "p"), "--incident-recipients", str(tmp_path / "nope.txt"))
    assert code == 2 and "no such file" in err and fandango.calls == []


def test_a_key_without_a_place_to_write(capsys, reg, fandango, age, recipients):
    code, _, err = evaluate(capsys, reg, "--incident-recipients", str(recipients))
    assert code == 2 and "goes with --incidents" in err


def test_inputs_are_not_kept_beside_a_private_record(capsys, reg, fandango, age, recipients, tmp_path):
    code, _, err = evaluate(capsys, reg, "--incidents", str(tmp_path / "p"), "--incident-recipients", str(recipients), "--keep", str(tmp_path / "k"))
    assert code == 2 and "does not go with --hide-crashes" in err and not (tmp_path / "k").exists()


def test_a_failing_age_writes_nothing_half_done(capsys, reg, fandango, recipients, tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky(cmd, **kwargs):
        calls["n"] += 1
        return FakeAge()(cmd, **kwargs) if calls["n"] == 1 else subprocess.CompletedProcess(cmd, 1, b"", b"disk full")

    monkeypatch.setattr(incidents, "_run", flaky)
    real_which = shutil.which
    monkeypatch.setattr(incidents.shutil, "which", lambda name, *a, **k: "/fake/age" if name == "age" else real_which(name, *a, **k))
    code, _, err = evaluate(capsys, reg, "-n", "2", "--incidents", str(tmp_path / "p"), "--incident-recipients", str(recipients))
    assert code == 2 and "could not encrypt" in err
    assert list((tmp_path / "p").glob("*")) == []


# --- with the real age

@needs_age
@posix
def test_the_record_can_be_read_with_the_private_key_and_only_with_it(capsys, crashing, fandango, tmp_path):
    keys = tmp_path / "key.txt"
    other = tmp_path / "other.txt"
    for path in (keys, other):
        REAL_RUN(["age-keygen", "-o", str(path)], check=True, capture_output=True)
    public = REAL_RUN(["age-keygen", "-y", str(keys)], check=True, capture_output=True, text=True).stdout.strip()
    recipients = tmp_path / "recipients.txt"
    recipients.write_text(f"# the maintainer\n{public}\n")

    code, out, err = evaluate(capsys, crashing, "-n", "4", "--incidents", str(tmp_path / "private"), "--incident-recipients", str(recipients))
    assert code == 0 and SECRET_STDERR not in out + err
    bundle = tmp_path / "private" / BUNDLE_NAME
    assert SECRET_INPUT not in bundle.read_bytes() and SECRET_STDERR.encode() not in bundle.read_bytes()  # not in the clear

    opened = REAL_RUN(["age", "-d", "-i", str(keys), str(bundle)], check=True, capture_output=True).stdout
    assert len(opened) % PAD == 0
    manifest = json.loads(tar_of(opened).extractfile("manifest.json").read())
    assert manifest["incidents"][0]["signal"] == "SIGSEGV"
    wrong = REAL_RUN(["age", "-d", "-i", str(other), str(bundle)], capture_output=True)
    assert wrong.returncode != 0 and wrong.stdout == b""  # and nobody else can


@needs_age
def test_the_real_record_of_a_quiet_run_is_as_big_as_a_loud_one(capsys, reg, crashing, fandango, tmp_path):
    keys = tmp_path / "key.txt"
    REAL_RUN(["age-keygen", "-o", str(keys)], check=True, capture_output=True)
    recipients = tmp_path / "recipients.txt"
    recipients.write_text(REAL_RUN(["age-keygen", "-y", str(keys)], check=True, capture_output=True, text=True).stdout)
    evaluate(capsys, reg, "-n", "4", "--incidents", str(tmp_path / "quiet"), "--incident-recipients", str(recipients), "--targets", "strict")
    evaluate(capsys, crashing, "-n", "4", "--incidents", str(tmp_path / "loud"), "--incident-recipients", str(recipients))
    assert (tmp_path / "quiet" / BUNDLE_NAME).stat().st_size == (tmp_path / "loud" / BUNDLE_NAME).stat().st_size


# --- the record stays small enough to be mailed, whatever broke

COUNT_CRASH = (  # a different message for each of ten files, so each is a cause of its own
    "import os, pathlib, signal, sys\n"
    "print('message', pathlib.Path(sys.argv[1]).read_bytes()[-1:].decode(), file=sys.stderr, flush=True)\n"
    "os.kill(os.getpid(), signal.SIGSEGV)\n"
)


@pytest.fixture
def many_causes(reg):
    target(reg, "varied", COUNT_CRASH)
    (reg / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["varied"]}))
    settle(reg)
    return reg


@posix
def test_causes_beyond_the_limit_are_counted_not_written(capsys, many_causes, fandango, tmp_path, monkeypatch):
    monkeypatch.setattr(incidents, "MAX_INCIDENTS", 3)
    evaluate(capsys, many_causes, "-n", "10", "--incidents", str(tmp_path / "private"))
    folder, = (tmp_path / "private").glob("incidents-*")
    manifest = json.loads((folder / "manifest.json").read_text())
    assert len(manifest["incidents"]) == 3 and manifest["not_itemised"] == 7
    assert "7 further occurrence(s)" in (folder / "REPORT.md").read_text()


@posix
def test_inputs_beyond_the_budget_are_described_not_kept(capsys, many_causes, fandango, tmp_path, monkeypatch):
    monkeypatch.setattr(incidents, "MAX_KEPT", 3 * len(SECRET_INPUT + b" 0"))
    evaluate(capsys, many_causes, "-n", "10", "-j", "1", "--incidents", str(tmp_path / "private"))
    folder, = (tmp_path / "private").glob("incidents-*")
    manifest = json.loads((folder / "manifest.json").read_text())
    inputs = [i for incident in manifest["incidents"] for i in incident["inputs"]]
    assert len(inputs) == 10 and sum(i["file"] is not None for i in inputs) == 3  # all are listed; three are kept
    assert all(len(i["sha256"]) == 64 for i in inputs) and len(list(folder.rglob("input-*"))) == 3


def test_the_worst_record_can_be_mailed(tmp_path, monkeypatch):
    from fanbase.incident_cmds import MAX_MAIL
    from fanbase.targets import CRASH_KIND, Target, Verdict

    monkeypatch.setattr(incidents, "version_of", lambda target: "1.0")
    log = incidents.IncidentLog()
    big = tmp_path / "big.bin"
    big.write_bytes(b"\x01" * (incidents.MAX_INPUT - 1))  # (as big as an input may be)
    for n in range(incidents.MAX_INCIDENTS + 40):
        t = Target(f"target-{n % 7}", tmp_path, "T", ("png",), ("run",))
        name = chr(97 + n % 26) + chr(97 + n // 26)  # (letters: a number of three digits is made the same as any other)
        stderr = f"cause {name}\n".encode() + bytes(range(256)) * 31  # (about as much as a target may say: a cause of its own)
        verdict = Verdict("crash", "", CRASH_KIND, -11, stderr, ("a-long-command", "x" * 200, "INPUT"))
        for _ in range(5):  # (the same cause again and again adds nothing)
            log.record(spec=f"png/spec-{n % 22}", version="1.0", sha256="0" * 64, target=t, file=big, verdict=verdict, confirmed=True)
    meta = incidents.run_meta(1, 100)
    data = incidents.build_bundle(log, meta)
    assert len(data) <= MAX_MAIL - (1 << 20), len(data)  # (with room for age's own header and padding)
    assert len(log.incidents) == incidents.MAX_INCIDENTS
