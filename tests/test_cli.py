import pytest

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


@pytest.mark.parametrize("remote", [False, True])
def test_list_install_update(capsys, registry, served, root, remote):
    where = served if remote else str(registry)
    code, out, _ = run(capsys, "--registry", where, "list")
    assert code == 0 and "png  2 specs" in out and "gif  1 spec" in out

    code, out, _ = run(capsys, "--registry", where, "list", "png")
    assert "png-apng" in out and "animated png" in out

    code, out, _ = run(capsys, "--registry", where, "install", "png", "png-apng")
    assert code == 0 and out.count("installed png/") == 2
    assert spec_path(root, "png", "png-apng").is_file()

    code, out, _ = run(capsys, "--registry", where, "list", "png")
    assert "*png-apng" in out and "*png " in out

    code, out, _ = run(capsys, "--registry", where, "update")
    assert out.count("up to date") == 2

    (registry / "specs/png/png/png.fan").write_text("<start> ::= 'changed'\n")
    from fanbase.manifest import INDEX_FILENAME, dump_index, reindex
    from fanbase.registry import Registry

    rows, _, _ = reindex(Registry(registry))
    (registry / INDEX_FILENAME).write_text(dump_index(rows))
    code, out, _ = run(capsys, "--registry", where, "update", "png")
    assert "updated png/png" in out


def test_show(capsys, registry):
    code, out, _ = run(capsys, "--registry", str(registry), "show", "png-apng")
    assert code == 0 and "kind: png-apng" in out and "description: animated png" in out


def test_unknown_spec_is_an_error(capsys, registry):
    code, _, err = run(capsys, "--registry", str(registry), "install", "nope")
    assert code == 2 and "unknown" in err


def test_unreachable_registry_is_an_error(capsys, root):
    code, _, err = run(capsys, "--registry", "http://127.0.0.1:9", "list")
    assert code == 2 and "could not reach" in err


def test_reindex_and_check(capsys, registry):
    code, out, _ = run(capsys, "--registry", str(registry), "reindex", "--check")
    assert code == 0
    (registry / "specs/png/png/png.fan").write_text("import brotli\n<start> ::= 'x'\n")
    code, out, _ = run(capsys, "--registry", str(registry), "reindex", "--check")
    assert code == 1 and "out of date" in out
    code, out, _ = run(capsys, "--registry", str(registry), "reindex")
    assert code == 0
    code, out, _ = run(capsys, "--registry", str(registry), "reindex", "--check")
    assert code == 0
    code, out, _ = run(capsys, "--registry", str(registry), "show", "png")
    assert "- brotli" in out


def test_reindex_refuses_a_url(capsys, served):
    code, _, err = run(capsys, "--registry", served, "reindex")
    assert code == 2 and "local" in err


def test_registry_is_not_guessed_from_an_unrelated_specs_folder(tmp_path, monkeypatch):
    from fanbase.source import find_registry
    from fanbase.remote import RemoteRegistry

    (tmp_path / "specs").mkdir()  # someone's own project
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FANBASE_REGISTRY", raising=False)
    monkeypatch.setattr("fanbase.source.RemoteRegistry", lambda url: ("remote", url))
    assert find_registry()[0] == "remote"
