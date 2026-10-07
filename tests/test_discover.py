import json

import pytest
from conftest import build_registry, republish

from fanbase.cli import main
from fanbase.manager import spec_path


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("FANDANGO_PATH", str(tmp_path / "fandango"))
    return tmp_path / "fandango"


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def reg(tmp_path):
    return build_registry(tmp_path / "reg", {
        ("png", "png"): dict(version="1.0", title="Portable Network Graphics", description="Valid PNGs", extensions=["png"], mime="image/png",
                             authors=["Ada Lovelace", {"name": "Grace Hopper", "orcid": "0000-0002-1825-0097"}]),
        ("png", "png-apng"): dict(version="1.0", description="Animated PNG", extensions=["png"]),
        ("gif", "gif"): dict(version="1.0", description="Plain GIF", extensions=["gif"], mime="image/gif"),
        ("bmp", "bmp"): dict(version="1.0", description="Windows bitmaps with RLE", extensions=["bmp", "dib"]),
    })


@pytest.fixture
def acme(tmp_path, reg, capsys):
    path = build_registry(tmp_path / "acme", {
        ("png", "png-strict"): dict(version="0.2", description="Strict PNG, nothing odd", extensions=["png"]),
    }, name="acme")
    assert run(capsys, "--registry", str(reg), "registry", "add", str(path), "--trust")[0] == 0
    return path


# --- outdated

def test_outdated_with_nothing_to_report(capsys, reg, root):
    run(capsys, "--registry", str(reg), "install", "png", "gif")
    code, out, _ = run(capsys, "--registry", str(reg), "outdated")
    assert code == 0 and "everything installed is up to date" in out


def test_outdated_says_what_moved(capsys, reg, root):
    run(capsys, "--registry", str(reg), "install", "png", "gif", "bmp")
    republish(reg, "png", "<start> ::= 'png 2'\n", version="1.1")
    republish(reg, "gif", "<start> ::= 'gif quietly changed'\n")
    import shutil

    shutil.rmtree(reg / "specs" / "bmp")
    republish(reg, "gif")
    code, out, _ = run(capsys, "--registry", str(reg), "outdated")
    assert code == 0
    assert "png/png  1.0 -> 1.1" in out
    assert "gif/gif  1.0  (changed, with no new version)" in out
    assert "bmp/bmp  1.0  (no longer in its registry)" in out
    assert "fanbase update" in out


def test_outdated_check_is_for_scripts(capsys, reg, root):
    run(capsys, "--registry", str(reg), "install", "png")
    assert run(capsys, "--registry", str(reg), "outdated", "--check")[0] == 0
    republish(reg, "png", "<start> ::= 'png 2'\n", version="1.1")
    assert run(capsys, "--registry", str(reg), "outdated", "--check")[0] == 1


def test_outdated_json(capsys, reg, root):
    run(capsys, "--registry", str(reg), "install", "png")
    republish(reg, "png", "<start> ::= 'png 2'\n", version="1.1")
    code, out, _ = run(capsys, "--registry", str(reg), "outdated", "--json")
    assert code == 0 and json.loads(out) == [{"spec": "png/png", "installed": "1.0", "registry": "1.1", "status": "newer"}]


def test_outdated_looks_at_each_specs_own_registry(capsys, reg, acme, root):
    run(capsys, "--registry", str(reg), "install", "png", "acme:png-strict")
    republish(acme, "png-strict", "<start> ::= 'stricter'\n", version="0.3")
    _, out, _ = run(capsys, "--registry", str(reg), "outdated")
    assert "acme:png/png-strict  0.2 -> 0.3" in out and "  png/png " not in out


# --- diff

def test_diff_of_something_not_installed(capsys, reg, root):
    code, _, err = run(capsys, "--registry", str(reg), "diff", "png")
    assert code == 2 and "png/png is not installed" in err and "fanbase diff A B" in err


def test_diff_of_an_unchanged_spec_is_empty(capsys, reg, root):
    run(capsys, "--registry", str(reg), "install", "png")
    code, out, _ = run(capsys, "--registry", str(reg), "diff", "png")
    assert code == 0 and out == ""


def test_diff_shows_how_the_registry_changed(capsys, reg, root):
    run(capsys, "--registry", str(reg), "install", "png")
    republish(reg, "png", "<start> ::= 'png'\n<more> ::= 'x'\n", version="1.1")
    code, out, _ = run(capsys, "--registry", str(reg), "diff", "png")
    assert code == 1
    assert out.startswith("--- installed png/png\n+++ registry png/png\n")
    assert "+<more> ::= 'x'" in out


def test_diff_of_two_specs(capsys, reg, acme, root):
    code, out, _ = run(capsys, "--registry", str(reg), "diff", "png", "acme:png-strict")
    assert code == 1
    assert out.startswith("--- png/png\n+++ acme:png/png-strict\n")
    assert "-<start> ::= 'png'" in out and "+<start> ::= 'png-strict'" in out
    assert run(capsys, "--registry", str(reg), "diff", "png", "png")[0] == 0


def test_diff_takes_one_or_two(capsys, reg):
    assert run(capsys, "--registry", str(reg), "diff", "png", "gif", "bmp")[0] == 2


def test_diff_does_not_pass_on_escape_sequences(capsys, reg, root):
    run(capsys, "--registry", str(reg), "install", "png")
    republish(reg, "png", "<start> ::= 'a'\n# \x1b[2J\x1b]0;owned\x07\n", version="1.1")
    _, out, _ = run(capsys, "--registry", str(reg), "diff", "png")
    assert "\x1b" not in out and "\x07" not in out and "owned" in out


# --- search

def test_search_finds_every_word(capsys, reg):
    _, out, _ = run(capsys, "--registry", str(reg), "search", "animated", "png")
    assert "png/png-apng" in out and "png/png " not in out and "gif" not in out
    _, out, _ = run(capsys, "--registry", str(reg), "search", "animated", "gif")
    assert out.strip() == "nothing found"


def test_search_looks_at_title_type_and_extension(capsys, reg):
    assert "png/png" in run(capsys, "--registry", str(reg), "search", "portable")[1]
    assert "gif/gif" in run(capsys, "--registry", str(reg), "search", "image/gif")[1]
    assert "bmp/bmp" in run(capsys, "--registry", str(reg), "search", "dib")[1]
    assert "png/png" in run(capsys, "--registry", str(reg), "search", "PNG")[1]  # any case


def test_search_by_extension(capsys, reg):
    _, out, _ = run(capsys, "--registry", str(reg), "search", "--extension", ".dib")
    assert out.strip().startswith("bmp/bmp") and out.count("\n") == 1
    _, out, _ = run(capsys, "--registry", str(reg), "search", "--extension", "png")
    assert out.count("\n") == 2


def test_search_everything_with_no_words(capsys, reg):
    assert run(capsys, "--registry", str(reg), "search")[1].count("\n") == 4


def test_search_only_looks_in_the_default_registry_unless_asked(capsys, reg, acme):
    assert run(capsys, "--registry", str(reg), "search", "strict")[1].strip() == "nothing found"
    _, out, _ = run(capsys, "--registry", str(reg), "search", "strict", "--all")
    assert "acme:png/png-strict" in out


def test_search_json(capsys, reg, acme):
    _, out, _ = run(capsys, "--registry", str(reg), "search", "png", "--all", "--json", "--extension", "png")
    found = json.loads(out)
    assert [row["spec"] for row in found] == ["png/png", "png/png-apng", "acme:png/png-strict"]
    assert found[2] == {"spec": "acme:png/png-strict", "registry": "acme", "format": "png", "kind": "png-strict",
                        "version": "0.2", "description": "Strict PNG, nothing odd", "extensions": ["png"]}


# --- cite

def test_cite_in_words(capsys, reg):
    code, out, _ = run(capsys, "--registry", str(reg), "cite", "png")
    assert code == 0
    assert out.startswith("Ada Lovelace, Grace Hopper. Portable Network Graphics (png/png), version 1.0. Fanbase registry")
    assert "specs/png/png/png.fan, sha256 " in out and "accessed 20" in out


def test_cite_prefers_the_doi_when_there_is_one(capsys, reg):
    republish(reg, "png", doi="10.5281/zenodo.1234567")
    _, out, _ = run(capsys, "--registry", str(reg), "cite", "png")
    assert "https://doi.org/10.5281/zenodo.1234567" in out and str(reg) not in out


def test_cite_as_bibtex(capsys, reg):
    republish(reg, "png", doi="10.5281/zenodo.1234567", title="Portable Network Graphics & friends {x}")
    _, out, _ = run(capsys, "--registry", str(reg), "cite", "png", "--bibtex")
    lines = out.splitlines()
    assert lines[0] == "@software{fanbase_png_png_1_0," and lines[-1] == "}"
    assert "  author  = {Ada Lovelace and Grace Hopper}," in lines
    assert "Portable Network Graphics \\& friends x (png/png)" in out  # specials escaped, braces dropped
    assert "  doi     = {10.5281/zenodo.1234567}," in lines and "  version = {1.0}," in lines
    assert any(line.startswith("  urldate = {20") for line in lines)


def test_cite_a_spec_nobody_signed(capsys, reg):
    _, out, _ = run(capsys, "--registry", str(reg), "cite", "gif")
    assert out.startswith("Fanbase contributors. gif (gif/gif), version 1.0.")


def test_cite_json_and_a_registry_you_added(capsys, reg, acme):
    _, out, _ = run(capsys, "--registry", str(reg), "cite", "acme:png-strict", "--json")
    c = json.loads(out)
    assert c["spec"] == "acme:png/png-strict" and c["version"] == "0.2" and c["url"] == str(acme.resolve())
    assert len(c["sha256"]) == 64


# --- --json elsewhere

def test_list_as_json(capsys, reg, root):
    run(capsys, "--registry", str(reg), "install", "png")
    _, out, _ = run(capsys, "--registry", str(reg), "list", "--json")
    assert json.loads(out) == [{"format": "bmp", "specs": 1}, {"format": "gif", "specs": 1}, {"format": "png", "specs": 2}]
    _, out, _ = run(capsys, "--registry", str(reg), "list", "png", "--json")
    rows = json.loads(out)
    assert [(r["kind"], r["installed"]) for r in rows] == [("png", True), ("png-apng", False)]
    assert rows[0]["version"] == "1.0" and rows[0]["extensions"] == ["png"]


def test_list_installed_as_json(capsys, reg, acme, root):
    run(capsys, "--registry", str(reg), "install", "png", "acme:png-strict")
    _, out, _ = run(capsys, "list", "--installed", "--json")
    rows = json.loads(out)
    assert [(r["spec"], r["registry"]) for r in rows] == [("png/png", "fanbase"), ("acme:png/png-strict", "acme")]
    assert rows[0]["path"] == str(spec_path(root, "png", "png")) and len(rows[0]["sha256"]) == 64
    assert json.loads(run(capsys, "list", "--installed", "--json", "gif")[1]) == []


def test_show_as_json(capsys, reg, acme):
    _, out, _ = run(capsys, "--registry", str(reg), "show", "png", "--json")
    shown = json.loads(out)
    assert shown["format"] == "png" and shown["kind"] == "png" and shown["extensions"] == ["png"]
    assert shown["authors"][1]["orcid"] == "0000-0002-1825-0097"
    assert json.loads(run(capsys, "--registry", str(reg), "show", "acme:png-strict", "--json")[1])["registry"] == "acme"


def test_json_does_not_hide_control_characters_but_escapes_them(capsys, reg):
    republish(reg, "png", description="bad \x1b[2J news")
    _, out, _ = run(capsys, "--registry", str(reg), "search", "bad", "--json")
    assert "\x1b" not in out and "\\u001b" in out and json.loads(out)[0]["description"] == "bad \x1b[2J news"
