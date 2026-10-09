"""`evaluate --reuse`: a spec that nothing it depends on has changed for is not evaluated again.

What the evaluation of a spec depends on is in fingerprint.py; each test here changes one of those things, and sees that
the spec is evaluated again, or changes something that is not one, and sees that it is not."""

import json
import subprocess
from pathlib import PurePath

import pytest
import yaml
from conftest import build_registry, republish
from helpers import ACCEPT, REAL_RUN, STRICT, FakeFandango, settle, target

from fanbase import contrib, quality
from fanbase.cli import main


@pytest.fixture(autouse=True)
def real_subprocess(monkeypatch):
    monkeypatch.setattr(subprocess, "run", REAL_RUN)


@pytest.fixture
def fake(monkeypatch):
    fandango = FakeFandango()
    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", fandango)
    monkeypatch.setattr("fanbase.evaluate.find_fandango", lambda: "/fake/fandango")
    return fandango


@pytest.fixture
def parser_version(tmp_path):
    """The file that the version command of the `lenient` target prints: change it, and the parser has been upgraded."""
    path = tmp_path / "parser-version"
    path.write_text("1.0")
    return path


@pytest.fixture
def reg(tmp_path, parser_version):
    root = build_registry(tmp_path / "reg", {
        ("png", "png"): dict(version="1.0", description="PNG", extensions=["png"], decodes="mostly"),
        ("png", "png-apng"): dict(version="1.0", description="APNG", extensions=["png"], extends=["png"], decodes="mostly"),
        ("gif", "gif"): dict(version="1.0", description="GIF", extensions=["gif"], decodes="mostly"),
    })
    target(root, "strict", STRICT, formats=("png",))
    target(root, "lenient", ACCEPT, formats=("gif",),
           version=["{python}", "-c", f"print(open({str(parser_version)!r}).read())"])
    (root / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["strict"]}))
    (root / "specs/gif/format.yml").write_text(yaml.safe_dump({"targets": ["lenient"]}))
    settle(root)
    return root


def evaluate(capsys, reg, tmp_path, *extra, name="report.json"):
    """`fanbase evaluate --all` with a fixed seed; the report and the quality results made of it."""
    report = tmp_path / name
    code = main(["--registry", str(reg), "evaluate", "--all", "-n", "8", "--seed", "1", "--budget", "0", "--json-file", str(report), *extra])
    out = capsys.readouterr()
    results = tmp_path / f"quality-{name}"
    assert main(["--registry", str(reg), "quality", "build", str(report), "-o", str(results)]) == 0
    capsys.readouterr()
    return code, json.loads(report.read_text()), results, out.err


def asked_about(fake):
    """The specs that Fandango was asked for inputs of."""
    names = {PurePath(part).stem for call in fake.calls for part in call["cmd"] if part.endswith(".fan")}  # (a path has \ on Windows)
    fake.calls.clear()
    return names


def reused(document):
    return {s["spec"].split("/")[-1] for s in document["specs"] if "reused" in s}


@pytest.fixture
def first(fake, capsys, reg, tmp_path):
    """The first, full, evaluation, and its quality results."""
    code, document, results, _ = evaluate(capsys, reg, tmp_path)
    assert code == 0 and asked_about(fake) == {"png", "png-apng", "gif"}
    return document, results


def again(capsys, reg, tmp_path, first, *extra):
    _, results = first
    return evaluate(capsys, reg, tmp_path, "--reuse", str(results), *extra, name="again.json")


# --- what is kept

def test_the_report_and_the_results_say_what_each_evaluation_depended_on(first):
    document, results = first
    prints = {s["spec"]: s["fingerprint"] for s in document["specs"]}
    assert len(set(prints.values())) == 3 and all(len(p) == 64 for p in prints.values())
    rows = quality.load(results)["specs"]
    assert {k: v["fingerprint"] for k, v in rows.items()} == prints


def test_a_fingerprint_that_is_not_one_is_not_believed():
    sha = "a" * 64
    rows = quality.parse(json.dumps({"schema": 1, "specs": {
        "png/png": {"sha256": sha, "fingerprint": "not a hash"},
        "png/png-apng": {"sha256": sha, "fingerprint": "b" * 64},
        "png/png-x": {"sha256": sha, "fingerprint": 7}}}))["specs"]
    assert (rows["png/png"]["fingerprint"], rows["png/png-apng"]["fingerprint"], rows["png/png-x"]["fingerprint"]) == (None, "b" * 64, None)


# --- nothing changed

def test_nothing_changed_nothing_is_evaluated_again(fake, capsys, reg, tmp_path, first):
    code, document, results, err = again(capsys, reg, tmp_path, first)
    assert code == 0 and asked_about(fake) == set()  # Fandango was not even asked
    assert reused(document) == {"png", "png-apng", "gif"}
    assert "reused the results of 3 of 3 spec(s)" in err and "evaluated 0" in err
    # ... and what is kept is exactly what was found
    assert quality.load(results)["specs"] == quality.load(first[1])["specs"]


def test_it_says_so_in_the_report(fake, capsys, reg, tmp_path, first):
    _, results = first
    main(["--registry", str(reg), "evaluate", "--all", "-n", "8", "--seed", "1", "--budget", "0", "--reuse", str(results)])
    out = capsys.readouterr().out
    assert "not evaluated again, nothing it depends on has changed" in out and "accepts" in out


# --- something it depends on changed

def test_a_changed_spec_is_evaluated_again_and_the_others_are_not(fake, capsys, reg, tmp_path, first):
    republish(reg, "gif", text="<start> ::= 'gif, changed'\n")
    _, document, _, _ = again(capsys, reg, tmp_path, first)
    assert asked_about(fake) == {"gif"} and reused(document) == {"png", "png-apng"}


def test_what_a_spec_extends_counts_as_part_of_it(fake, capsys, reg, tmp_path, first):
    # png-apng is the same file; png, which it builds on, is not
    republish(reg, "png", text="<start> ::= 'png, changed'\n")
    _, document, _, _ = again(capsys, reg, tmp_path, first)
    assert asked_about(fake) == {"png", "png-apng"} and reused(document) == {"gif"}


def test_a_changed_target_changes_the_specs_it_judges(fake, capsys, reg, tmp_path, first):
    harness = reg / "targets/strict/harness.py"
    harness.write_text(harness.read_text() + "# a stricter test\n")
    _, document, _, _ = again(capsys, reg, tmp_path, first)
    assert asked_about(fake) == {"png", "png-apng"} and reused(document) == {"gif"}


def test_an_upgraded_parser_changes_the_specs_it_judges(fake, capsys, reg, tmp_path, first, parser_version):
    parser_version.write_text("2.0")  # nothing in the registry changed: the parser did
    _, document, _, _ = again(capsys, reg, tmp_path, first)
    assert asked_about(fake) == {"gif"} and reused(document) == {"png", "png-apng"}


def test_a_parser_that_is_not_there_any_more_is_a_change(fake, capsys, reg, tmp_path, first, monkeypatch):
    monkeypatch.setattr("fanbase.fingerprint.unavailable", lambda t: "not installed" if t.name == "lenient" else None)
    _, document, _, _ = again(capsys, reg, tmp_path, first)
    assert reused(document) == {"png", "png-apng"}


@pytest.mark.parametrize("settings", [("--seed", "2"), ("-n", "9"), ("--budget", "50"), ("--coverage",)])
def test_other_settings_change_everything(fake, capsys, reg, tmp_path, first, settings):
    _, results = first
    report = tmp_path / "other.json"
    main(["--registry", str(reg), "evaluate", "--all", "-n", "8", "--seed", "1", "--budget", "0", *settings, "--reuse", str(results),
          "--json-file", str(report)])
    capsys.readouterr()
    assert reused(json.loads(report.read_text())) == set()


# --- what is not a change

def test_what_the_spec_says_to_expect_is_read_again_without_evaluating(fake, capsys, reg, tmp_path, first):
    # decodes is in metadata.yml, not in the spec: the files are the same, but whether they are what the spec says is not
    republish(reg, "png", decodes="always")  # the best parser accepts 75%: that is not always
    code, document, results, _ = again(capsys, reg, tmp_path, first, "--strict")
    assert asked_about(fake) == set() and reused(document) == {"png", "png-apng", "gif"}
    row = quality.load(results)["specs"]["png/png"]
    assert row["decodes"] == "always" and row["expectation_met"] is False
    assert code == 1  # --strict sees it, though nothing was evaluated again


# --- when there is nothing to reuse

@pytest.mark.parametrize("what", ["missing", "garbage"])
def test_results_that_cannot_be_read_mean_a_full_evaluation(fake, capsys, reg, tmp_path, what):
    path = tmp_path / "earlier.json"
    if what == "garbage":
        path.write_text("this is not JSON")
    report = tmp_path / "full.json"
    code = main(["--registry", str(reg), "evaluate", "--all", "-n", "8", "--seed", "1", "--budget", "0", "--reuse", str(path),
                 "--json-file", str(report)])
    err = capsys.readouterr().err
    assert code == 0 and "nothing reused" in err
    assert asked_about(fake) == {"png", "png-apng", "gif"} and reused(json.loads(report.read_text())) == set()
