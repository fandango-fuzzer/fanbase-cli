"""`fanbase fork --with-deps`: a spec and what it extends, copied together."""

import pytest
import yaml
from conftest import build_registry, republish

from fanbase import contrib
from fanbase.cli import main
from fanbase.registry import RegistryError


@pytest.fixture(autouse=True)
def who(monkeypatch):
    monkeypatch.setattr(contrib, "git_user", lambda: "Ada Lovelace")


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def meta(reg, kind):
    return yaml.safe_load((reg / "specs" / kind.split("-")[0] / kind / "metadata.yml").read_text())


def text(reg, kind):
    return (reg / "specs" / kind.split("-")[0] / kind / f"{kind}.fan").read_text()


def write_text(reg, kind, content):
    (reg / "specs" / kind.split("-")[0] / kind / f"{kind}.fan").write_text(content, newline="\n")
    republish(reg, kind)


def kinds(reg):
    return sorted(p.name for p in (reg / "specs/png").iterdir()) if (reg / "specs/png").exists() else []


def in_order(capsys, reg):
    return run(capsys, "--registry", str(reg), "reindex", "--check")[0] == 0


@pytest.fixture
def reg(tmp_path):
    """The public registry: png-anim builds on png-apng, which builds on png."""
    root = build_registry(tmp_path / "reg", {
        ("png", "png"): dict(version="1.0", extensions=["png"], description="Valid PNGs", authors=["Grace Hopper"]),
        ("png", "png-apng"): dict(version="1.2", extensions=["png"], description="Animated", extends=["png"]),
        ("png", "png-anim"): dict(version="1.0", extensions=["png"], description="Longer", extends=["png-apng>=1.0,<2"]),
        ("gif", "gif"): dict(version="1.0", description="GIF"),
    })
    write_text(root, "png-apng", 'include("png/png.fan")\n<start> ::= <png>\n')
    write_text(root, "png-anim", 'include("png/png-apng.fan")\n<start> ::= <png>\n')
    return root


@pytest.fixture
def mine(tmp_path):
    return build_registry(tmp_path / "mine", {("gif", "gif"): dict(version="1.0")}, name="mine")


@pytest.fixture
def acme(tmp_path):
    """Someone's own registry: png-top builds on png-mid, which builds on png-base."""
    root = build_registry(tmp_path / "acme", {
        ("png", "png-base"): dict(version="1.0", extensions=["png"], authors=["Bob"]),
        ("png", "png-mid"): dict(version="1.1", extensions=["png"], extends=["png-base"], authors=["Bob"]),
        ("png", "png-top"): dict(version="2.0", extensions=["png"], extends=["png-mid>=1.0"], authors=["Bob"]),
    }, name="acme")
    write_text(root, "png-mid", 'include("acme/png/png-base.fan")\n<start> ::= <base>\n')
    write_text(root, "png-top", 'include("acme/png/png-mid.fan")\n<start> ::= <mid>\n')
    return root


def added(capsys, reg, acme):
    assert run(capsys, "--registry", str(reg), "registry", "add", str(acme), "--trust")[0] == 0


# --- from the public registry into one of your own

def test_what_it_extends_is_copied_and_the_copies_find_each_other(capsys, reg, mine):
    code, out, err = run(capsys, "--registry", str(reg), "fork", "png-apng", "--as", "png-apng-mine", "--into", str(mine), "--with-deps")
    assert code == 0 and err == ""
    assert "forked fanbase:png/png into specs/png/png/" in out and "forked fanbase:png/png-apng into specs/png/png-apng-mine/" in out
    assert kinds(mine) == ["png", "png-apng-mine"]
    assert meta(mine, "png-apng-mine")["extends"] == ["png"]  # the copy of png, not fanbase:png
    assert text(mine, "png-apng-mine") == 'include("mine/png/png.fan")\n<start> ::= <png>\n'  # which is the one it includes
    assert text(mine, "png") == text(reg, "png")  # a copy that needs no change is the bytes of the original
    png = meta(mine, "png")
    assert (png["derived_from"], png["version"], png["status"], png["authors"]) == ("fanbase:png/png@1.0", "0.1", "draft", ["Grace Hopper", "Ada Lovelace"])
    assert meta(mine, "png-apng-mine")["derived_from"] == "fanbase:png/png-apng@1.2"
    assert in_order(capsys, mine)


def test_without_the_flag_only_the_spec_is_copied(capsys, reg, mine):
    run(capsys, "--registry", str(reg), "fork", "png-apng", "--as", "png-apng-mine", "--into", str(mine))
    assert kinds(mine) == ["png-apng-mine"] and meta(mine, "png-apng-mine")["extends"] == ["fanbase:png"]
    assert text(mine, "png-apng-mine") == 'include("png/png.fan")\n<start> ::= <png>\n'  # still the public registry's png


def test_a_chain_is_copied_to_the_end_and_a_version_range_is_dropped(capsys, reg, mine):
    code, out, _ = run(capsys, "--registry", str(reg), "fork", "png-anim", "--as", "png-anim-mine", "--into", str(mine), "--with-deps")
    assert code == 0 and kinds(mine) == ["png", "png-anim-mine", "png-apng"]
    # the originals asked for png-apng>=1.0,<2; the copy is at 0.1, and is what is meant
    assert meta(mine, "png-anim-mine")["extends"] == ["png-apng"] and meta(mine, "png-apng")["extends"] == ["png"]
    assert text(mine, "png-anim-mine") == 'include("mine/png/png-apng.fan")\n<start> ::= <png>\n'
    assert text(mine, "png-apng") == 'include("mine/png/png.fan")\n<start> ::= <png>\n'
    assert out.index("specs/png/png/") < out.index("specs/png/png-apng/") < out.index("specs/png/png-anim-mine/")  # what it needs first
    assert in_order(capsys, mine)


def test_a_dependency_keeps_its_name_but_not_a_version_it_does_not_have(capsys, reg, mine):
    run(capsys, "--registry", str(reg), "fork", "png-anim", "--as", "png-anim-mine", "--into", str(mine), "--with-deps")
    assert meta(mine, "png-apng")["version"] == "0.1" and meta(mine, "png-apng")["derived_from"] == "fanbase:png/png-apng@1.2"


def test_a_spec_that_extends_nothing_has_nothing_to_bring(capsys, reg, mine):
    code, out, _ = run(capsys, "--registry", str(reg), "fork", "gif", "--as", "gif-mine", "--into", str(mine), "--with-deps")
    assert code == 0 and "nothing else to copy" in out and (mine / "specs/gif/gif-mine").is_dir()


def test_inside_the_same_registry_what_it_extends_is_there_already(capsys, reg):
    code, out, _ = run(capsys, "--registry", str(reg), "fork", "png-apng", "--as", "png-apng-better", "--with-deps")
    assert code == 0 and "nothing else to copy" in out
    assert kinds(reg) == ["png", "png-anim", "png-apng", "png-apng-better"]
    assert meta(reg, "png-apng-better")["extends"] == ["png"]


# --- from a registry you added

def test_a_spec_of_a_third_registry_can_be_moved_with_what_it_extends(capsys, reg, acme, mine):
    added(capsys, reg, acme)
    code, out, err = run(capsys, "--registry", str(reg), "fork", "acme:png-top", "--as", "png-top-mine", "--into", str(mine), "--with-deps")
    assert code == 0 and err == ""  # (without it: "fork what it extends first")
    assert kinds(mine) == ["png-base", "png-mid", "png-top-mine"]
    assert meta(mine, "png-top-mine")["extends"] == ["png-mid"] and meta(mine, "png-mid")["extends"] == ["png-base"]
    assert text(mine, "png-mid") == 'include("mine/png/png-base.fan")\n<start> ::= <base>\n'
    assert text(mine, "png-top-mine") == 'include("mine/png/png-mid.fan")\n<start> ::= <mid>\n'
    assert meta(mine, "png-base")["derived_from"] == "acme:png/png-base@1.0" and meta(mine, "png-base")["authors"] == ["Bob", "Ada Lovelace"]
    assert meta(mine, "png-top-mine")["derived_from"] == "acme:png/png-top@2.0"
    assert in_order(capsys, mine)


def test_without_the_flag_a_third_registrys_spec_cannot_be_moved(capsys, reg, acme, mine):
    added(capsys, reg, acme)
    code, _, err = run(capsys, "--registry", str(reg), "fork", "acme:png-top", "--as", "png-top-mine", "--into", str(mine))
    assert code == 2 and "fork what it extends first" in err and kinds(mine) == []


def test_what_a_third_registry_takes_from_the_public_one_is_copied_too(capsys, reg, acme, mine):
    republish(acme, "png-base", extends=["fanbase:png"])
    write_text(acme, "png-base", 'include("png/png.fan")\n<start> ::= <png>\n')
    added(capsys, reg, acme)
    code, out, _ = run(capsys, "--registry", str(reg), "fork", "acme:png-top", "--as", "png-top-mine", "--into", str(mine), "--with-deps")
    assert code == 0 and kinds(mine) == ["png", "png-base", "png-mid", "png-top-mine"]
    assert meta(mine, "png")["derived_from"] == "fanbase:png/png@1.0"
    assert meta(mine, "png-base")["extends"] == ["png"]  # the copy
    assert text(mine, "png-base") == 'include("mine/png/png.fan")\n<start> ::= <png>\n'
    assert in_order(capsys, mine)


def test_a_third_registrys_specs_can_be_brought_into_the_public_one(capsys, reg, acme):
    added(capsys, reg, acme)
    code, _, err = run(capsys, "--registry", str(reg), "fork", "acme:png-top", "--as", "png-top-pub", "--with-deps")
    assert code == 0 and err == ""
    assert meta(reg, "png-top-pub")["extends"] == ["png-mid"] and meta(reg, "png-mid")["extends"] == ["png-base"]
    assert text(reg, "png-mid") == 'include("png/png-base.fan")\n<start> ::= <base>\n'  # the public registry's files have no prefix
    assert in_order(capsys, reg)


# --- what is refused, and what is kept

def test_a_name_that_is_taken_by_something_else_stops_everything(capsys, reg, mine):
    clash = build_registry(mine.parent / "mine-with-png", {("png", "png"): dict(version="9.0", description="not a fork")}, name="mine")
    code, _, err = run(capsys, "--registry", str(reg), "fork", "png-apng", "--as", "png-apng-mine", "--into", str(clash), "--with-deps")
    assert code == 2 and "png/png exists already" in err and "not a fork of fanbase:png/png" in err and "nothing was copied" in err
    assert kinds(clash) == ["png"] and meta(clash, "png")["version"] == "9.0"  # and nothing else was written
    assert in_order(capsys, clash)


def test_a_fork_of_it_that_is_there_already_is_kept_as_it_is(capsys, reg, mine):
    run(capsys, "--registry", str(reg), "fork", "png-apng", "--as", "png-apng-mine", "--into", str(mine), "--with-deps")
    write_text(mine, "png", "# my changes\n<start> ::= 'mine'\n")
    code, out, _ = run(capsys, "--registry", str(reg), "fork", "png-anim", "--as", "png-anim-mine", "--into", str(mine), "--with-deps")
    assert code == 0 and "kept specs/png/png/, which is a fork of fanbase:png/png already" in out
    assert text(mine, "png") == "# my changes\n<start> ::= 'mine'\n"  # what you did with it is yours
    assert meta(mine, "png-anim-mine")["extends"] == ["png-apng"] and text(mine, "png-anim-mine").startswith('include("mine/png/png-apng.fan")')
    assert kinds(mine) == ["png", "png-anim-mine", "png-apng", "png-apng-mine"]
    assert in_order(capsys, mine)


def test_two_of_the_specs_would_be_called_the_same(capsys, reg, acme, mine):
    added(capsys, reg, acme)
    code, _, err = run(capsys, "--registry", str(reg), "fork", "acme:png-top", "--as", "png-base", "--into", str(mine), "--with-deps")
    assert code == 2 and "would both be called png/png-base" in err and kinds(mine) == []


def test_a_failure_halfway_leaves_nothing_behind(capsys, reg, mine, monkeypatch):
    def fail(registry):
        raise RegistryError("the index could not be written")

    monkeypatch.setattr(contrib, "refresh", fail)
    code, _, err = run(capsys, "--registry", str(reg), "fork", "png-anim", "--as", "png-anim-mine", "--into", str(mine), "--with-deps")
    assert code == 2 and "could not be written" in err
    assert kinds(mine) == [] and not (mine / "specs/png").exists()


def test_the_hash_that_rebase_merges_from_is_the_original_not_the_rewritten_copy(capsys, reg, mine):
    run(capsys, "--registry", str(reg), "fork", "png-apng", "--as", "png-apng-mine", "--into", str(mine), "--with-deps")
    original = b'include("png/png.fan")\n<start> ::= <png>\n'
    assert meta(mine, "png-apng-mine")["derived_sha256"] == contrib.sha256(original)
    assert contrib.sha256(original) != contrib.sha256(text(mine, "png-apng-mine").encode())  # the copy's include moved
