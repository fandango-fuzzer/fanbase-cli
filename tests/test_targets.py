import subprocess
import sys

import pytest
from helpers import CRASH_SIGNAL
import yaml

from fanbase import targets
from fanbase.targets import (
    ACCEPTED,
    CRASH,
    ERROR,
    INVALID,
    LIMIT,
    TIMEOUT,
    UNSUPPORTED,
    TargetError,
    judge,
    load_target,
    run_target,
    unavailable,
    version_of,
)

# The suite replaces subprocess.run for every test (see conftest); this is the real one.
REAL_RUN = subprocess.run

posix = pytest.mark.skipif(sys.platform == "win32", reason="signals and limits are POSIX")
linux = pytest.mark.skipif(sys.platform != "linux", reason="the memory limit is only enforced on Linux")


@pytest.fixture(autouse=True)
def real_subprocess(monkeypatch):
    monkeypatch.setattr(subprocess, "run", REAL_RUN)


def make(root, name, harness=None, **config):
    """A target in a registry at `root`: a harness script, and a target.yml that runs it."""
    folder = root / "targets" / name
    folder.mkdir(parents=True, exist_ok=True)
    if harness is not None:
        (folder / "harness.py").write_text(harness)
    data = {"formats": ["png"], "run": ["{python}", "{dir}/harness.py", "{file}"], **config}
    (folder / "target.yml").write_text(yaml.safe_dump(data))
    return load_target(root, name)


def sample(tmp_path, content=b"hello", name="input.bin"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


ACCEPT = "import sys\nsys.exit(0)\n"
STRICT = (
    "import sys, pathlib\n"
    "data = pathlib.Path(sys.argv[1]).read_bytes()\n"
    "if data.startswith(b'bad'):\n"
    "    print('invalid header in', sys.argv[1], 'after 123456 bytes', file=sys.stderr); sys.exit(1)\n"
)


# --- judging one file

def test_exit_zero_accepts(tmp_path):
    target = make(tmp_path, "accept", ACCEPT)
    assert judge(target, sample(tmp_path)) == targets.Verdict(ACCEPTED)


def test_a_nonzero_exit_rejects_and_says_why_without_the_file_name_or_numbers(tmp_path):
    target = make(tmp_path, "strict", STRICT)
    verdict = judge(target, sample(tmp_path, b"bad data", "some-input-name.bin"))
    assert verdict.category == INVALID
    assert verdict.reason == "invalid header in <file> after N bytes"  # the same for every file that fails this way


def test_what_a_rejection_means_is_up_to_the_targets_rules(tmp_path):
    harness = "import sys\nprint('Image size (1199849310 pixels) exceeds limit of 89478485 pixels', file=sys.stderr)\nsys.exit(1)\n"
    plain = make(tmp_path, "plain", harness)
    ruled = make(tmp_path, "ruled", harness, classify=[
        {"match": "(?i)decompression ?bomb|exceeds limit", "as": "resource-limit"},
        {"match": "(?i)unsupported", "as": "unsupported"},
    ])
    assert judge(plain, sample(tmp_path)).category == INVALID
    assert judge(ruled, sample(tmp_path)).category == LIMIT
    unsupported = make(tmp_path, "ruled2", "import sys\nprint('Unsupported BMP compression', file=sys.stderr)\nsys.exit(1)\n",
                       classify=[{"match": "(?i)unsupported", "as": "unsupported"}])
    assert judge(unsupported, sample(tmp_path)).category == UNSUPPORTED


def test_a_target_that_fails_itself_is_not_a_rejection(tmp_path):
    target = make(tmp_path, "buggy", "raise ValueError('the harness is broken')\n")
    verdict = judge(target, sample(tmp_path))
    assert verdict.category == ERROR and verdict.reason == "ValueError: the harness is broken"


def test_a_command_that_is_not_there_is_an_error_not_a_rejection(tmp_path):
    target = make(tmp_path, "ghost", run=["definitely-not-a-command-xyz", "{file}"])
    verdict = judge(target, sample(tmp_path))
    assert verdict.category == ERROR and "definitely-not-a-command-xyz" in verdict.reason


@posix
def test_a_crash_is_a_crash(tmp_path):
    target = make(tmp_path, "crasher", f"import os, signal\nos.kill(os.getpid(), signal.{CRASH_SIGNAL})\n")
    verdict = judge(target, sample(tmp_path))
    assert verdict.category == CRASH and verdict.reason == f"killed by {CRASH_SIGNAL}"


@posix
def test_being_killed_for_resources_is_a_limit_not_a_crash(tmp_path):
    target = make(tmp_path, "killed", "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
    assert judge(target, sample(tmp_path)).category == LIMIT


def test_no_answer_in_time_is_a_timeout(tmp_path):
    target = make(tmp_path, "sleeper", "import time\ntime.sleep(60)\n")
    verdict = judge(target, sample(tmp_path), timeout=1)
    assert verdict.category == TIMEOUT and "1 seconds" in verdict.reason


@posix
def test_core_dumps_are_switched_off(tmp_path):
    target = make(tmp_path, "cores", "import resource, sys\nsys.exit(0 if resource.getrlimit(resource.RLIMIT_CORE) == (0, 0) else 1)\n")
    assert judge(target, sample(tmp_path)).category == ACCEPTED  # a dump of a crash would hold the input that caused it


@linux
def test_a_target_cannot_take_all_the_memory(tmp_path):
    target = make(tmp_path, "hog", "x = bytearray(900 * 1024 * 1024)\n")
    assert judge(target, sample(tmp_path), memory_mb=300).category != ACCEPTED
    assert judge(target, sample(tmp_path), memory_mb=2048).category == ACCEPTED


def test_a_reason_is_cleaned_and_cut(tmp_path):
    harness = "import sys\nprint('\\x1b[2Jbad ' + 'x' * 500, file=sys.stderr)\nsys.exit(1)\n"
    reason = judge(make(tmp_path, "loud", harness), sample(tmp_path)).reason
    assert "\x1b" not in reason and len(reason) == 100


def test_the_target_runs_where_it_is_told_to(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    target = make(tmp_path, "writer", "import pathlib\npathlib.Path('left-behind').write_text('x')\n")
    assert judge(target, sample(tmp_path), cwd=work).category == ACCEPTED
    assert (work / "left-behind").exists() and not (tmp_path / "targets" / "writer" / "left-behind").exists()


# --- all the files

def test_counting_and_the_reasons_that_come_up(tmp_path):
    target = make(tmp_path, "strict", STRICT)
    files = [sample(tmp_path, b"fine", f"ok{i}.bin") for i in range(7)] + [sample(tmp_path, b"bad", f"bad{i}.bin") for i in range(3)]
    result = run_target(target, files, jobs=4)
    assert (result.total, result.accepted) == (10, 7)
    assert result.counts["invalid"] == 3
    assert result.top_reasons() == [("invalid: invalid header in <file> after N bytes", 3)]
    assert result.per_second > 0


def test_jobs_do_not_change_the_answer(tmp_path):
    target = make(tmp_path, "strict", STRICT)
    files = [sample(tmp_path, b"bad" if i % 3 == 0 else b"ok", f"f{i}.bin") for i in range(12)]
    assert run_target(target, files, jobs=1).counts == run_target(target, files, jobs=6).counts


@posix
def test_a_public_report_does_not_say_which_files_crash_what(tmp_path):
    target = make(tmp_path, "crasher", f"import os, signal\nos.kill(os.getpid(), signal.{CRASH_SIGNAL})\n")
    files = [sample(tmp_path, b"x", f"f{i}.bin") for i in range(3)]
    shown = run_target(target, files)
    assert shown.counts[CRASH] == 3 and shown.top_reasons() == [(f"crash: killed by {CRASH_SIGNAL}", 3)]
    hidden = run_target(target, files, hide_crashes=True)
    assert hidden.counts[CRASH] == 0 and hidden.counts[ERROR] == 3 and hidden.top_reasons() == []


def test_the_version_is_what_the_target_says(tmp_path):
    target = make(tmp_path, "versioned", ACCEPT, version=["{python}", "-c", "print('12.3.0'); print('more')"])
    assert version_of(target) == "12.3.0"
    assert version_of(make(tmp_path, "unversioned", ACCEPT)) is None
    assert version_of(make(tmp_path, "failing", ACCEPT, version=["{python}", "-c", "raise SystemExit(1)"])) is None


# --- what has to be there

def test_what_is_missing_is_said(tmp_path):
    needs_command = make(tmp_path, "c", ACCEPT, needs={"commands": ["definitely-not-a-command-xyz"]})
    assert unavailable(needs_command) == "needs the command definitely-not-a-command-xyz"
    needs_module = make(tmp_path, "m", ACCEPT, needs={"python": ["fanbase_no_such_module"], "pip": ["No-Such-Package>=1"]})
    assert unavailable(needs_module) == "needs the Python module fanbase_no_such_module (pip install No-Such-Package>=1)"
    assert unavailable(make(tmp_path, "ok", ACCEPT, needs={"commands": [], "python": ["yaml"]})) is None
    assert unavailable(make(tmp_path, "nothing", ACCEPT)) is None


# --- target.yml

@pytest.mark.parametrize("change, complaint", [
    ({"run": None}, "run has to be a list of texts"),
    ({"run": ["cmd"]}, "has to name the file once"),
    ({"run": ["cmd", "{file}", "{file}"]}, "has to name the file once"),
    ({"run": ["cmd", "{file}", "{home}"]}, "{home}, which is not one of"),
    ({"run": "cmd {file}"}, "run has to be a list of texts"),
    ({"formats": []}, "formats has to be a list of texts"),
    ({"formats": ["../x"]}, "formats has to be a list of format names"),
    ({"title": 5}, "title has to be text"),
    ({"needs": ["x"]}, "needs has to be a mapping"),
    ({"needs": {"commands": ["/bin/sh"]}}, "command names, without a path"),
    ({"needs": {"commands": ["a b"]}}, "command names, without a path"),
    ({"needs": {"python": ["a b"]}}, "list of module names"),
    ({"needs": {"pip": ["--index-url=http://example.org"]}}, "refusing requirement"),
    ({"classify": [{"match": "(", "as": "invalid"}]}, "classify pattern"),
    ({"classify": [{"match": "x", "as": "crash"}]}, "`as` one of invalid, unsupported, resource-limit"),
    ({"classify": [{"match": "x" * 201, "as": "invalid"}]}, "longer than 200"),
    ({"classify": ["x"]}, "each classify rule needs"),
    ({"version": ["cmd", "{bogus}"]}, "{bogus}"),
])
def test_a_target_yml_of_the_wrong_shape(tmp_path, change, complaint):
    with pytest.raises(TargetError, match=complaint.replace("{", r"\{").replace("}", r"\}").replace("(", r"\(").replace("`", "`")):
        make(tmp_path, "bad", ACCEPT, **change)


def test_a_target_that_does_not_exist_or_is_not_one(tmp_path):
    with pytest.raises(TargetError, match="no target called ghost"):
        load_target(tmp_path, "ghost")
    with pytest.raises(TargetError, match="not a target name"):
        load_target(tmp_path, "../ghost")
    (tmp_path / "targets" / "list").mkdir(parents=True)
    (tmp_path / "targets" / "list" / "target.yml").write_text("- a\n- list\n")
    with pytest.raises(TargetError, match="expected a mapping"):
        load_target(tmp_path, "list")
    (tmp_path / "targets" / "yaml").mkdir()
    (tmp_path / "targets" / "yaml" / "target.yml").write_text("a: [unclosed\n")
    with pytest.raises(TargetError, match="invalid YAML"):
        load_target(tmp_path, "yaml")


def test_the_placeholders_are_filled_in(tmp_path):
    # {dir} is the target's folder (it has the target.yml) and {file} the file asked about. The paths are arguments, never
    # part of the source: a Windows path has backslashes in it.
    script = "import os, sys; sys.exit(0 if os.path.isfile(os.path.join(sys.argv[1], 'target.yml')) and os.path.isfile(sys.argv[2]) else 1)"
    target = make(tmp_path, "paths", run=["{python}", "-c", script, "{dir}", "{file}"])
    assert judge(target, sample(tmp_path)).category == ACCEPTED


def test_a_command_that_cannot_be_started_is_an_error_whatever_the_system(tmp_path, monkeypatch):
    # On POSIX a limits wrapper starts the command and a missing one is exit status 127; on Windows starting it fails
    # outright. Either way it is the target that is broken, and evaluating the other targets goes on.
    def refuse(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr("fanbase.targets.subprocess.run", refuse)
    target = make(tmp_path, "ghost", run=["definitely-not-a-command-xyz", "{file}"])
    verdict = judge(target, sample(tmp_path))
    assert verdict.category == ERROR and verdict.reason.startswith("cannot run ")


def test_addresses_in_memory_do_not_make_reasons_differ(tmp_path):
    harness = "import sys\nprint('[in#0/mpegts @ 0x7c45014000] could not find codec parameters', file=sys.stderr)\nsys.exit(1)\n"
    other = harness.replace("0x7c45014000", "0x7a89014000")
    a = judge(make(tmp_path, "a", harness), sample(tmp_path)).reason
    b = judge(make(tmp_path, "b", other), sample(tmp_path)).reason
    assert a == b == "[in#0/mpegts @ 0xN] could not find codec parameters"


def test_sizes_and_numbers_do_not_make_reasons_differ(tmp_path):
    def reason(text):
        harness = f"import sys\nprint({text!r}, file=sys.stderr)\nsys.exit(1)\n"
        return judge(make(tmp_path, "r", harness), sample(tmp_path)).reason

    assert reason("Picture size 58810x7219 is invalid") == reason("Picture size 36x33424 is invalid") == "Picture size NxN is invalid"
    assert reason("bad marker 0xe9 at offset 123456") == "bad marker 0xN at offset N"
    assert reason("SOF0 not supported") == "SOF0 not supported"  # a short number stays: it can be what the reason is


def test_a_target_is_found_from_a_relative_registry_path(tmp_path, monkeypatch):
    # Targets are run from another folder; a path relative to the registry would lead nowhere there.
    make(tmp_path, "rel", ACCEPT)
    monkeypatch.chdir(tmp_path)
    target = load_target(__import__("pathlib").Path("."), "rel")
    assert target.directory.is_absolute()
    work = tmp_path / "elsewhere"
    work.mkdir()
    assert judge(target, sample(tmp_path), cwd=work).category == ACCEPTED


def test_a_command_that_names_a_file_the_target_does_not_have(tmp_path):
    with pytest.raises(TargetError, match="run names harness.py, which is not in the target's folder"):
        make(tmp_path, "empty")  # no harness.py written
    with pytest.raises(TargetError, match="version names nothing.py, which is not in the target's folder"):
        make(tmp_path, "noversion", ACCEPT, version=["{python}", "{dir}/nothing.py"])
    with pytest.raises(TargetError, match=r"leaves the target's folder"):
        make(tmp_path, "up", ACCEPT, run=["{python}", "{dir}/../other/harness.py", "{file}"])


@posix
def test_a_public_report_does_not_say_which_files_hang_either(tmp_path):
    target = make(tmp_path, "sleeper", "import time\ntime.sleep(60)\n")
    files = [sample(tmp_path, b"x", f"f{i}.bin") for i in range(2)]
    shown = run_target(target, files, timeout=1)
    assert shown.counts[TIMEOUT] == 2 and shown.top_reasons() == [("timeout: no answer in 1 seconds", 2)]
    hidden = run_target(target, files, timeout=1, hide_crashes=True)
    assert hidden.counts[TIMEOUT] == 0 and hidden.counts[ERROR] == 2 and hidden.top_reasons() == []


def test_a_target_that_is_slow_on_a_spec_runs_out_of_its_time_not_everyone_elses(tmp_path):
    target = make(tmp_path, "slow", "import time\ntime.sleep(0.3)\n")
    files = [sample(tmp_path, b"x", f"f{i}.bin") for i in range(12)]
    result = run_target(target, files, jobs=1, budget=1.0)
    assert 0 < result.total < 12 and result.skipped == 12 - result.total  # every file is either judged or skipped
    assert result.accepted == result.total and result.seconds < 3


def test_with_enough_time_nothing_is_skipped(tmp_path):
    target = make(tmp_path, "quick", ACCEPT)
    files = [sample(tmp_path, b"x", f"f{i}.bin") for i in range(6)]
    result = run_target(target, files, budget=60)
    assert (result.total, result.skipped) == (6, 0)


@pytest.mark.parametrize("said, version", [
    ("ffmpeg version 7.1.5-0+deb13u1 Copyright (c) 2000-2026 the FFmpeg developers", "ffmpeg version 7.1.5-0+deb13u1"),
    ("Version: ImageMagick 7.1.1-43 Q16 aarch64 22550 https://imagemagick.org", "Version: ImageMagick 7.1.1-43 Q16 aarch64 22550"),
    ("libpng 1.6.59", "libpng 1.6.59"),
    ("12.3.0", "12.3.0"),
])
def test_a_version_is_the_version_and_not_the_notice_after_it(tmp_path, said, version):
    target = targets.Target("t", tmp_path, "T", ("png",), ("run",), version=(sys.executable, "-c", f"print({said!r})"))
    assert targets.version_of(target) == version
