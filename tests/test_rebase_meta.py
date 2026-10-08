"""`fanbase rebase` and the metadata a fork keeps from its original: extends, fandango, pip, extensions."""

import os
import subprocess
import sys

import pytest
import yaml
from conftest import build_registry, republish

from fanbase import contrib, rebase as rebase_module
from fanbase.cli import main

REAL_RUN = subprocess.run


@pytest.fixture(autouse=True)
def real_git(monkeypatch):
    monkeypatch.setattr(subprocess, "run", REAL_RUN)
    monkeypatch.setattr(contrib, "git_user", lambda: "Ada Lovelace")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def meta(reg, kind):
    return yaml.safe_load((reg / "specs" / kind.split("-")[0] / kind / "metadata.yml").read_text())


def set_meta(reg, kind, **values):
    """What the person does to the metadata.yml of a spec of theirs."""
    path = reg / "specs" / kind.split("-")[0] / kind / "metadata.yml"
    data = yaml.safe_load(path.read_text())
    for key, value in values.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    run_quiet(reg)


def run_quiet(reg):
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["--registry", str(reg), "reindex"]) == 0


def fan_text(reg, kind, lines=None):
    body = "\n".join((lines or {}).get(i, f"# line {i}") for i in range(1, 11))
    (reg / "specs" / kind.split("-")[0] / kind / f"{kind}.fan").write_text(body + "\n<start> ::= 'png'\n")
    republish(reg, kind)


@pytest.fixture
def up(tmp_path):
    """The registry the original is in: png builds on png-base."""
    reg = build_registry(tmp_path / "up", {
        ("png", "png-base"): dict(version="1.0", description="base"),
        ("png", "png-extra"): dict(version="1.0", description="extra"),
        ("png", "png-more"): dict(version="1.0", description="more"),
        ("png", "png"): dict(version="1.0", description="PNG", extends=["png-base"], extensions=["png"], pip=["pillow"]),
    })
    fan_text(reg, "png")
    return reg


@pytest.fixture
def mine(tmp_path):
    reg = build_registry(tmp_path / "mine", {("png", "png-local"): dict(version="1.0", description="mine")}, name="mine")
    return reg


@pytest.fixture
def forked(capsys, up, mine):
    assert run(capsys, "--registry", str(up), "fork", "png", "--as", "png-mine", "--into", str(mine))[0] == 0
    return mine


def rebase(capsys, mine, up, *extra):
    return run(capsys, "--registry", str(mine), "rebase", "png-mine", "--upstream", str(up), *extra)


def original_changes(up, **values):
    """The original's maintainers change its metadata, and its file a little."""
    fan_text(up, "png", {9: "# line 9, theirs"})
    set_meta(up, "png", version="1.1", **values)


# --- what the fork keeps

def test_a_fork_keeps_what_its_original_said(forked):
    assert meta(forked, "png-mine")["derived_meta"] == {
        "extends": ["png-base"], "fandango": ">=1.3", "pip": ["pillow"], "extensions": ["png"]}
    assert meta(forked, "png-mine")["extends"] == ["fanbase:png-base"]  # (in the fork's registry, the original's `png-base`)


def test_a_malformed_record_is_refused(capsys, forked):
    set_meta_raw = forked / "specs/png/png-mine/metadata.yml"
    data = yaml.safe_load(set_meta_raw.read_text())
    data["derived_meta"] = {"extends": "png-base", "license": "MIT"}
    set_meta_raw.write_text(yaml.safe_dump(data, sort_keys=False))
    code, _, err = run(capsys, "--registry", str(forked), "reindex")
    assert code == 2 and "derived_meta" in err


# --- lists: both sides' changes are kept

def test_the_originals_additions_and_removals_are_applied_to_the_forks_own_list(capsys, up, forked):
    set_meta(forked, "png-mine", extends=["fanbase:png-base", "png-local"], extensions=["png", "apng"])  # the fork's own additions
    original_changes(up, extends=["png-extra"], extensions=["png", "mng"])  # base dropped, extra added; apng-like added
    code, out, _ = rebase(capsys, forked, up, "--metadata", "adopt")
    assert code == 0
    m = meta(forked, "png-mine")
    assert m["extends"] == ["png-local", "fanbase:png-extra"]  # base dropped, extra added, the fork's own kept
    assert m["extensions"] == ["png", "apng", "mng"]
    assert "adopted from the original: extends, extensions" in out
    assert m["derived_meta"]["extends"] == ["png-extra"] and m["derived_from"] == "fanbase:png/png@1.1"


def test_a_list_the_fork_already_has_is_not_doubled(capsys, up, forked):
    set_meta(forked, "png-mine", pip=["pillow", "numpy"])
    original_changes(up, pip=["pillow", "numpy"])  # the original added what the fork added too
    rebase(capsys, forked, up, "--metadata", "adopt")
    assert meta(forked, "png-mine")["pip"] == ["pillow", "numpy"]


def test_a_list_can_end_up_empty_and_the_key_goes(capsys, up, forked):
    original_changes(up, pip=None)
    rebase(capsys, forked, up, "--metadata", "adopt")
    assert "pip" not in meta(forked, "png-mine")


# --- values: taken if only the original changed it

def test_a_value_only_the_original_changed_is_taken(capsys, up, forked):
    original_changes(up, fandango=">=1.4")
    code, out, _ = rebase(capsys, forked, up, "--metadata", "adopt")
    assert code == 0 and meta(forked, "png-mine")["fandango"] == ">=1.4"
    assert "fandango: the original changed it from '>=1.3' to '>=1.4', and you did not" in out


def test_a_value_both_changed_is_a_conflict_and_is_left_alone(capsys, up, forked):
    set_meta(forked, "png-mine", fandango=">=1.5")
    original_changes(up, fandango=">=1.4")
    code, out, _ = rebase(capsys, forked, up, "--metadata", "adopt")
    assert code == 0 and meta(forked, "png-mine")["fandango"] == ">=1.5"
    assert "fandango: the original changed it from '>=1.3' to '>=1.4' and the fork has '>=1.5'" in out and "a conflict: not touched" in out


def test_a_value_both_changed_the_same_way_is_nothing(capsys, up, forked):
    set_meta(forked, "png-mine", fandango=">=1.4")
    original_changes(up, fandango=">=1.4")
    code, out, _ = rebase(capsys, forked, up, "--metadata", "adopt")
    assert code == 0 and "fandango" not in out.replace("fandango.fan", "")


# --- who decides

def test_not_at_a_terminal_nothing_is_changed_and_it_is_said_how_to_take_it(capsys, up, forked, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    original_changes(up, fandango=">=1.4")
    code, out, _ = rebase(capsys, forked, up)
    assert code == 0 and meta(forked, "png-mine")["fandango"] == ">=1.3"
    assert "not adopted yet" in out and "--metadata adopt" in out
    assert meta(forked, "png-mine")["derived_meta"]["fandango"] == ">=1.3"  # still what it was: the offer stands
    assert "line 9, theirs" in (forked / "specs/png/png-mine/png-mine.fan").read_text()  # (the file was merged all the same)


def test_and_the_offer_can_be_taken_later_although_the_file_is_up_to_date(capsys, up, forked, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    original_changes(up, fandango=">=1.4")
    rebase(capsys, forked, up)
    code, out, _ = rebase(capsys, forked, up, "--metadata", "adopt")
    assert code == 0 and "only the metadata differs" in out and meta(forked, "png-mine")["fandango"] == ">=1.4"
    code, out, _ = rebase(capsys, forked, up)
    assert code == 0 and "is up to date" in out  # and now there is nothing more to say


def test_at_a_terminal_it_asks_and_yes_takes_it(capsys, up, forked, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    asked = []
    monkeypatch.setattr(rebase_module, "_ask", lambda question: asked.append(question) or True)
    original_changes(up, fandango=">=1.4")
    rebase(capsys, forked, up)
    assert len(asked) == 1 and "metadata.yml" in asked[0] and meta(forked, "png-mine")["fandango"] == ">=1.4"


def test_at_a_terminal_no_leaves_it_and_does_not_ask_again(capsys, up, forked, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(rebase_module, "_ask", lambda question: False)
    original_changes(up, fandango=">=1.4")
    rebase(capsys, forked, up)
    assert meta(forked, "png-mine")["fandango"] == ">=1.3" and meta(forked, "png-mine")["derived_meta"]["fandango"] == ">=1.4"
    code, out, _ = rebase(capsys, forked, up)
    assert code == 0 and "is up to date" in out


def test_keep_leaves_the_metadata_alone_and_does_not_offer_it_again(capsys, up, forked):
    original_changes(up, fandango=">=1.4")
    code, out, _ = rebase(capsys, forked, up, "--metadata", "keep")
    assert code == 0 and meta(forked, "png-mine")["fandango"] == ">=1.3" and "left as they are" in out
    assert "is up to date" in rebase(capsys, forked, up)[1]


def test_a_dry_run_writes_nothing(capsys, up, forked):
    original_changes(up, fandango=">=1.4", extends=["png-extra"])
    folder = forked / "specs/png/png-mine"
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    code, out, _ = rebase(capsys, forked, up, "--metadata", "adopt", "--dry-run")
    assert code == 0 and "dry run" in out and "metadata: fandango" in out and "metadata: extends" in out
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before


# --- forks that did not record the original

def test_the_originals_value_can_be_taken_for_a_fork_that_did_not_record_it(capsys, up, forked):
    set_meta(forked, "png-mine", derived_meta=None)  # as a fork made by an earlier fanbase is
    original_changes(up, fandango=">=1.4", extends=["png-extra"])
    code, out, _ = rebase(capsys, forked, up, "--adopt", "fandango")
    assert code == 0 and meta(forked, "png-mine")["fandango"] == ">=1.4"
    assert meta(forked, "png-mine")["extends"] == ["fanbase:png-base"]  # (only what was asked for)
    assert "note: the original now extends ['fanbase:png-extra']" in out  # (and the rest is told, as before)
    assert meta(forked, "png-mine")["derived_meta"]["extends"] == ["png-extra"]  # from now on it is recorded


def test_adopt_takes_the_originals_whole_list(capsys, up, forked):
    set_meta(forked, "png-mine", extends=["png-local"])
    original_changes(up, extends=["png-extra", "png-more"])
    rebase(capsys, forked, up, "--adopt", "extends")
    assert meta(forked, "png-mine")["extends"] == ["fanbase:png-extra", "fanbase:png-more"]


def test_adopt_knows_its_keys(capsys, up, forked):
    code, _, err = rebase(capsys, forked, up, "--adopt", "license")
    assert code == 2 and "--adopt takes some of extends, fandango, pip, extensions, not license" in err


# --- safety

def test_a_failure_while_writing_changes_nothing(capsys, up, forked, monkeypatch):
    original_changes(up, fandango=">=1.4")
    folder = forked / "specs/png/png-mine"
    before = {p.name: p.read_bytes() for p in folder.iterdir()}

    def fail(registry):
        raise contrib.RegistryError("the registry would not be in order")

    monkeypatch.setattr(rebase_module, "refresh", fail)
    code, _, err = rebase(capsys, forked, up, "--metadata", "adopt")
    assert code == 2 and "would not be in order; nothing was changed" in err
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before


def test_metadata_of_a_fork_in_the_registry_it_was_made_from(capsys, up):
    run(capsys, "--registry", str(up), "fork", "png", "--as", "png-here")
    original_changes(up, extends=["png-base", "png-extra"])
    code, out, _ = run(capsys, "--registry", str(up), "rebase", "png-here", "--metadata", "adopt")
    assert code == 0 and meta(up, "png-here")["extends"] == ["png-base", "png-extra"]  # (the same registry: no prefix)


def test_a_dependency_the_fork_cannot_name_is_said_and_not_added(capsys, tmp_path, mine):
    acme = build_registry(tmp_path / "acme", {
        ("png", "png-a"): dict(version="1.0"), ("png", "png-b"): dict(version="1.0"),
        ("png", "png"): dict(version="1.0", extends=["png-a"]),
    }, name="acme")
    run(capsys, "--registry", str(acme), "fork", "png", "--as", "png-mine", "--into", str(mine), "--with-deps")
    republish(acme, "png", version="1.1", extends=["png-a", "png-b"])
    code, out, _ = run(capsys, "--registry", str(mine), "rebase", "png-mine", "--upstream", str(acme), "--metadata", "adopt")
    assert code == 0 and "cannot extend" in out  # acme's png-b is not something a spec of `mine` can extend
    assert meta(mine, "png-mine")["extends"] == ["png-a"]  # (the copy of png-a, which --with-deps made)


def test_with_deps_copies_record_what_their_originals_said(capsys, up, mine):
    run(capsys, "--registry", str(up), "fork", "png", "--as", "png-mine", "--into", str(mine), "--with-deps")
    assert meta(mine, "png-base")["derived_meta"] == {"fandango": ">=1.3"}
    assert meta(mine, "png-mine")["extends"] == ["png-base"]  # (the copy)
    assert meta(mine, "png-mine")["derived_meta"]["extends"] == ["png-base"]  # (what the original said, in its own words)
