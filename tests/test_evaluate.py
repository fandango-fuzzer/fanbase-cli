import json
import shutil
import subprocess
import sys

import pytest
import yaml
from conftest import build_registry, republish

from fanbase import contrib
from fanbase.cli import main

# The suite replaces subprocess.run for every test (see conftest); this is the real one.
REAL_RUN = subprocess.run
posix = pytest.mark.skipif(sys.platform == "win32", reason="signals are POSIX")

ACCEPT = "import sys\nsys.exit(0)\n"
STRICT = (  # rejects what starts with "bad", saying so
    "import sys, pathlib\n"
    "if pathlib.Path(sys.argv[1]).read_bytes().startswith(b'bad'):\n"
    "    print('invalid header in', sys.argv[1], file=sys.stderr); sys.exit(1)\n"
)
PICKY = (  # rejects what starts with "bad" or "odd"
    "import sys, pathlib\n"
    "data = pathlib.Path(sys.argv[1]).read_bytes()\n"
    "if data.startswith(b'odd'):\n    print('unsupported feature', file=sys.stderr); sys.exit(1)\n"
    "if data.startswith(b'bad'):\n    print('invalid header', file=sys.stderr); sys.exit(1)\n"
)


@pytest.fixture(autouse=True)
def real_subprocess(monkeypatch):
    monkeypatch.setattr(subprocess, "run", REAL_RUN)


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


class FakeFandango:
    """Stands in for `fandango fuzz`: makes the inputs it is told to, where it is told to.

    The inputs are a running sequence, different from each other, whatever the number of runs.
    """

    def __init__(self, content=None, limit=None, returncode=0, make=True, delay=0.0, overrun_on=None):
        # by default every fourth input is bad
        self.content = content or (lambda i: (b"bad input " if i % 4 == 0 else b"good input ") + str(i).encode())
        self.limit, self.returncode, self.make, self.delay, self.overrun_on = limit, returncode, make, delay, overrun_on
        self.calls = []
        self.made = 0

    def __call__(self, cmd, **kwargs):
        if cmd[0] != "/fake/fandango":
            return REAL_RUN(cmd, **kwargs)
        import pathlib
        import time

        n = int(cmd[cmd.index("-n") + 1])
        out = pathlib.Path(cmd[cmd.index("-d") + 1])
        self.calls.append({"cmd": cmd, "env": kwargs["env"], "n": n, "timeout": kwargs.get("timeout")})
        if self.overrun_on is not None and len(self.calls) - 1 == self.overrun_on:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        if self.make:
            room = n if self.limit is None else max(0, min(n, self.limit - self.made))
            if room or self.limit is None:
                out.mkdir(parents=True)
            for _ in range(room):
                time.sleep(self.delay)
                (out / f"fandango-{self.made:04d}.bin").write_bytes(self.content(self.made))
                self.made += 1
        return subprocess.CompletedProcess(cmd, self.returncode, "", "could not find enough" if self.returncode else "")


@pytest.fixture
def fake(monkeypatch):
    fandango = FakeFandango()
    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", fandango)
    monkeypatch.setattr("fanbase.evaluate.find_fandango", lambda: "/fake/fandango")
    return fandango


def target(reg, name, harness, formats=("png",), **config):
    folder = reg / "targets" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "harness.py").write_text(harness)
    (folder / "target.yml").write_text(yaml.safe_dump({
        "title": name.title(), "formats": list(formats), "run": ["{python}", "{dir}/harness.py", "{file}"], **config}))


def settle(reg):
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["--registry", str(reg), "reindex"]) == 0


@pytest.fixture
def reg(tmp_path):
    root = build_registry(tmp_path / "reg", {
        ("png", "png"): dict(version="1.0", description="PNG", decodes="mostly"),
        ("png", "png-odd"): dict(version="1.0", description="Odd PNG", decodes="rarely"),
        ("gif", "gif"): dict(version="1.0", description="GIF"),
    })
    target(root, "strict", STRICT)
    target(root, "picky", PICKY)
    (root / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["strict", "picky"]}))
    settle(root)
    return root


def evaluate(capsys, reg, *args):
    return run(capsys, "--registry", str(reg), "evaluate", *args)


# --- the numbers

def test_how_many_files_each_target_accepts(capsys, reg, fake):
    code, out, _ = evaluate(capsys, reg, "png", "-n", "20")
    assert code == 0
    assert "png/png  version 1.0: 20 inputs from seed 1" in out
    rows = {line.split()[0]: line.split() for line in out.splitlines() if line.startswith(("  strict", "  picky"))}
    assert rows["strict"][1:3] == ["15/20", "5"]  # every fourth input is bad: 5 of 20
    assert rows["picky"][1:3] == ["15/20", "5"]
    assert "strict: 5x invalid: invalid header in <file>" in out
    assert fake.calls[0]["cmd"][-2:] == ["--random-seed", "1"]


def test_the_report_as_json(capsys, reg, fake):
    code, out, _ = evaluate(capsys, reg, "png", "-n", "20", "--json")
    report = json.loads(out)
    assert code == 0 and report["schema"] == 1 and report["seed"] == 1 and report["count"] == 20
    spec, = report["specs"]
    assert spec["spec"] == "png/png" and spec["version"] == "1.0" and len(spec["sha256"]) == 64
    assert spec["generated"]["requested"] == 20 and spec["generated"]["produced"] == 20
    assert spec["generated"]["per_second"] > 0 and spec["error"] is None
    strict = next(t for t in spec["targets"] if t["name"] == "strict")
    assert strict["status"] == "ok" and strict["total"] == 20 and strict["accepted"] == 15
    assert strict["categories"] == {"accepted": 15, "invalid": 5, "unsupported": 0, "resource-limit": 0, "crash": 0, "timeout": 0, "error": 0}
    assert strict["reasons"] == [["invalid: invalid header in <file>", 5]] and strict["files_per_second"] > 0
    assert spec["best_accepted"] == 0.75 and spec["decodes"] == "mostly" and spec["expectation_met"] is True


def test_the_same_seed_is_asked_for_each_time(capsys, reg, fake):
    evaluate(capsys, reg, "png", "-n", "4", "--seed", "7")
    call = fake.calls[0]
    assert call["cmd"][-2:] == ["--random-seed", "7"] and call["env"]["PYTHONHASHSEED"] == "7"


def test_the_report_as_markdown(capsys, reg, fake):
    code, out, _ = evaluate(capsys, reg, "png", "-n", "8", "--markdown")
    assert code == 0 and out.startswith("## Evaluation of 1 spec(s)")
    assert "### `png/png` 1.0" in out and "| target | accepted | invalid |" in out
    assert "| strict | 6/8 | 2 |" in out
    assert "✅ expected mostly: best target accepts 75%, which is mostly; as expected" in out


def test_the_report_can_be_written_to_a_file_too(capsys, reg, fake, tmp_path):
    code, out, _ = evaluate(capsys, reg, "png", "-n", "4", "--json-file", str(tmp_path / "r.json"))
    assert code == 0 and "png/png" in out and not out.lstrip().startswith("{")  # text on screen
    assert json.loads((tmp_path / "r.json").read_text())["specs"][0]["spec"] == "png/png"


# --- what a spec is meant to do

def test_a_spec_that_decodes_as_often_as_it_says(capsys, reg, fake):
    _, out, _ = evaluate(capsys, reg, "png", "-n", "20")
    assert "expected mostly: best target accepts 75%, which is mostly; as expected" in out


def test_a_spec_that_does_not_is_said_so_and_fails_only_if_asked(capsys, reg, fake):
    code, out, _ = evaluate(capsys, reg, "png-odd", "-n", "20")  # says rarely, but 75% are accepted
    assert code == 0 and "expected rarely: best target accepts 75%, which is mostly; NOT as expected" in out
    code, _, _ = evaluate(capsys, reg, "png-odd", "-n", "20", "--strict")
    assert code == 1


@pytest.mark.parametrize("share, band_", [(1.0, "always"), (0.95, "always"), (0.94, "mostly"), (0.5, "mostly"),
                                          (0.49, "rarely"), (0.01, "rarely"), (0.0, "never")])
def test_the_bands(share, band_):
    from fanbase.evaluate import band

    assert band(share) == band_


def test_what_the_best_target_says_is_what_counts(capsys, reg, fake, monkeypatch):
    # picky also rejects "odd" inputs, so it accepts fewer than strict; strict decides
    monkeypatch.setattr(contrib, "_run", FakeFandango(content=lambda i: [b"good ", b"bad ", b"odd ", b"good "][i % 4] + str(i).encode()))
    _, out, _ = evaluate(capsys, reg, "png", "-n", "20")
    rows = {line.split()[0]: line.split() for line in out.splitlines() if line.startswith(("  strict", "  picky"))}
    assert rows["strict"][1] == "15/20" and rows["picky"][1] == "10/20"
    assert rows["picky"][2] == "10"  # without a rule saying otherwise, a rejection is invalid
    assert "best target accepts 75%" in out


def test_what_a_target_calls_unsupported_is_counted_so(capsys, reg, monkeypatch):
    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr("fanbase.evaluate.find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", FakeFandango(content=lambda i: [b"good ", b"bad ", b"odd ", b"good "][i % 4] + str(i).encode()))
    target(reg, "picky", PICKY, classify=[{"match": "unsupported", "as": "unsupported"}])
    settle(reg)
    _, out, _ = evaluate(capsys, reg, "png", "-n", "20", "--targets", "picky", "--json")
    categories = json.loads(out)["specs"][0]["targets"][0]["categories"]
    assert (categories["accepted"], categories["invalid"], categories["unsupported"]) == (10, 5, 5)


def test_a_spec_that_says_nothing_is_only_reported(capsys, reg, fake):
    (reg / "specs/gif/format.yml").write_text(yaml.safe_dump({"targets": ["strict"]}))
    target(reg, "strict", STRICT, formats=("png", "gif"))
    settle(reg)
    code, out, _ = evaluate(capsys, reg, "gif", "-n", "8", "--strict")
    assert code == 0 and "expected" not in out


# --- choosing targets

def test_a_spec_can_name_its_own_targets(capsys, reg, fake):
    republish(reg, "png-odd", targets=["picky"])
    _, out, _ = evaluate(capsys, reg, "png-odd", "-n", "4")
    assert "picky" in out and "strict" not in out.split("png/png-odd")[1]


def test_targets_can_be_chosen_on_the_command_line(capsys, reg, fake):
    _, out, _ = evaluate(capsys, reg, "png", "-n", "4", "--targets", "picky")
    assert "picky" in out and "  strict" not in out


def test_a_target_that_does_not_judge_the_format_is_left_out(capsys, reg, fake):
    target(reg, "strict", STRICT, formats=("gif",))
    code, out, _ = evaluate(capsys, reg, "png", "-n", "4", "--json")
    spec = json.loads(out)["specs"][0]
    skipped = next(t for t in spec["targets"] if t["name"] == "strict")
    assert skipped["status"] == "skipped" and skipped["note"] == "it does not judge png"


def test_a_format_with_no_targets(capsys, reg, fake):
    code, out, _ = evaluate(capsys, reg, "gif", "-n", "4")
    assert code == 0 and "no targets for gif: name some in specs/gif/format.yml" in out
    assert fake.calls == []  # and Fandango was not asked for anything


def test_a_target_that_is_not_there(capsys, reg, fake):
    code, _, err = evaluate(capsys, reg, "png", "--targets", "ghost")
    assert code == 2 and "png/png: no target called ghost" in err


def test_a_target_that_cannot_run_here(capsys, reg, fake):
    target(reg, "needy", ACCEPT, needs={"commands": ["definitely-not-a-command-xyz"]})
    code, out, _ = evaluate(capsys, reg, "png", "-n", "4", "--targets", "needy,strict")
    assert code == 0
    assert "(unavailable: needs the command definitely-not-a-command-xyz)" in out and "strict" in out
    code, _, _ = evaluate(capsys, reg, "png", "-n", "4", "--targets", "needy,strict", "--require-targets")
    assert code == 1


# --- what goes wrong

def test_a_spec_that_produces_nothing(capsys, reg, monkeypatch):
    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr("fanbase.evaluate.find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", FakeFandango(make=False, returncode=1))
    code, out, err = evaluate(capsys, reg, "png", "png-odd", "-n", "4")
    assert code == 1 and "png/png  version 1.0: no inputs: fandango produced no files" in out
    assert "no inputs from png/png, png/png-odd" in err


def test_one_spec_failing_does_not_stop_the_others(capsys, reg, monkeypatch):
    def fandango(cmd, **kwargs):
        if cmd[0] == "/fake/fandango" and "png-odd.fan" in cmd[3]:
            return subprocess.CompletedProcess(cmd, 1, "", "no good")
        return FakeFandango()(cmd, **kwargs)

    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr("fanbase.evaluate.find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", fandango)
    code, out, _ = evaluate(capsys, reg, "png", "png-odd", "-n", "4")
    assert code == 1 and "png/png-odd  version 1.0: no inputs" in out and "png/png  version 1.0: 4 inputs" in out


def test_fewer_inputs_than_asked_for_are_still_evaluated(capsys, reg, monkeypatch):
    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr("fanbase.evaluate.find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", FakeFandango(limit=6, returncode=1))
    code, out, _ = evaluate(capsys, reg, "png", "-n", "20")
    assert code == 0 and "6 inputs" in out and "note: Fandango found 6 of the 20 inputs asked for (could not find enough)" in out
    assert "strict" in out and "/6" in out


def test_without_fandango(capsys, reg, monkeypatch):
    monkeypatch.setattr("fanbase.evaluate.find_fandango", lambda: None)
    code, _, err = evaluate(capsys, reg, "png")
    assert code == 2 and "evaluate needs fandango" in err


# --- crashes are not for a public log

@pytest.fixture
def crashing(reg):
    target(reg, "crasher", "import os, signal\nos.kill(os.getpid(), signal.SIGSEGV)\n")
    (reg / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["strict", "crasher"]}))
    settle(reg)
    return reg


@posix
def test_a_crash_is_counted_and_said(capsys, crashing, fake):
    _, out, _ = evaluate(capsys, crashing, "png", "-n", "4", "--json")
    crasher = next(t for t in json.loads(out)["specs"][0]["targets"] if t["name"] == "crasher")
    assert crasher["categories"]["crash"] == 4 and crasher["reasons"] == [["crash: killed by SIGSEGV", 4]]


@posix
def test_for_a_public_report_it_is_not(capsys, crashing, fake):
    for flags in (["--json"], ["--markdown"], []):
        _, out, _ = evaluate(capsys, crashing, "png", "-n", "4", "--hide-crashes", *flags)
        assert "SIGSEGV" not in out and "killed by" not in out and "crash:" not in out
    _, out, _ = evaluate(capsys, crashing, "png", "-n", "4", "--hide-crashes", "--json")
    crasher = next(t for t in json.loads(out)["specs"][0]["targets"] if t["name"] == "crasher")
    assert crasher["categories"]["crash"] == 0 and crasher["categories"]["error"] == 4 and crasher["reasons"] == []


def test_the_inputs_are_not_kept_unless_asked(capsys, reg, fake, tmp_path):
    import glob
    import tempfile

    before = set(glob.glob(f"{tempfile.gettempdir()}/fanbase-*"))
    evaluate(capsys, reg, "png", "-n", "4")
    assert set(glob.glob(f"{tempfile.gettempdir()}/fanbase-*")) == before


def test_inputs_can_be_kept_to_look_at(capsys, reg, fake, tmp_path):
    evaluate(capsys, reg, "png", "-n", "4", "--keep", str(tmp_path / "kept"))
    assert sorted(p.name for p in (tmp_path / "kept/png/png").iterdir()) == [f"fandango-000{i}.bin" for i in range(4)]


def test_but_not_together_with_hiding_crashes(capsys, reg, fake, tmp_path):
    code, _, err = evaluate(capsys, reg, "png", "--hide-crashes", "--keep", str(tmp_path / "kept"))
    assert code == 2 and "does not go with --hide-crashes" in err and not (tmp_path / "kept").exists()


# --- choosing the specs

def test_nothing_chosen(capsys, reg, fake):
    code, _, err = evaluate(capsys, reg)
    assert code == 2 and "name the specs to evaluate" in err


def test_every_spec(capsys, reg, fake):
    code, out, _ = evaluate(capsys, reg, "--all", "-n", "4")
    assert code == 0 and [line.split()[0] for line in out.splitlines() if "version 1.0:" in line] == ["gif/gif", "png/png", "png/png-odd"]


def test_what_changed_and_what_builds_on_it(capsys, reg, fake, tmp_path):
    republish(reg, "png-odd", extends=["png"], decodes="rarely")
    settle(reg)
    before = tmp_path / "before"
    shutil.copytree(reg, before)
    republish(reg, "png", "<start> ::= 'png 2'\n", version="1.1")  # png changes; png-odd extends it
    code, out, _ = evaluate(capsys, reg, "--changed", "--base", str(before), "-n", "4")
    assert code == 0
    evaluated = [line.split()[0] for line in out.splitlines() if "version " in line and ":" in line]
    assert evaluated == ["png/png", "png/png-odd"]  # not gif/gif


def test_changed_needs_a_base(capsys, reg, fake):
    code, _, err = evaluate(capsys, reg, "--changed")
    assert code == 2 and "--changed needs --base" in err


def test_nothing_changed_nothing_evaluated(capsys, reg, fake, tmp_path):
    before = tmp_path / "before"
    shutil.copytree(reg, before)
    code, out, _ = evaluate(capsys, reg, "--changed", "--base", str(before))
    assert code == 0 and fake.calls == []


# --- the targets of a checkout

def test_the_targets_and_whether_they_can_run(capsys, reg):
    target(reg, "needy", ACCEPT, needs={"commands": ["definitely-not-a-command-xyz"]})
    target(reg, "versioned", ACCEPT, version=["{python}", "-c", "print('3.1.4')"])
    code, out, _ = run(capsys, "--registry", str(reg), "targets")
    lines = {line.split()[0]: line for line in out.splitlines()}
    assert code == 0
    assert "cannot run here: needs the command definitely-not-a-command-xyz" in lines["needy"]
    assert lines["versioned"].endswith("ready 3.1.4") and lines["strict"].endswith("ready")


def test_a_registry_without_targets(capsys, tmp_path):
    root = build_registry(tmp_path / "r", {("png", "png"): dict(version="1.0", description="x")})
    assert "defines no targets" in run(capsys, "--registry", str(root), "targets")[1]


# --- with the real Fandango

@pytest.mark.skipif(contrib.find_fandango() is None, reason="fandango is not installed")
def test_with_the_real_fandango(capsys, tmp_path, monkeypatch):
    root = build_registry(tmp_path / "real", {("txt", "txt"): dict(version="1.0", description="x", decodes="mostly")})
    (root / "specs/txt/txt/txt.fan").write_text('<start> ::= "good" "-" <n>\n<n> ::= "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8"\n')
    target(root, "any", ACCEPT, formats=("txt",))
    (root / "specs/txt/format.yml").write_text(yaml.safe_dump({"targets": ["any"]}))
    settle(root)
    code, out, _ = run(capsys, "--registry", str(root), "evaluate", "txt", "-n", "5", "--json")
    spec = json.loads(out)["specs"][0]
    assert code == 0 and spec["generated"]["produced"] == 5 and spec["targets"][0]["accepted"] == 5
    assert spec["expectation_met"] is False  # it says mostly, but everything is accepted: always
    again = json.loads(run(capsys, "--registry", str(root), "evaluate", "txt", "-n", "5", "--json")[1])
    assert [t["accepted"] for t in again["specs"][0]["targets"]] == [5]


# --- a time budget for Fandango

def fandango_for(monkeypatch, fandango):
    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr("fanbase.evaluate.find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", fandango)
    return fandango


def test_a_fast_spec_takes_two_runs(capsys, reg, monkeypatch):
    fandango = fandango_for(monkeypatch, FakeFandango())
    code, out, _ = evaluate(capsys, reg, "png", "-n", "100", "--json")
    spec = json.loads(out)["specs"][0]
    assert code == 0 and spec["generated"]["produced"] == 100 and spec["generated"]["note"] is None
    assert [c["n"] for c in fandango.calls] == [5, 95]  # a first batch to see how fast, then all that is left


def test_each_run_has_its_own_seed(capsys, reg, monkeypatch):
    fandango = fandango_for(monkeypatch, FakeFandango())
    evaluate(capsys, reg, "png", "-n", "40", "--seed", "7")
    assert [c["cmd"][-2:] for c in fandango.calls] == [["--random-seed", "7"], ["--random-seed", "8"]]
    assert [c["env"]["PYTHONHASHSEED"] for c in fandango.calls] == ["7", "8"]


def test_a_slow_spec_is_stopped_by_its_budget_and_what_it_made_is_used(capsys, reg, monkeypatch):
    fandango_for(monkeypatch, FakeFandango(delay=0.05))
    code, out, _ = evaluate(capsys, reg, "png", "-n", "1000", "--budget", "0.6", "--json")
    spec = json.loads(out)["specs"][0]
    produced = spec["generated"]["produced"]
    assert code == 0 and 0 < produced < 1000
    assert spec["generated"]["note"].startswith("the budget of 0.6s ran out with ")
    assert spec["targets"][0]["total"] == produced  # and those are the ones that were judged


def test_a_batch_that_overruns_loses_only_itself(capsys, reg, monkeypatch):
    fandango_for(monkeypatch, FakeFandango(overrun_on=1))
    code, out, _ = evaluate(capsys, reg, "png", "-n", "100", "--json")
    spec = json.loads(out)["specs"][0]
    assert code == 0 and spec["generated"]["produced"] == 5 and "ran out with 5 of the 100 inputs" in spec["generated"]["note"]


def test_a_spec_with_few_different_inputs_stops_when_there_are_no_new_ones(capsys, reg, monkeypatch):
    fandango = fandango_for(monkeypatch, FakeFandango(limit=5, returncode=1))
    code, out, _ = evaluate(capsys, reg, "png", "-n", "100", "--json")
    spec = json.loads(out)["specs"][0]
    assert code == 0 and spec["generated"]["produced"] == 5
    assert spec["generated"]["note"].startswith("Fandango found 5 of the 100 inputs asked for")
    assert len(fandango.calls) == 3  # one that made them, and two that had nothing new


def test_the_inputs_of_all_the_runs_are_kept_together(capsys, reg, monkeypatch, tmp_path):
    fandango_for(monkeypatch, FakeFandango())
    evaluate(capsys, reg, "png", "-n", "30", "--keep", str(tmp_path / "kept"))
    assert sorted(p.name for p in (tmp_path / "kept/png/png").iterdir()) == [f"fandango-{i:04d}.bin" for i in range(30)]


def test_without_a_budget_it_is_one_run(capsys, reg, monkeypatch):
    fandango = fandango_for(monkeypatch, FakeFandango())
    code, out, _ = evaluate(capsys, reg, "png", "-n", "100", "--budget", "0", "--json")
    assert code == 0 and json.loads(out)["specs"][0]["generated"]["produced"] == 100
    assert [c["n"] for c in fandango.calls] == [100]


def test_a_target_that_runs_out_of_time_is_said_so(capsys, reg, fake):
    target(reg, "slow", "import time\ntime.sleep(0.25)\n")
    (reg / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["slow"]}))
    settle(reg)
    code, out, _ = evaluate(capsys, reg, "png", "-n", "20", "-j", "1", "--judge-budget", "0.8", "--json")
    slow = json.loads(out)["specs"][0]["targets"][0]
    assert code == 0 and 0 < slow["total"] < 20 and slow["skipped"] == 20 - slow["total"]
    _, text, _ = evaluate(capsys, reg, "png", "-n", "20", "-j", "1", "--judge-budget", "0.8")
    assert "slow: out of time after " in text and " of 20 files" in text
    _, md, _ = evaluate(capsys, reg, "png", "-n", "20", "-j", "1", "--judge-budget", "0.8", "--markdown")
    assert "_slow: out of time after " in md
