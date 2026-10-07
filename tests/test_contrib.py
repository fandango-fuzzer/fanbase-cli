import subprocess

import pytest
import yaml
from conftest import build_registry, republish

from fanbase import contrib
from fanbase.cli import main
from fanbase.manifest import INDEX_FILENAME

# The suite replaces subprocess.run for every test (see conftest); this is the real one.
REAL_RUN = subprocess.run


@pytest.fixture(autouse=True)
def who(monkeypatch):
    """Whoever runs the tests, the author of new specs is Ada."""
    monkeypatch.setattr(contrib, "git_user", lambda: "Ada Lovelace")


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def meta(reg, kind):
    return yaml.safe_load((reg / "specs" / kind.split("-")[0] / kind / "metadata.yml").read_text())


def text(reg, kind):
    return (reg / "specs" / kind.split("-")[0] / kind / f"{kind}.fan").read_text()


@pytest.fixture
def reg(tmp_path):
    return build_registry(tmp_path / "reg", {
        ("png", "png"): dict(version="1.0", extensions=["png"], description="Valid PNGs", authors=["Grace Hopper"], license="Apache-2.0"),
        ("png", "png-apng"): dict(version="1.0", extensions=["png"], description="Animated", extends=["png"],
                                  authors=["Grace Hopper"], license="Apache-2.0"),
        ("gif", "gif"): dict(version="1.0", description="Plain GIF", authors=["Grace Hopper"], license="Apache-2.0"),
    })


# --- new

def test_new_starts_a_spec_that_is_ready_to_edit(capsys, reg):
    code, out, _ = run(capsys, "--registry", str(reg), "new", "gif-animated", "--description", "Animated GIFs")
    assert code == 0 and "created specs/gif/gif-animated/" in out and "fanbase check gif-animated" in out
    m = meta(reg, "gif-animated")
    assert (m["description"], m["version"], m["status"], m["authors"]) == ("Animated GIFs", "0.1", "draft", ["Ada Lovelace"])
    assert "# gif-animated: Animated GIFs" in text(reg, "gif-animated") and '<start> ::= "TODO"' in text(reg, "gif-animated")
    # and the registry stays in order: the index has it
    assert run(capsys, "--registry", str(reg), "reindex", "--check")[0] == 0
    index = yaml.safe_load((reg / INDEX_FILENAME).read_text())
    assert "gif-animated" in [row["kind"] for row in index["specs"]]


def test_new_that_builds_on_another_spec(capsys, reg):
    code, _, _ = run(capsys, "--registry", str(reg), "new", "png-fancy", "--extends", "png>=1.0", "--description", "Fancy")
    assert code == 0
    assert 'include("png/png.fan")' in text(reg, "png-fancy")
    m = meta(reg, "png-fancy")
    assert m["extends"] == ["png>=1.0"] and m["extensions"] == ["png"]  # what it builds on says what the files are
    assert run(capsys, "--registry", str(reg), "deps", "png-fancy")[1].splitlines()[1] == "└── png/png  1.0"


def test_new_in_a_registry_of_someones_own_includes_by_its_name(capsys, tmp_path):
    acme = build_registry(tmp_path / "acme", {("png", "png-base"): dict(version="1.0", extensions=["png"])}, name="acme")
    code, _, _ = run(capsys, "--registry", str(acme), "new", "png-fancy", "--extends", "png-base", "--extends", "fanbase:png")
    assert code == 0
    assert 'include("acme/png/png-base.fan")' in text(acme, "png-fancy")
    assert 'include("png/png.fan")' in text(acme, "png-fancy")  # the default registry's file is not under acme/
    assert meta(acme, "png-fancy")["extends"] == ["png-base", "fanbase:png"]


def test_new_format(capsys, reg):
    assert run(capsys, "--registry", str(reg), "new", "webp/webp")[0] == 0
    assert (reg / "specs/webp/webp/webp.fan").is_file()


@pytest.mark.parametrize("ref, complaint", [
    ("png", "exists already"),
    ("png/png-apng", "exists already"),
    ("gif-", "not a usable spec name|is called"),
    ("png/jpeg-x", "is called png"),
    ("png/x", "is called png"),
    ("../x", "not a usable spec name"),
    ("png/../x", "not a usable spec name"),
    ("", "not a usable spec name"),
])
def test_new_refuses_a_bad_or_taken_name(capsys, reg, ref, complaint):
    code, _, err = run(capsys, "--registry", str(reg), "new", ref)
    assert code == 2
    import re

    assert re.search(complaint, err)


@pytest.mark.parametrize("extends, complaint", [
    (["nope"], "cannot extend nope"),
    (["png>=3"], "needs png/png in >=3"),
    (["acme:png"], "can only extend its own"),
])
def test_new_leaves_nothing_behind_when_it_cannot_extend(capsys, reg, extends, complaint):
    args = [x for e in extends for x in ("--extends", e)]
    code, _, err = run(capsys, "--registry", str(reg), "new", "png-fancy", *args)
    assert code == 2 and complaint in err
    assert not (reg / "specs/png/png-fancy").exists()


def test_new_leaves_nothing_behind_when_the_index_cannot_be_made(capsys, reg, monkeypatch):
    def boom(registry):
        raise contrib.RegistryError("boom")

    monkeypatch.setattr(contrib, "refresh", boom)
    assert run(capsys, "--registry", str(reg), "new", "webp/webp")[0] == 2
    assert not (reg / "specs/webp").exists()  # not even the new format's folder


# --- fork

@pytest.fixture
def mine(tmp_path):
    return build_registry(tmp_path / "mine", {("gif", "gif"): dict(version="1.0")}, name="mine")


def test_fork_a_spec_of_the_same_checkout(capsys, reg):
    code, out, _ = run(capsys, "--registry", str(reg), "fork", "png", "--as", "png-better")
    assert code == 0 and "forked fanbase:png/png into specs/png/png-better/" in out
    assert text(reg, "png-better") == text(reg, "png")
    m = meta(reg, "png-better")
    assert m["derived_from"] == "fanbase:png/png@1.0"
    assert m["authors"] == ["Grace Hopper", "Ada Lovelace"]  # the original authors stay, and you are added
    assert (m["version"], m["status"], m["license"]) == ("0.1", "draft", "Apache-2.0")
    assert m["description"] == "Valid PNGs (fork of fanbase:png/png)" and m["extensions"] == ["png"]
    assert run(capsys, "--registry", str(reg), "reindex", "--check")[0] == 0


def test_fork_keeps_what_the_spec_extends(capsys, reg):
    run(capsys, "--registry", str(reg), "fork", "png-apng", "--as", "png-apng-better")
    assert meta(reg, "png-apng-better")["extends"] == ["png"]
    assert meta(reg, "png-apng-better")["derived_from"] == "fanbase:png/png-apng@1.0"


def test_fork_into_a_registry_of_your_own(capsys, reg, mine):
    code, out, _ = run(capsys, "--registry", str(reg), "fork", "png-apng", "--as", "png-apng-mine", "--into", str(mine))
    assert code == 0
    m = meta(mine, "png-apng-mine")
    assert m["extends"] == ["fanbase:png"]  # `png` meant the registry it came from, which is now another one
    assert m["derived_from"] == "fanbase:png/png-apng@1.0"
    assert run(capsys, "--registry", str(mine), "reindex", "--check")[0] == 0


def test_fork_from_a_registry_you_added(capsys, reg, mine, tmp_path):
    acme = build_registry(tmp_path / "acme", {("png", "png-strict"): dict(version="0.2", extensions=["png"], authors=["Bob"])}, name="acme")
    run(capsys, "--registry", str(reg), "registry", "add", str(acme), "--trust")
    code, _, _ = run(capsys, "--registry", str(reg), "fork", "acme:png-strict", "--as", "png-stricter", "--into", str(mine))
    assert code == 0
    m = meta(mine, "png-stricter")
    assert m["derived_from"] == "acme:png/png-strict@0.2" and m["authors"] == ["Bob", "Ada Lovelace"]


def test_fork_cannot_move_a_spec_that_extends_a_third_registry(capsys, reg, tmp_path):
    acme = build_registry(tmp_path / "acme", {
        ("png", "png-base"): dict(version="1.0"), ("png", "png-top"): dict(version="1.0", extends=["png-base"]),
    }, name="acme")
    mine = build_registry(tmp_path / "mine", {("gif", "gif"): dict(version="1.0")}, name="mine")
    run(capsys, "--registry", str(reg), "registry", "add", str(acme), "--trust")
    code, _, err = run(capsys, "--registry", str(reg), "fork", "acme:png-top", "--as", "png-mine", "--into", str(mine))
    assert code == 2 and "fork what it extends first" in err
    assert not (mine / "specs/png").exists()


@pytest.mark.parametrize("name, complaint", [("gif-x", "the format png, not gif"), ("png", "exists already"), ("png-apng", "exists already")])
def test_fork_refuses_a_bad_name(capsys, reg, name, complaint):
    code, _, err = run(capsys, "--registry", str(reg), "fork", "png", "--as", name)
    assert code == 2 and complaint in err


def test_fork_needs_a_name(capsys, reg):
    with pytest.raises(SystemExit):
        main(["--registry", str(reg), "fork", "png"])


# --- changes

@pytest.fixture
def before(tmp_path, reg):
    import shutil

    shutil.copytree(reg, tmp_path / "before")
    return tmp_path / "before"


def test_changes_lists_what_differs(capsys, reg, before):
    republish(reg, "png", "<start> ::= 'png 2'\n", version="1.1")
    republish(reg, "gif", "<start> ::= 'gif quietly changed'\n")
    run(capsys, "--registry", str(reg), "new", "png-fancy", "--description", "Fancy")
    import shutil

    shutil.rmtree(reg / "specs/png/png-apng")
    republish(reg, "png")
    code, out, _ = run(capsys, "--registry", str(reg), "changes", "--base", str(before))
    assert code == 0
    lines = [line.split() for line in out.splitlines()]
    assert ["changed", "gif/gif", "1.0", "->", "1.0", "(no", "new", "version:", "bump", "it)"] in lines
    assert ["changed", "png/png", "1.0", "->", "1.1"] in lines
    assert ["added", "png/png-fancy", "0.1"] in lines
    assert ["removed", "png/png-apng", "1.0"] in lines


def test_changes_with_nothing_to_report(capsys, reg, before):
    code, out, _ = run(capsys, "--registry", str(reg), "changes", "--base", str(before))
    assert code == 0 and out.strip() == "no changes"


def test_changes_check_wants_a_new_version(capsys, reg, before):
    republish(reg, "png", "<start> ::= 'png 2'\n", version="1.1")
    assert run(capsys, "--registry", str(reg), "changes", "--base", str(before), "--check")[0] == 0
    republish(reg, "gif", "<start> ::= 'changed'\n")
    code, _, err = run(capsys, "--registry", str(reg), "changes", "--base", str(before), "--check")
    assert code == 1 and "gif/gif" in err and "png/png" not in err
    republish(reg, "gif", version="0.9")  # going down is no better than staying
    assert run(capsys, "--registry", str(reg), "changes", "--base", str(before), "--check")[0] == 1
    republish(reg, "gif", version="1.0.1")
    assert run(capsys, "--registry", str(reg), "changes", "--base", str(before), "--check")[0] == 0


def test_changes_as_release_notes(capsys, reg, before):
    republish(reg, "png", "<start> ::= 'png 2'\n", version="1.1")
    run(capsys, "--registry", str(reg), "new", "png-fancy", "--description", "Fancy PNGs")
    code, out, _ = run(capsys, "--registry", str(reg), "changes", "--base", str(before), "--markdown")
    assert code == 0
    assert out.splitlines()[0].startswith("## Changes since ")
    assert "### Added\n\n- `png/png-fancy` 0.1 — Fancy PNGs" in out
    assert "### Changed\n\n- `png/png` 1.0 → 1.1 — Valid PNGs" in out
    assert "Removed" not in out


def test_changes_against_a_registry_over_http(capsys, reg, before):
    import functools
    import http.server
    import threading

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(before))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        republish(reg, "png", "<start> ::= 'png 2'\n", version="1.1")
        code, out, _ = run(capsys, "--registry", str(reg), "changes", "--base", f"http://127.0.0.1:{server.server_address[1]}")
        assert code == 0 and "png/png  1.0 -> 1.1" in out
    finally:
        server.shutdown()
        server.server_close()


# --- check

def test_check_of_a_registry_in_order(capsys, reg):
    code, out, _ = run(capsys, "--registry", str(reg), "check", "--no-generate")
    assert code == 0 and "3 specs looked at, 0 produced inputs; nothing wrong" in out


def test_check_notices_a_stale_registry(capsys, reg):
    (reg / "specs/gif/gif/gif.fan").write_text("import brotli\n<start> ::= 'gif'\n")
    code, out, _ = run(capsys, "--registry", str(reg), "check", "--no-generate")
    assert code == 1 and "FAILED:  gif/gif: metadata.yml is out of date" in out and "index.yml is out of date" in out
    run(capsys, "--registry", str(reg), "reindex")
    assert run(capsys, "--registry", str(reg), "check", "--no-generate")[0] == 0


def test_check_notices_a_broken_extends(capsys, reg):
    path = reg / "specs/png/png-apng/metadata.yml"
    data = yaml.safe_load(path.read_text())
    data["extends"] = ["ghost"]
    path.write_text(yaml.safe_dump(data))
    code, _, err = run(capsys, "--registry", str(reg), "check", "--no-generate")
    assert code == 2 and "extends ghost" in err


def test_check_says_what_a_reader_would_miss(capsys, reg):
    run(capsys, "--registry", str(reg), "new", "gif-animated")
    code, out, _ = run(capsys, "--registry", str(reg), "check", "--no-generate")
    assert code == 0 and "warning: gif/gif-animated: no description, no license" in out
    code, out, _ = run(capsys, "--registry", str(reg), "check", "--no-generate", "--strict")
    assert code == 1 and "FAILED:  gif/gif-animated: no description, no license (--strict)" in out


def test_check_a_few_specs(capsys, reg):
    run(capsys, "--registry", str(reg), "new", "gif-animated")
    _, out, _ = run(capsys, "--registry", str(reg), "check", "--no-generate", "png")
    assert "1 specs looked at" in out and "gif-animated" not in out


def test_check_against_a_base(capsys, reg, before):
    republish(reg, "gif", "<start> ::= 'changed'\n")
    code, out, _ = run(capsys, "--registry", str(reg), "check", "--no-generate", "--base", str(before))
    assert code == 1 and "gif/gif: changed since" in out and "still at version 1.0" in out
    republish(reg, "gif", version="1.1")
    assert run(capsys, "--registry", str(reg), "check", "--no-generate", "--base", str(before))[0] == 0


class FakeFandango:
    """Stands in for `fandango fuzz`: writes what it is told to into the output directory."""

    def __init__(self, returncode=0, files=("fandango-0000.txt",), content="x", timeout=False):
        self.returncode, self.files, self.content, self.timeout = returncode, files, content, timeout
        self.calls = []

    def __call__(self, cmd, **kwargs):
        if cmd[0] != "/fake/fandango":
            return subprocess.run(cmd, **kwargs)
        library = kwargs["env"]["FANDANGO_PATH"]
        import pathlib

        self.calls.append({"cmd": cmd, "library": library, "has": sorted(str(p.relative_to(library)) for p in pathlib.Path(library).rglob("*.fan"))})
        if self.timeout:
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        out = pathlib.Path(cmd[cmd.index("-d") + 1])
        if self.returncode == 0:
            out.mkdir(parents=True)
            for name in self.files:
                (out / name).write_text(self.content)
        return subprocess.CompletedProcess(cmd, self.returncode, "", "Population did not converge" if self.returncode else "")


@pytest.fixture
def fake(monkeypatch):
    fandango = FakeFandango()
    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", fandango)
    return fandango


def test_check_makes_fandango_produce_inputs(capsys, reg, fake):
    code, out, _ = run(capsys, "--registry", str(reg), "check", "--count", "5")
    assert code == 0 and "3 specs looked at, 3 produced inputs; nothing wrong" in out
    call = next(c for c in fake.calls if c["cmd"][3].endswith("png-apng.fan"))
    assert call["cmd"][:3] == ["/fake/fandango", "fuzz", "-f"] and call["cmd"][4:6] == ["-n", "5"]
    assert call["has"] == ["png/png-apng.fan", "png/png.fan"]  # what it extends is where include() looks


def test_check_of_a_registry_of_someones_own_installs_under_its_name(capsys, tmp_path, fake):
    acme = build_registry(tmp_path / "acme", {
        ("png", "png-base"): dict(version="1.0"), ("png", "png-top"): dict(version="1.0", extends=["png-base"]),
    }, name="acme")
    assert run(capsys, "--registry", str(acme), "check")[0] == 0
    top = next(c for c in fake.calls if c["cmd"][3].endswith("png-top.fan"))
    assert top["has"] == ["acme/png/png-base.fan", "acme/png/png-top.fan"]


@pytest.mark.parametrize("fandango, complaint", [
    (FakeFandango(returncode=1), "fandango failed (1): Population did not converge"),
    (FakeFandango(files=()), "fandango produced no files"),
    (FakeFandango(content=""), "fandango produced only empty files"),
    (FakeFandango(timeout=True), "fandango did not finish in 7 seconds"),
])
def test_check_says_why_a_spec_produced_nothing(capsys, reg, monkeypatch, fandango, complaint):
    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", fandango)
    code, out, _ = run(capsys, "--registry", str(reg), "check", "--timeout", "7")
    assert code == 1 and f"FAILED:  gif/gif: {complaint}" in out and "0 produced inputs" in out


def test_check_without_fandango_says_so(capsys, reg, monkeypatch):
    monkeypatch.setattr(contrib, "find_fandango", lambda: None)
    code, out, _ = run(capsys, "--registry", str(reg), "check")
    assert code == 0 and "note: fandango is not installed" in out


def test_check_leaves_no_temporary_files(capsys, reg, fake, tmp_path):
    import glob
    import tempfile

    before = set(glob.glob(f"{tempfile.gettempdir()}/fanbase-check-*"))
    run(capsys, "--registry", str(reg), "check")
    assert set(glob.glob(f"{tempfile.gettempdir()}/fanbase-check-*")) == before


@pytest.mark.skipif(contrib.find_fandango() is None, reason="fandango is not installed")
def test_check_with_the_real_fandango(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(contrib, "_run", REAL_RUN)
    reg = build_registry(tmp_path / "real", {("txt", "txt"): dict(version="1.0", description="x", authors=["a"], license="MIT")})
    (reg / "specs/txt/txt/txt.fan").write_text('<start> ::= "hello" | "world" | "again"\n')
    run(capsys, "--registry", str(reg), "reindex")
    code, out, err = run(capsys, "--registry", str(reg), "check", "--count", "1")
    assert code == 0 and "1 produced inputs" in out, out + err
    (reg / "specs/txt/txt/txt.fan").write_text("<start> ::= <nothing>\n")
    run(capsys, "--registry", str(reg), "reindex")
    code, out, _ = run(capsys, "--registry", str(reg), "check", "--count", "1")
    assert code == 1 and "FAILED:  txt/txt: fandango failed" in out
