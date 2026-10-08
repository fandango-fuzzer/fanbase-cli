"""quality.json: what is known of how well a spec does, `quality build`, `list --quality`, `evaluate --compare-with`."""

import copy
import functools
import http.server
import json
import subprocess
import threading

import pytest
import yaml
from conftest import build_registry
from helpers import REAL_RUN, STRICT, FakeFandango, settle, target

from fanbase import compare, contrib, quality
from fanbase.cli import main
from fanbase.registry import RegistryError

SHA_A, SHA_B = "a" * 64, "b" * 64


@pytest.fixture(autouse=True)
def real_subprocess(monkeypatch):
    monkeypatch.setattr(subprocess, "run", REAL_RUN)


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def report(spec="png/png", sha=SHA_A, accepted=100, total=100, version="12.3.0", per_second=15.5, coverage=None, seed=1, name="pillow"):
    """What `fanbase evaluate --json` says of one spec."""
    row = {"name": name, "title": name, "status": "ok", "version": version, "total": total, "accepted": accepted,
           "skipped": 0, "categories": {}, "files_per_second": 100.0, "reasons": []}
    if coverage:
        row["coverage"] = coverage
    return {"schema": 1, "fanbase": "0.5.0", "fandango": "1.3.0", "seed": seed, "count": total,
            "specs": [{"spec": spec, "version": "1.0", "sha256": sha, "generated": {"requested": total, "produced": total, "per_second": per_second},
                       "error": None, "decodes": "always", "targets": [row]}]}


COVERAGE = {"lines_total": 8000, "branches_total": 5000, "seeds": {"files": 6, "lines": 1300, "lines_share": 0.1625},
            "curve": [{"inputs": 1, "lines": 1200, "lines_share": 0.15}, {"inputs": 100, "lines": 1800, "lines_share": 0.225,
                                                                        "branches": 900, "branches_share": 0.18}],
            "only_generated_lines": 700, "only_seed_lines": 200}


# --- making it

def test_a_report_becomes_quality_results():
    doc = quality.build([report()])
    row = doc["specs"]["png/png"]
    assert doc["schema"] == 1 and doc["fanbase"] == "0.5.0" and doc["fandango"] == "1.3.0"
    assert row["sha256"] == SHA_A and row["version"] == "1.0" and row["count"] == 100 and row["seed"] == 1 and row["per_second"] == 15.5
    assert row["targets"] == {"pillow": {"version": "12.3.0", "accepted": 1.0, "files": 100}}
    assert row["best_accepted"] == 1.0 and row["decodes"] == "always" and row["expectation_met"] is True


def test_a_curve_that_is_not_one_is_cut_down_to_what_is():
    doc = quality.build([report(coverage=COVERAGE)])
    doc["specs"]["png/png"]["coverage"]["pillow"] = {"curve": [[1, 0.1], [2], ["x", 0.5], [3, 7], [4, 0.4]] + [[n, 0.5] for n in range(30)]}
    read = quality.parse(json.dumps(doc))["specs"]["png/png"]["coverage"]["pillow"]["curve"]
    assert read[:2] == [[1, 0.1], [4, 0.4]] and len(read) <= 20


def test_the_best_parser_decides_and_the_expectation_is_checked():
    first = report(accepted=40, name="pillow")
    second = report(accepted=100, name="ffmpeg")
    both = {**first, "specs": [{**first["specs"][0], "targets": first["specs"][0]["targets"] + second["specs"][0]["targets"]}]}
    row = quality.build([both])["specs"]["png/png"]
    assert row["best_accepted"] == 1.0 and set(row["targets"]) == {"pillow", "ffmpeg"}
    row = quality.build([report(accepted=40)])["specs"]["png/png"]
    assert row["best_accepted"] == 0.4 and row["expectation_met"] is False  # (it was meant to decode always)


def test_a_run_of_the_parsers_and_a_run_of_the_coverage_are_one_document():
    doc = quality.build([report(), report(accepted=100, name="libpng-cov", version="libpng 1.6.59", coverage=COVERAGE)])
    row = doc["specs"]["png/png"]
    assert set(row["targets"]) == {"pillow", "libpng-cov"}
    assert row["coverage"]["libpng-cov"] == {"version": "libpng 1.6.59", "lines": 0.225, "branches": 0.18, "seeds_lines": 0.1625,
                                             "only_generated": 700, "only_seeds": 200, "lines_total": 8000, "inputs": 100,
                                             "curve": [[1, 0.15], [100, 0.225]]}


def test_results_for_another_version_of_a_spec_replace_the_old_ones():
    doc = quality.build([report(sha=SHA_A), report(sha=SHA_B, name="ffmpeg")])
    row = doc["specs"]["png/png"]
    assert row["sha256"] == SHA_B and set(row["targets"]) == {"ffmpeg"}  # (pillow was measured on the spec as it was)


def test_what_was_known_of_other_specs_stays():
    old = quality.build([report(spec="gif/gif", sha=SHA_B)])
    doc = quality.build([report()], previous=old)
    assert set(doc["specs"]) == {"gif/gif", "png/png"}


def test_a_spec_that_produced_nothing_has_no_results():
    broken = report()
    broken["specs"][0]["error"] = "fandango produced no files"
    assert quality.build([broken])["specs"] == {}


def test_what_is_not_a_report_is_refused():
    with pytest.raises(RegistryError, match="not an evaluation report"):
        quality.build([{"schema": 2, "specs": []}])


# --- reading it

def test_what_is_made_is_read_back():
    doc = quality.build([report(coverage=COVERAGE)])
    assert quality.parse(json.dumps(doc)) == {**doc, "made": doc["made"]}  # (the same, nothing added and nothing lost)


def test_an_evaluation_report_can_be_read_as_the_results():
    assert quality.parse(json.dumps(report()))["specs"]["png/png"]["best_accepted"] == 1.0


@pytest.mark.parametrize("change, complaint", [
    (lambda d: d.update(schema=2), "schema 1"),
    (lambda d: d.update(specs="x"), "schema 1"),
    (lambda d: d["specs"].update({"png": d["specs"]["png/png"]}), "not a spec"),
    (lambda d: d["specs"].update({"../png/png": d["specs"]["png/png"]}), "not a spec"),
    (lambda d: d["specs"]["png/png"].pop("sha256"), "do not say which file"),
    (lambda d: d["specs"]["png/png"].update(sha256="short"), "do not say which file"),
])
def test_results_that_do_not_fit_are_refused(change, complaint):
    doc = quality.build([report()])
    change(doc)
    with pytest.raises(RegistryError, match=complaint):
        quality.parse(json.dumps(doc))


def test_only_what_is_known_is_kept_as_numbers_and_cleaned_text():
    doc = quality.build([report()])
    row = doc["specs"]["png/png"]
    row["best_accepted"] = 7  # more than all of them
    row["per_second"] = "fast"
    row["decodes"] = "sometimes"
    row["version"] = "1.0\x1b[31m red \x00"
    row["evil"] = {"do": "this"}
    row["targets"]["pillow"]["version"] = "\x1b]0;title\x07 12.3"
    row["targets"]["../x"] = {"accepted": 1.0}
    row["targets"]["ok"] = {"accepted": "all", "files": -3}
    read = quality.parse(json.dumps(doc))["specs"]["png/png"]
    assert read["best_accepted"] is None and read["per_second"] is None and read["decodes"] is None
    assert "\x1b" not in read["version"] and "\x00" not in read["version"] and "evil" not in read
    assert "\x1b" not in read["targets"]["pillow"]["version"] and "../x" not in read["targets"]
    assert read["targets"]["ok"] == {"version": None, "accepted": None, "files": None}


def test_nonsense_is_refused():
    for bad in ("", "[]", "not json", '"x"', "{}"):
        with pytest.raises(RegistryError):
            quality.parse(bad)
    with pytest.raises(RegistryError, match="far too large"):
        quality.parse(b"0" * (quality.MAX_BYTES + 1))


@pytest.fixture
def served(tmp_path):
    (tmp_path / "quality.json").write_text(json.dumps(quality.build([report()])))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_loaded_from_a_path_or_a_url(tmp_path, served):
    (tmp_path / "mine.json").write_text(json.dumps(quality.build([report()])))
    assert "png/png" in quality.load(tmp_path / "mine.json")["specs"]
    assert "png/png" in quality.load(f"{served}/quality.json")["specs"]


def test_plain_http_is_for_this_machine_only():
    with pytest.raises(RegistryError, match="https, not plain http"):
        quality.load("http://example.org/quality.json")


def test_what_is_not_there(tmp_path, served):
    with pytest.raises(RegistryError):
        quality.load(tmp_path / "nope.json")
    with pytest.raises(RegistryError, match="HTTP 404"):
        quality.load(f"{served}/nothing.json")


# --- where a registry keeps it

class Stub:
    def __init__(self, info=None, url=None):
        self.info, self.url = info or {}, url


def test_a_registry_can_say_where():
    assert quality.default_source(Stub({"quality": " https://example.org/q.json "})) == "https://example.org/q.json"


@pytest.mark.parametrize("url", ["https://github.com/fandango-fuzzer/fanbase", "https://github.com/fandango-fuzzer/fanbase/",
                                 "https://github.com/fandango-fuzzer/fanbase.git", "https://github.com/fandango-fuzzer/fanbase/tree/main"])
def test_a_github_registry_keeps_it_with_its_latest_release(url):
    assert quality.default_source(Stub(url=url)) == "https://github.com/fandango-fuzzer/fanbase/releases/latest/download/quality.json"


def test_another_host_says_nothing():
    assert quality.default_source(Stub(url="https://example.org/registry")) is None


def test_a_checkout_keeps_it_in_its_root(tmp_path):
    reg = build_registry(tmp_path / "reg", {("png", "png"): dict(version="1.0")})
    from fanbase.registry import Registry

    assert quality.default_source(Registry(reg)) is None
    (reg / "quality.json").write_text("{}")
    assert quality.default_source(Registry(reg)) == str(reg / "quality.json")


def test_a_registry_yml_that_says_something_else_than_text_is_refused(tmp_path):
    reg = build_registry(tmp_path / "reg", {("png", "png"): dict(version="1.0")})
    (reg / "registry.yml").write_text(yaml.safe_dump({"quality": 7}))
    code, _, err = run_main(reg, "list")
    assert code == 2 and "quality is where the registry keeps" in err


def run_main(reg, *argv):
    import contextlib
    import io

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = main(["--registry", str(reg), *argv])
    return code, "", err.getvalue()


# --- showing it

def test_in_words():
    row = quality.build([report(), report(name="libpng-cov", coverage=COVERAGE)])["specs"]["png/png"]
    assert quality.summary(row) == "accepts 100%, covers 22.5%, 16/s"
    assert quality.summary(row, current=False).endswith("(for an earlier version)")
    assert quality.summary({"targets": {}, "coverage": {}}) == "measured"


@pytest.fixture
def reg(tmp_path):
    return build_registry(tmp_path / "reg", {("png", "png"): dict(version="1.0", description="PNG"),
                                              ("png", "png-apng"): dict(version="1.0", description="Animated")})


def sha_of(reg, kind):
    from fanbase.manager import entry_sha
    from fanbase.registry import Registry

    r = Registry(reg)
    return entry_sha(r, r.resolve(kind))


@pytest.fixture
def results(tmp_path, reg):
    doc = quality.build([report(spec="png/png", sha=sha_of(reg, "png")),
                         report(spec="png/png-apng", sha=SHA_B, accepted=50, per_second=3.0)])  # (png-apng has changed since)
    path = tmp_path / "results.json"
    path.write_text(json.dumps(doc))
    return path


def test_list_shows_how_each_spec_does(capsys, reg, results):
    code, out, _ = run(capsys, "--registry", str(reg), "list", "png", "--quality", str(results))
    assert code == 0
    assert "PNG [accepts 100%, 16/s]" in out
    assert "Animated [accepts 50%, 3/s (for an earlier version)]" in out  # not measured on the spec as it is


def test_list_in_json(capsys, reg, results):
    _, out, _ = run(capsys, "--registry", str(reg), "list", "png", "--quality", str(results), "--json")
    rows = {r["kind"]: r for r in json.loads(out)}
    assert rows["png"]["quality_current"] is True and rows["png"]["quality"]["best_accepted"] == 1.0
    assert rows["png-apng"]["quality_current"] is False


def test_show_says_how_it_does(capsys, reg, results):
    code, out, _ = run(capsys, "--registry", str(reg), "show", "png", "--quality", str(results))
    assert code == 0 and "# quality: accepts 100%, 16/s" in out and "quality:" in out
    _, out, _ = run(capsys, "--registry", str(reg), "show", "png", "--quality", str(results), "--json")
    assert json.loads(out)["quality"]["current"] is True


def test_without_the_flag_nothing_is_fetched(capsys, reg, results):
    (reg / "quality.json").write_text(results.read_text())
    _, out, _ = run(capsys, "--registry", str(reg), "list", "png")
    assert "accepts" not in out


def test_the_flag_alone_finds_the_registrys_own(capsys, reg, results):
    (reg / "quality.json").write_text(results.read_text())
    _, out, _ = run(capsys, "--registry", str(reg), "list", "png", "--quality")
    assert "accepts 100%" in out


def test_no_results_is_said_and_the_list_goes_on(capsys, reg, tmp_path):
    code, out, err = run(capsys, "--registry", str(reg), "list", "png", "--quality")
    assert code == 0 and "PNG" in out and "this registry does not say where it keeps them" in err
    code, out, err = run(capsys, "--registry", str(reg), "list", "png", "--quality", str(tmp_path / "nope.json"))
    assert code == 0 and "PNG" in out and "no quality results" in err


def test_results_that_do_not_cover_a_spec_say_so(capsys, reg, tmp_path):
    other = tmp_path / "other.json"
    other.write_text(json.dumps(quality.build([report(spec="gif/gif")])))
    _, out, _ = run(capsys, "--registry", str(reg), "list", "png", "--quality", str(other))
    assert "the quality results do not cover these specs" in out
    _, out, _ = run(capsys, "--registry", str(reg), "show", "png", "--quality", str(other))
    assert "# quality: the results do not cover this spec" in out


# --- quality build

def test_quality_build_from_reports(capsys, tmp_path):
    (tmp_path / "parsers.json").write_text(json.dumps(report()))
    (tmp_path / "coverage.json").write_text(json.dumps(report(name="libpng-cov", coverage=COVERAGE)))
    out = tmp_path / "quality.json"
    code, _, err = run(capsys, "quality", "build", str(tmp_path / "parsers.json"), str(tmp_path / "coverage.json"), "-o", str(out))
    assert code == 0 and "1 spec(s)" in err
    assert set(json.loads(out.read_text())["specs"]["png/png"]["coverage"]) == {"libpng-cov"}


def test_quality_build_adds_to_what_was_known(capsys, tmp_path):
    (tmp_path / "old.json").write_text(json.dumps(quality.build([report(spec="gif/gif", sha=SHA_B)])))
    (tmp_path / "new.json").write_text(json.dumps(report()))
    run(capsys, "quality", "build", str(tmp_path / "new.json"), "--add-to", str(tmp_path / "old.json"), "-o", str(tmp_path / "q.json"))
    assert set(json.loads((tmp_path / "q.json").read_text())["specs"]) == {"gif/gif", "png/png"}


def test_quality_build_to_the_screen_and_from_nonsense(capsys, tmp_path):
    (tmp_path / "r.json").write_text(json.dumps(report()))
    code, out, _ = run(capsys, "quality", "build", str(tmp_path / "r.json"), "-o", "-")
    assert code == 0 and json.loads(out)["schema"] == 1
    (tmp_path / "bad.json").write_text("nonsense")
    code, _, err = run(capsys, "quality", "build", str(tmp_path / "bad.json"))
    assert code == 2 and "not a report of `fanbase evaluate --json-file`" in err
    code, _, err = run(capsys, "quality", "build", str(tmp_path / "missing.json"))
    assert code == 2


# --- comparing

def docs(**changes):
    old = quality.build([report(coverage=None), report(name="libpng-cov", version="libpng 1.6.59", coverage=COVERAGE)])
    new = copy.deepcopy(old)
    return old, new


def row(doc):
    return doc["specs"]["png/png"]


def test_nothing_differing_is_nothing():
    old, new = docs()
    assert compare.compare(old, new) == []
    assert "nothing differs" in compare.as_text(old, [])


def test_a_parser_that_accepts_less_with_the_same_spec_is_worse():
    old, new = docs()
    row(new)["targets"]["pillow"]["accepted"] = 0.9
    found, = compare.compare(old, new)
    assert (found.kind, found.spec) == ("worse", "png/png")
    assert "pillow accepts 100% -> 90%" in found.text and "nothing changed that is known" in found.text and "pillow is the same" in found.text


def test_a_small_move_is_not_worth_saying():
    old, new = docs()
    row(new)["targets"]["pillow"]["accepted"] = 0.97
    assert compare.compare(old, new) == []


def test_a_parser_that_was_updated_is_not_the_specs_fault():
    old, new = docs()
    row(new)["targets"]["pillow"].update(accepted=0.5, version="12.4.0")
    found, = compare.compare(old, new)
    assert found.kind == "note" and "pillow 12.3.0 -> 12.4.0" in found.text


def test_a_spec_that_changed_and_accepts_less_is_worse_and_says_why():
    old, new = docs()
    row(new).update(sha256=SHA_B, version="1.1")
    row(new)["targets"]["pillow"]["accepted"] = 0.8
    kinds = {d.kind: d.text for d in compare.compare(old, new)}
    assert "the spec changed (1.0 -> 1.1)" in kinds["note"] and "the spec changed; pillow is the same" in kinds["worse"]


def test_better_is_said_too():
    old, new = docs()
    row(new)["targets"]["pillow"]["accepted"] = 0.5
    row(old)["targets"]["pillow"]["accepted"] = 0.2
    assert [d.kind for d in compare.compare(old, new)] == ["better"]


def test_coverage_that_drops_is_worse():
    old, new = docs()
    row(new)["coverage"]["libpng-cov"]["lines"] = 0.2
    found, = compare.compare(old, new)
    assert found.kind == "worse" and "libpng-cov covers 22.5% -> 20.0%" in found.text
    row(new)["coverage"]["libpng-cov"]["lines"] = 0.215
    assert compare.compare(old, new) == []


def test_speed_is_told_only_when_it_moves_a_lot_and_is_never_worse():
    old, new = docs()
    row(new)["per_second"] = 12.0
    assert compare.compare(old, new) == []
    row(new)["per_second"] = 3.0
    found, = compare.compare(old, new)
    assert found.kind == "note" and "16/s -> 3/s" in found.text


def test_new_and_gone():
    old, new = docs()
    new["specs"]["gif/gif"] = copy.deepcopy(row(new))
    del new["specs"]["png/png"]
    kinds = {(d.spec, d.text) for d in compare.compare(old, new)}
    assert ("gif/gif", "new: not in the earlier results") in kinds and ("png/png", "not in these results any more") in kinds


def test_a_target_that_appears_or_goes():
    old, new = docs()
    row(new)["targets"]["ffmpeg"] = {"version": "7.1", "accepted": 0.9, "files": 100}
    del row(new)["targets"]["pillow"]
    texts = [d.text for d in compare.compare(old, new)]
    assert "ffmpeg: new, accepts 90%" in texts and "pillow: no longer measured" in texts


def test_worst_first_and_in_markdown():
    old, new = docs()
    row(new)["targets"]["pillow"]["accepted"] = 0.5
    new["specs"]["gif/gif"] = copy.deepcopy(row(new))
    found = compare.compare(old, new)
    assert [d.kind for d in found] == ["worse", "note"]
    md = compare.as_markdown(old, found)
    assert "⚠️ worse | `png/png`" in md and "ℹ️ | `gif/gif`" in md


# --- evaluate --compare-with

@pytest.fixture
def evaluated(tmp_path):
    root = build_registry(tmp_path / "ev", {("png", "png"): dict(version="1.0", description="PNG")})
    target(root, "strict", STRICT, version=["{python}", "-c", "print('strict 1')"])
    (root / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["strict"]}))
    settle(root)
    return root


def fake_fandango(monkeypatch, bad_every):
    made = FakeFandango(content=lambda i: (b"bad input " if bad_every and i % bad_every == 0 else b"good input ") + str(i).encode())
    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr("fanbase.evaluate.find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", made)


def test_a_run_is_compared_with_an_earlier_one(capsys, evaluated, tmp_path, monkeypatch):
    fake_fandango(monkeypatch, bad_every=0)
    assert run(capsys, "--registry", str(evaluated), "evaluate", "png", "-n", "20", "--json-file", str(tmp_path / "before.json"))[0] == 0
    fake_fandango(monkeypatch, bad_every=2)  # half of the files are now bad
    code, out, _ = run(capsys, "--registry", str(evaluated), "evaluate", "png", "-n", "20", "--compare-with", str(tmp_path / "before.json"))
    assert code == 0 and "compared with the earlier results" in out
    assert "worse  png/png  strict accepts 100% -> 50%" in out and "strict is the same" in out


def test_in_json_and_in_markdown(capsys, evaluated, tmp_path, monkeypatch):
    fake_fandango(monkeypatch, bad_every=0)
    run(capsys, "--registry", str(evaluated), "evaluate", "png", "-n", "20", "--json-file", str(tmp_path / "before.json"))
    fake_fandango(monkeypatch, bad_every=2)
    _, out, _ = run(capsys, "--registry", str(evaluated), "evaluate", "png", "-n", "20", "--compare-with", str(tmp_path / "before.json"), "--json")
    comparison = json.loads(out)["comparison"]
    assert comparison["differences"][0]["kind"] == "worse" and comparison["fanbase"]
    _, out, _ = run(capsys, "--registry", str(evaluated), "evaluate", "png", "-n", "20", "--compare-with", str(tmp_path / "before.json"), "--markdown")
    assert "## Compared with the earlier results" in out and "⚠️ worse" in out


def test_a_worse_run_can_fail(capsys, evaluated, tmp_path, monkeypatch):
    fake_fandango(monkeypatch, bad_every=0)
    run(capsys, "--registry", str(evaluated), "evaluate", "png", "-n", "20", "--json-file", str(tmp_path / "before.json"))
    assert run(capsys, "--registry", str(evaluated), "evaluate", "png", "-n", "20", "--compare-with", str(tmp_path / "before.json"), "--fail-on-worse")[0] == 0
    fake_fandango(monkeypatch, bad_every=2)
    assert run(capsys, "--registry", str(evaluated), "evaluate", "png", "-n", "20", "--compare-with", str(tmp_path / "before.json"), "--fail-on-worse")[0] == 1


def test_nothing_to_compare_with_is_only_said(capsys, evaluated, tmp_path, monkeypatch):
    fake_fandango(monkeypatch, bad_every=0)
    code, out, err = run(capsys, "--registry", str(evaluated), "evaluate", "png", "-n", "4", "--compare-with", str(tmp_path / "nope.json"), "--fail-on-worse")
    assert code == 0 and "no comparison:" in err and "png/png" in out


def test_the_results_can_be_made_from_the_run_itself(capsys, evaluated, tmp_path, monkeypatch):
    fake_fandango(monkeypatch, bad_every=0)
    run(capsys, "--registry", str(evaluated), "evaluate", "png", "-n", "20", "--json-file", str(tmp_path / "r.json"))
    run(capsys, "quality", "build", str(tmp_path / "r.json"), "-o", str(evaluated / "quality.json"))
    _, out, _ = run(capsys, "--registry", str(evaluated), "list", "png", "--quality")
    assert "[accepts 100%" in out
