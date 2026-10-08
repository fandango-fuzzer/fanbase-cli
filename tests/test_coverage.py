"""`fanbase evaluate --coverage`: how much of a parser the generated files reach, against real files."""

import json
import subprocess
import sys

import pytest
import yaml
from conftest import build_registry
from helpers import REAL_RUN, FakeFandango, settle

from fanbase import contrib, coverage
from fanbase.cli import main
from fanbase.coverage import CoverageError, checkpoints, parse_snapshot
from fanbase.targets import TargetError, load_target

posix = pytest.mark.skipif(sys.platform == "win32", reason="signals are POSIX")

# A library whose coverage is easy to know: a file reaches line N for each byte N in it.
HARNESS = (
    "import json, os, pathlib, sys\n"
    "state = pathlib.Path(os.environ['FANBASE_TEST_STATE'])\n"
    "now = json.loads(state.read_text()) if state.exists() else {'lines': [], 'branches': []}\n"
    "data = pathlib.Path(sys.argv[1]).read_bytes()\n"
    "now['lines'] = sorted(set(now['lines']) | {f'lib.c:{b}' for b in data})\n"
    "now['branches'] = sorted(set(now['branches']) | {f'lib.c:{b}:0' for b in data})\n"
    "state.write_text(json.dumps(now))\n"
    "sys.exit(1 if data[:1] == b'\\x01' else 0)\n"  # (it rejects some files, having run its code all the same)
)
RESET = "import os, pathlib\npathlib.Path(os.environ['FANBASE_TEST_STATE']).unlink(missing_ok=True)\n"
SNAPSHOT = (
    "import json, os, pathlib\n"
    "state = pathlib.Path(os.environ['FANBASE_TEST_STATE'])\n"
    "now = json.loads(state.read_text()) if state.exists() else {'lines': [], 'branches': []}\n"
    "print(json.dumps({'schema': 1, 'lines': {'total': 256, 'covered': now['lines']},\n"
    "                  'branches': {'total': 512, 'covered': now['branches']}}))\n"
)


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def cov_target(reg, name="libfake", formats=("png",), seeds=(bytes([5, 6, 200]),), snapshot=SNAPSHOT, **config):
    folder = reg / "targets" / name
    (folder / "seeds").mkdir(parents=True, exist_ok=True)
    for old in (folder / "seeds").glob("*"):
        old.unlink()
    (folder / "harness.py").write_text(HARNESS)
    (folder / "reset.py").write_text(RESET)
    (folder / "snapshot.py").write_text(snapshot)
    for n, content in enumerate(seeds):
        (folder / "seeds" / f"seed-{n}.bin").write_bytes(content)
    (folder / "target.yml").write_text(yaml.safe_dump({
        "title": "Libfake", "formats": list(formats), "run": ["{python}", "{dir}/harness.py", "{file}"],
        "version": ["{python}", "-c", "print('libfake 1.0')"],
        "coverage": {"reset": ["{python}", "{dir}/reset.py"], "snapshot": ["{python}", "{dir}/snapshot.py"], "seeds": ["seeds/*"]},
        **config}))


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", REAL_RUN)
    monkeypatch.setenv("FANBASE_TEST_STATE", str(tmp_path / "state.json"))
    return tmp_path / "state.json"


@pytest.fixture
def reg(tmp_path):
    root = build_registry(tmp_path / "reg", {("png", "png"): dict(version="1.0", description="PNG"),
                                              ("gif", "gif"): dict(version="1.0", description="GIF")})
    cov_target(root)
    for fmt, targets in (("png", []), ("gif", [])):  # (the format names no target: the coverage target is not one of them)
        (root / "specs" / fmt / "format.yml").write_text(yaml.safe_dump({"targets": targets}) if targets else "title: " + fmt + "\n")
    settle(root)
    return root


@pytest.fixture
def fandango(monkeypatch):
    # input i has the bytes 0..i, so the first n inputs reach the lines 0..n-1; and the first byte is 0
    made = FakeFandango(content=lambda i: bytes(range(i + 1)))
    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr("fanbase.evaluate.find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", made)
    return made


def evaluate(capsys, reg, *args):
    return run(capsys, "--registry", str(reg), "evaluate", "png", "-j", "1", *args)


def coverage_of(out):
    return json.loads(out)["specs"][0]["targets"][0]["coverage"]


# --- the numbers

def test_the_curve_and_the_comparison_with_real_files(capsys, reg, fandango):
    code, out, _ = evaluate(capsys, reg, "-n", "12", "--coverage", "--curve", "1,5,10", "--json")
    assert code == 0
    c = coverage_of(out)
    assert [p["inputs"] for p in c["curve"]] == [1, 5, 10, 12]  # (and the last one)
    assert [p["lines"] for p in c["curve"]] == [1, 5, 10, 12] and [p["branches"] for p in c["curve"]] == [1, 5, 10, 12]
    assert c["curve"][-1]["lines_share"] == round(12 / 256, 4) and c["curve"][0]["branches_share"] == round(1 / 512, 4)
    assert c["seeds"] == {"files": 1, "lines": 3, "branches": 3, "lines_share": round(3 / 256, 4)}  # the lines 5, 6 and 200
    assert c["only_generated_lines"] == 10  # 0..11 without 5 and 6
    assert c["only_seed_lines"] == 1  # 200
    assert c["lines_total"] == 256 and c["branches_total"] == 512 and c["note"] is None


def test_the_default_curve_is_powers_of_ten_that_there_are_files_for(capsys, reg, fandango):
    _, out, _ = evaluate(capsys, reg, "-n", "12", "--coverage", "--json")
    assert [p["inputs"] for p in coverage_of(out)["curve"]] == [1, 10, 12]


def test_a_curve_longer_than_the_inputs_is_cut_off(capsys, reg, fandango):
    _, out, _ = evaluate(capsys, reg, "-n", "3", "--coverage", "--curve", "1,10,100,1000", "--json")
    assert [p["inputs"] for p in coverage_of(out)["curve"]] == [1, 3]


def test_what_the_verdict_pass_ran_does_not_count(capsys, reg, fandango):
    # the harness is asked about every file once for the verdicts, which reach the lines 0..11; the coverage starts from nothing
    _, out, _ = evaluate(capsys, reg, "-n", "12", "--coverage", "--curve", "1", "--json")
    assert coverage_of(out)["curve"][0]["lines"] == 1


def test_a_file_the_library_rejects_still_counts_for_what_it_ran(capsys, reg, fandango, monkeypatch):
    monkeypatch.setattr(fandango, "content", lambda i: bytes([1, 2, 3]) if i == 0 else bytes(range(i + 10)))  # the first is rejected
    _, out, _ = evaluate(capsys, reg, "-n", "2", "--coverage", "--curve", "1", "--json")
    c = coverage_of(out)
    assert c["curve"][0]["lines"] == 3  # lines 1, 2 and 3, although the harness said no
    target = json.loads(out)["specs"][0]["targets"][0]
    assert target["accepted"] == 1 and target["total"] == 2


def test_the_real_files_are_the_baseline_not_the_inputs(capsys, reg, fandango):
    cov_target(reg, seeds=(bytes([5]), bytes([6]), bytes([7, 7])))
    _, out, _ = evaluate(capsys, reg, "-n", "4", "--coverage", "--curve", "4", "--json")
    c = coverage_of(out)
    assert c["seeds"]["files"] == 3 and c["seeds"]["lines"] == 3 and c["only_generated_lines"] == 4 and c["only_seed_lines"] == 3


# --- how it is said

def test_in_words(capsys, reg, fandango):
    code, out, _ = evaluate(capsys, reg, "-n", "12", "--coverage", "--curve", "1,10")
    assert code == 0
    assert "coverage of libfake libfake 1.0: 256 lines, 512 branches" in out
    assert "1 real file(s) reach 3 (1.2%) lines and 3 (0.6%) branches" in out
    assert "10 generated: 10 (3.9%) lines, 10 (2.0%) branches" in out
    assert "the generated files reach 10 lines the real ones do not; the real ones reach 1 the generated ones do not" in out


def test_in_markdown(capsys, reg, fandango):
    code, out, _ = evaluate(capsys, reg, "-n", "12", "--coverage", "--curve", "1,10", "--markdown")
    assert code == 0 and "**Coverage of libfake libfake 1.0: 256 lines, 512 branches**" in out
    assert "| inputs | lines | branches |" in out and "| 1 real | 3 | 3 |" in out and "| 10 generated | 10 (3.9%) | 10 |" in out


def test_the_target_is_judged_too(capsys, reg, fandango):
    _, out, _ = evaluate(capsys, reg, "-n", "4", "--coverage", "--json")
    target = json.loads(out)["specs"][0]["targets"][0]
    assert target["name"] == "libfake" and target["total"] == 4 and target["version"] == "libfake 1.0"


# --- when it is not asked for, or cannot be

def test_without_the_flag_nothing_is_measured_and_the_target_is_not_run(capsys, reg, fandango):
    code, out, err = evaluate(capsys, reg, "-n", "4", "--json")
    assert code == 0 and "coverage" not in out and json.loads(out)["specs"][0]["targets"] == []


def test_a_target_the_format_names_is_measured_once(capsys, reg, fandango):
    (reg / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["libfake"]}))
    settle(reg)
    _, out, _ = evaluate(capsys, reg, "-n", "4", "--coverage", "--json")
    targets = json.loads(out)["specs"][0]["targets"]
    assert [t["name"] for t in targets] == ["libfake"] and "coverage" in targets[0]


def test_coverage_only_leaves_the_targets_the_format_names_out(capsys, reg, fandango):
    target_dir = reg / "targets" / "plain"
    target_dir.mkdir()
    (target_dir / "harness.py").write_text("")
    (target_dir / "target.yml").write_text(yaml.safe_dump({"formats": ["png"], "run": ["{python}", "{dir}/harness.py", "{file}"]}))
    (reg / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["plain"]}))
    settle(reg)
    _, both, _ = evaluate(capsys, reg, "-n", "2", "--coverage", "--json")
    assert [t["name"] for t in json.loads(both)["specs"][0]["targets"]] == ["plain", "libfake"]
    _, only, _ = evaluate(capsys, reg, "-n", "2", "--coverage-only", "--json")
    assert [t["name"] for t in json.loads(only)["specs"][0]["targets"]] == ["libfake"] and "coverage" in only


def test_a_format_with_nothing_to_measure_it_with(capsys, reg, fandango):
    code, out, _ = run(capsys, "--registry", str(reg), "evaluate", "gif", "-n", "4", "--coverage", "--json")
    spec = json.loads(out)["specs"][0]
    assert code == 0 and spec["targets"] == [] and "no targets for gif" in spec["unjudged"]


def test_without_real_files_there_is_still_a_curve(capsys, reg, fandango):
    cov_target(reg, seeds=())
    _, out, _ = evaluate(capsys, reg, "-n", "4", "--coverage", "--curve", "2", "--json")
    c = coverage_of(out)
    assert c["seeds"]["lines"] is None and c["only_generated_lines"] is None and [p["lines"] for p in c["curve"]] == [2, 4]
    assert "no real files to compare with" in c["note"]


@pytest.mark.parametrize("snapshot, complaint", [
    ("print('this is not json')\n", "the snapshot is not JSON"),
    ("print('{\"schema\": 1}')\n", "should be a JSON object, schema 1, with `lines`"),
    ("print('{\"lines\": {\"total\": \"many\", \"covered\": []}}')\n", "the snapshot's lines should be"),
    ("print('{\"lines\": {\"total\": 3, \"covered\": [1, 2]}}')\n", "should list at most"),
    ("import sys\nprint('no gcov data', file=sys.stderr)\nsys.exit(3)\n", "the snapshot failed: no gcov data"),
])
def test_a_snapshot_that_is_wrong_is_said_and_the_rest_goes_on(capsys, reg, fandango, snapshot, complaint):
    cov_target(reg, snapshot=snapshot)
    code, out, _ = evaluate(capsys, reg, "-n", "4", "--coverage", "--json")
    target = json.loads(out)["specs"][0]["targets"][0]
    assert code == 0 and complaint in target["note"] and "coverage" not in target and target["total"] == 4


@posix
def test_a_snapshot_that_never_comes(capsys, reg, fandango, monkeypatch):
    cov_target(reg, snapshot="import time\ntime.sleep(60)\n")
    monkeypatch.setattr(coverage, "SNAPSHOT_TIMEOUT", 1)
    code, out, _ = evaluate(capsys, reg, "-n", "2", "--coverage", "--json")
    assert code == 0 and "did not finish in 1 seconds" in json.loads(out)["specs"][0]["targets"][0]["note"]


def test_a_reset_that_fails(capsys, reg, fandango):
    (reg / "targets/libfake/reset.py").write_text("import sys\nprint('cannot', file=sys.stderr)\nsys.exit(1)\n")
    _, out, _ = evaluate(capsys, reg, "-n", "2", "--coverage", "--json")
    assert "the reset failed: cannot" in json.loads(out)["specs"][0]["targets"][0]["note"]


def test_a_bad_curve(capsys, reg, fandango):
    for bad in ("", "a,b", "0,10", "-1"):
        code, _, err = evaluate(capsys, reg, "--coverage", "--curve", bad)
        assert code == 2 and "--curve is a list of numbers of inputs" in err


# --- the parts

def test_checkpoints():
    assert checkpoints((1, 10, 100, 1000), 12) == [1, 10, 12]
    assert checkpoints((1, 10), 10) == [1, 10]
    assert checkpoints((5,), 3) == [3]
    assert checkpoints((1, 10), 0) == []


def test_a_snapshot_is_read_strictly():
    ok = parse_snapshot('{"schema": 1, "lines": {"total": 5, "covered": ["a:1", "a:2"]}}')
    assert ok.lines == {"a:1", "a:2"} and ok.lines_total == 5 and ok.branches is None
    for bad in ("", "[]", '{"schema": 2, "lines": {"total": 1, "covered": []}}', '{"lines": {"total": true, "covered": []}}',
                '{"lines": {"total": -1, "covered": []}}', '{"lines": {"total": 1, "covered": [""]}}',
                '{"lines": {"total": 1, "covered": []}, "branches": {"total": 1}}'):
        with pytest.raises(CoverageError):
            parse_snapshot(bad)


# --- what a target may say about its coverage

def target_yml(reg, section):
    folder = reg / "targets" / "odd"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "x.py").write_text("")
    (folder / "target.yml").write_text(yaml.safe_dump({"formats": ["png"], "run": ["{python}", "{dir}/x.py", "{file}"], "coverage": section}))
    return load_target(reg, "odd")


@pytest.mark.parametrize("section, complaint", [
    ({"reset": ["true"]}, "needs a snapshot"),
    ({"snapshot": ["true", "{file}"]}, "cannot have {file}"),
    ({"snapshot": ["true"], "reset": ["true", "{file}"]}, "cannot have {file}"),
    ({"snapshot": ["true"], "colour": "red"}, "coverage has to be a mapping"),
    ({"snapshot": ["true"], "seeds": ["../../etc/*"]}, "cannot leave the target's folder"),
    ({"snapshot": ["true"], "seeds": "seeds/*"}, "coverage.seeds has to be a list"),
    ({"snapshot": ["{dir}/nope.py"]}, "which is not in the target's folder"),
    ("snapshot", "coverage has to be a mapping"),
])
def test_a_coverage_section_that_is_wrong(reg, section, complaint):
    with pytest.raises(TargetError, match=complaint.replace("{", r"\{").replace("}", r"\}")):
        target_yml(reg, section)


def test_a_coverage_section_that_is_right(reg):
    target = target_yml(reg, {"snapshot": ["{python}", "{dir}/x.py"], "seeds": ["seeds/*", "/opt/seeds/*"]})
    assert target.coverage is not None and target.coverage.seeds == ("seeds/*", "/opt/seeds/*") and target.coverage.reset == ()
