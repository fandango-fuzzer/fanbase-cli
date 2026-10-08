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

    (registry / "specs/png/png/png.fan").write_text("<start> ::= 'changed'\n", newline="\n")
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
    (registry / "specs/png/png/png.fan").write_text("import brotli\n<start> ::= 'x'\n", newline="\n")
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


@pytest.mark.parametrize("remote", [False, True])
def test_install_all(capsys, registry, served, root, remote):
    where = served if remote else str(registry)
    code, out, _ = run(capsys, "--registry", where, "install", "--all")
    assert code == 0
    assert "3 specs: 3 installed" in out
    for fmt, kind in [("png", "png"), ("png", "png-apng"), ("gif", "gif")]:
        assert spec_path(root, fmt, kind).is_file()

    code, out, _ = run(capsys, "--registry", where, "install", "--all")
    assert code == 0 and "3 specs: 3 up to date" in out


def test_install_all_reports_what_the_specs_need(capsys, registry, root, pip_calls):
    (registry / "specs/png/png-apng/png-apng.fan").write_text("import brotli\n<start> ::= 'x'\n", newline="\n")
    run(capsys, "--registry", str(registry), "reindex")
    code, out, _ = run(capsys, "--registry", str(registry), "install", "--all", "--no-requirements")
    assert code == 0 and "requires: pip install brotli" in out
    assert pip_calls == []


def test_install_needs_specs_or_all(capsys, registry, root):
    code, _, err = run(capsys, "--registry", str(registry), "install")
    assert code == 2 and "--all" in err
    code, _, err = run(capsys, "--registry", str(registry), "install", "png", "--all")
    assert code == 2 and "not both" in err
    assert not spec_path(root, "png", "png").exists()


NO_NETWORK = "http://127.0.0.1:9"  # nothing listens here


def test_list_installed_asks_no_registry(capsys, registry, root):
    code, out, _ = run(capsys, "--registry", NO_NETWORK, "list", "--installed")
    assert code == 0 and out.strip() == "nothing installed"

    run(capsys, "--registry", str(registry), "install", "png", "png-apng", "gif")
    code, out, _ = run(capsys, "--registry", NO_NETWORK, "list", "--installed")
    assert code == 0
    assert "png/png  " in out and "plain png" in out
    assert "png/png-apng" in out and "animated png" in out
    assert "gif/gif" in out

    code, out, _ = run(capsys, "--registry", NO_NETWORK, "list", "--installed", "png")
    assert "png/png-apng" in out and "gif/gif" not in out


def test_list_installed_ignores_spec_files_fanbase_did_not_install(capsys, root):
    (root / "mine").mkdir(parents=True)
    (root / "mine" / "mine.fan").write_text("<start> ::= 'x'\n", newline="\n")
    code, out, _ = run(capsys, "list", "--installed")
    assert code == 0 and out.strip() == "nothing installed"


def test_uninstall_removes_the_spec_and_its_metadata_offline(capsys, registry, root):
    run(capsys, "--registry", str(registry), "install", "png", "png-apng", "gif")
    code, out, _ = run(capsys, "--registry", NO_NETWORK, "uninstall", "png-apng", "gif")
    assert code == 0 and "removed png/png-apng" in out and "removed gif/gif" in out
    assert not spec_path(root, "png", "png-apng").exists()
    assert not spec_path(root, "png", "png-apng").with_suffix(".yml").exists()
    assert spec_path(root, "png", "png").is_file()  # the other spec is untouched
    assert not (root / "gif").exists()  # an empty format directory goes with its last spec


def test_uninstall_something_not_installed_changes_nothing(capsys, registry, root):
    run(capsys, "--registry", str(registry), "install", "png")
    code, _, err = run(capsys, "uninstall", "png", "png-apng")
    assert code == 2 and "png-apng is not installed" in err
    assert spec_path(root, "png", "png").is_file()  # nothing was removed, not even the one that was there


def test_uninstall_never_removes_a_file_fanbase_did_not_install(capsys, root):
    mine = root / "png" / "mine.fan"
    mine.parent.mkdir(parents=True)
    mine.write_text("<start> ::= 'x'\n")
    code, _, err = run(capsys, "uninstall", "png/mine")
    assert code == 2 and "not installed" in err
    assert mine.is_file()


def test_uninstall_keeps_a_directory_that_holds_other_files(capsys, registry, root):
    run(capsys, "--registry", str(registry), "install", "png")
    other = root / "png" / "mine.fan"
    other.write_text("<start> ::= 'x'\n")
    code, _, _ = run(capsys, "uninstall", "png")
    assert code == 0 and other.is_file()
    assert not spec_path(root, "png", "png").exists()
