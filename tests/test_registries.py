import io
import sys

import pytest
import yaml

from fanbase.cli import main
from fanbase.config import ENV_CONFIG, load_config
from fanbase.manager import all_installed, ensure, installed_copy, spec_path
from fanbase.registry import RegistryError


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("FANDANGO_PATH", str(tmp_path / "fandango"))
    return tmp_path / "fandango"


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def added(capsys, registry, acme):
    """The default registry is the `registry` fixture; acme has been added."""
    code, _, _ = run(capsys, "--registry", str(registry), "registry", "add", str(acme), "--trust")
    assert code == 0
    return acme


# --- adding, listing, removing

def test_a_registry_is_added_under_the_name_it_gives_itself(capsys, registry, acme):
    code, out, _ = run(capsys, "--registry", str(registry), "registry", "add", str(acme), "--trust")
    assert code == 0 and "added acme: 2 formats" in out
    assert load_config().registries["acme"].url == str(acme.resolve())


def test_adding_says_what_trusting_a_registry_means(capsys, registry, acme):
    _, out, _ = run(capsys, "--registry", str(registry), "registry", "add", str(acme), "--trust")
    assert "Python code that runs inside Fandango" in out


def test_without_confirmation_nothing_is_added(capsys, registry, acme, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # not a terminal
    code, _, err = run(capsys, "--registry", str(registry), "registry", "add", str(acme))
    assert code == 2 and "--trust" in err
    assert load_config().registries == {}


@pytest.mark.parametrize("answer, added_", [("y", True), ("yes", True), ("", False), ("n", False)])
def test_on_a_terminal_the_user_is_asked(capsys, registry, acme, monkeypatch, answer, added_):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Terminal(""))
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    code, _, err = run(capsys, "--registry", str(registry), "registry", "add", str(acme))
    assert (code == 0) is added_
    assert ("acme" in load_config().registries) is added_
    if not added_:
        assert "not added" in err


def test_a_registry_without_a_name_needs_one(capsys, registry, tmp_path):
    from conftest import write_spec
    from fanbase.manifest import INDEX_FILENAME, dump_index, reindex
    from fanbase.registry import Registry

    anon = tmp_path / "anon"
    write_spec(anon, "png", "png", "<start> ::= 'x'\n", "x")
    rows, _, _ = reindex(Registry(anon))
    (anon / INDEX_FILENAME).write_text(dump_index(rows))
    code, _, err = run(capsys, "--registry", str(registry), "registry", "add", str(anon), "--trust")
    assert code == 2 and "--name" in err
    code, out, _ = run(capsys, "--registry", str(registry), "registry", "add", str(anon), "--trust", "--name", "mine")
    assert code == 0 and "mine" in load_config().registries


@pytest.mark.parametrize("name", ["fanbase", "Acme", "a b", "1x", "a/b", ""])
def test_a_bad_registry_name_is_refused(capsys, registry, acme, name):
    code, _, err = run(capsys, "--registry", str(registry), "registry", "add", str(acme), "--trust", "--name", name)
    assert code == 2
    assert load_config().registries == {}


def test_adding_twice(capsys, registry, acme, added, tmp_path):
    code, out, _ = run(capsys, "--registry", str(registry), "registry", "add", str(acme), "--trust")
    assert code == 0 and "already added" in out
    other = tmp_path / "other"
    other.mkdir()
    (other / "specs").mkdir()
    code, _, err = run(capsys, "--registry", str(registry), "registry", "add", str(other), "--trust", "--name", "acme")
    assert code == 2 and "already added" in err


def test_a_registry_that_calls_itself_something_else_is_pointed_out(capsys, registry, acme):
    _, _, err = run(capsys, "--registry", str(registry), "registry", "add", str(acme), "--trust", "--name", "other")
    assert "calls itself acme" in err


def test_listing_the_registries(capsys, registry, added):
    code, out, _ = run(capsys, "--registry", str(registry), "registry", "list")
    assert code == 0
    assert "fanbase" in out and str(registry) in out and "(default)" in out
    assert "acme" in out and str(added.resolve()) in out


def test_removing_a_registry_keeps_what_was_installed_unless_asked(capsys, registry, added, root):
    run(capsys, "--registry", str(registry), "install", "acme:png-strict")
    code, out, _ = run(capsys, "--registry", str(registry), "registry", "remove", "acme")
    assert code == 0 and "1 installed specs of acme are still there" in out
    assert "acme" not in load_config().registries
    assert spec_path(root, "png", "png-strict", "acme").is_file()


def test_removing_a_registry_can_remove_its_specs(capsys, registry, added, root):
    run(capsys, "--registry", str(registry), "install", "acme:png-strict", "acme:bmp")
    code, out, _ = run(capsys, "--registry", str(registry), "registry", "remove", "acme", "--uninstall")
    assert code == 0 and "removed 2 installed specs" in out
    assert not (root / "acme").exists()


def test_removing_an_unknown_registry(capsys, registry):
    assert run(capsys, "--registry", str(registry), "registry", "remove", "nope")[0] == 2


# --- installing from a registry that was added

def test_a_spec_of_an_added_registry_is_installed_under_its_name(capsys, registry, added, root):
    code, out, _ = run(capsys, "--registry", str(registry), "install", "acme:png/png-strict")
    assert code == 0
    assert 'include("acme/png/png-strict.fan")' in out
    path = root / "acme" / "png" / "png-strict.fan"
    assert path.read_text() == "<start> ::= 'strict'\n"
    meta = yaml.safe_load(path.with_suffix(".yml").read_text())
    assert meta["registry"] == "acme" and meta["format"] == "png" and meta["kind"] == "png-strict"


def test_the_same_name_in_two_registries_does_not_clash(capsys, registry, added, root):
    run(capsys, "--registry", str(registry), "install", "png", "acme:png")
    assert spec_path(root, "png", "png").read_text() == "<start> ::= 'png'\n"
    assert spec_path(root, "png", "png", "acme").read_text() == "<start> ::= 'acme png'\n"
    assert [str(d) for d in all_installed(root)] == ["png/png", "acme:png/png"]


def test_a_plain_name_never_reaches_a_registry_you_did_not_name(capsys, registry, added, root):
    # png-strict exists only in acme. `install png-strict` must not fetch it from there.
    code, _, err = run(capsys, "--registry", str(registry), "install", "png-strict")
    assert code == 2 and "unknown" in err
    assert "acme:png/png-strict" in err  # a hint, never a fallback
    assert not (root / "png" / "png-strict.fan").exists() and not (root / "acme").exists()


def test_fanbase_colon_names_the_default_registry(capsys, registry, added, root):
    code, out, _ = run(capsys, "--registry", str(registry), "install", "fanbase:png")
    assert code == 0 and spec_path(root, "png", "png").is_file()


def test_an_unknown_registry_says_how_to_add_it(capsys, registry):
    code, _, err = run(capsys, "--registry", str(registry), "install", "nope:png")
    assert code == 2 and "fanbase registry add nope" in err


def test_install_all_from_a_registry(capsys, registry, added, root):
    code, out, _ = run(capsys, "--registry", str(registry), "install", "--all", "--from", "acme")
    assert code == 0 and "3 specs: 3 installed" in out
    assert [str(d) for d in all_installed(root)] == ["acme:bmp/bmp", "acme:png/png", "acme:png/png-strict"]
    assert run(capsys, "--registry", str(registry), "install", "png", "--from", "acme")[0] == 2


def test_a_registry_cannot_take_the_name_of_a_format_that_is_installed(capsys, registry, added, root):
    run(capsys, "--registry", str(registry), "install", "png")  # <root>/png/png.fan
    run(capsys, "--registry", str(registry), "registry", "remove", "acme")
    from fanbase.config import Config, RegistryConfig, save_config

    config = load_config()
    config.registries["png"] = RegistryConfig("png", str(added.resolve()))
    save_config(config)
    code, _, err = run(capsys, "--registry", str(registry), "install", "png:bmp")
    assert code == 2 and "cannot be installed under that name" in err
    assert spec_path(root, "png", "png").is_file()


# --- listing, showing, updating, uninstalling

def test_list_and_show_a_registry_that_was_added(capsys, registry, added, root):
    code, out, _ = run(capsys, "--registry", str(registry), "list", "acme:")
    assert code == 0 and "bmp" in out and "png  2 specs" in out
    code, out, _ = run(capsys, "--registry", str(registry), "list", "acme:png")
    assert "png-strict" in out and "strict png" in out
    code, out, _ = run(capsys, "--registry", str(registry), "show", "acme:png-strict")
    shown = yaml.safe_load(out)
    assert shown["registry"] == "acme" and shown["description"] == "strict png"


def test_list_marks_what_is_installed_per_registry(capsys, registry, added, root):
    run(capsys, "--registry", str(registry), "install", "acme:png-strict")
    _, out, _ = run(capsys, "--registry", str(registry), "list", "acme:png")
    assert "*png-strict" in out and " png " in out
    _, out, _ = run(capsys, "--registry", str(registry), "list", "png")
    assert "*png-strict" not in out and "png-strict" not in out


def test_list_installed_can_be_narrowed_to_a_registry(capsys, registry, added, root):
    run(capsys, "--registry", str(registry), "install", "png", "acme:png-strict", "acme:bmp")
    _, out, _ = run(capsys, "list", "--installed")
    assert "png/png" in out and "acme:png/png-strict" in out and "acme:bmp/bmp" in out
    _, out, _ = run(capsys, "list", "--installed", "acme:")
    assert "acme:bmp/bmp" in out and "  png/png " not in out
    _, out, _ = run(capsys, "list", "--installed", "acme:bmp")
    assert "acme:bmp/bmp" in out and "png-strict" not in out
    _, out, _ = run(capsys, "list", "--installed", "png")
    assert "acme" not in out


def test_update_goes_back_to_the_registry_each_spec_came_from(capsys, registry, added, root):
    run(capsys, "--registry", str(registry), "install", "png", "acme:png-strict")
    (added / "specs/png/png-strict/png-strict.fan").write_text("<start> ::= 'stricter'\n")
    from fanbase.manifest import INDEX_FILENAME, dump_index, reindex
    from fanbase.registry import Registry

    reg = Registry(added)
    rows, _, _ = reindex(reg)
    (added / INDEX_FILENAME).write_text(dump_index(rows, reg.info))
    code, out, _ = run(capsys, "--registry", str(registry), "update")
    assert code == 0
    assert "up to date png/png" in out and "updated acme:png/png-strict" in out
    assert spec_path(root, "png", "png-strict", "acme").read_text() == "<start> ::= 'stricter'\n"


def test_update_notes_a_spec_that_left_its_registry(capsys, registry, added, root):
    run(capsys, "--registry", str(registry), "install", "acme:bmp")
    import shutil

    shutil.rmtree(added / "specs" / "bmp")
    from fanbase.manifest import INDEX_FILENAME, dump_index, reindex
    from fanbase.registry import Registry

    reg = Registry(added)
    rows, _, _ = reindex(reg)
    (added / INDEX_FILENAME).write_text(dump_index(rows, reg.info))
    code, _, err = run(capsys, "--registry", str(registry), "update")
    assert code == 0 and "acme:bmp/bmp is no longer in its registry" in err
    assert spec_path(root, "bmp", "bmp", "acme").is_file()


def test_uninstall_a_spec_of_an_added_registry(capsys, registry, added, root):
    run(capsys, "--registry", str(registry), "install", "png", "acme:png", "acme:png-strict")
    code, out, _ = run(capsys, "uninstall", "acme:png-strict")
    assert code == 0 and "removed acme:png/png-strict" in out
    assert spec_path(root, "png", "png", "acme").is_file() and spec_path(root, "png", "png").is_file()
    run(capsys, "uninstall", "acme:png")
    assert not (root / "acme").exists()  # the registry's directory goes with its last spec
    assert run(capsys, "uninstall", "acme:png")[0] == 2


def test_uninstalling_the_plain_name_does_not_touch_the_other_registrys_spec(capsys, registry, added, root):
    run(capsys, "--registry", str(registry), "install", "acme:png")
    assert run(capsys, "uninstall", "png")[0] == 2
    assert spec_path(root, "png", "png", "acme").is_file()


# --- pins

def test_a_pin_changes_what_a_plain_name_means(capsys, registry, added, root):
    code, out, _ = run(capsys, "--registry", str(registry), "pin", "png", "acme:png/png-strict")
    assert code == 0 and "png now means acme:png/png-strict" in out
    code, out, _ = run(capsys, "--registry", str(registry), "install", "png")
    assert code == 0 and "acme:png/png-strict" in out
    assert not spec_path(root, "png", "png").exists()
    _, out, _ = run(capsys, "pin")
    assert "png -> acme:png/png-strict" in out


def test_a_pin_only_applies_to_the_exact_name(capsys, registry, added, root):
    run(capsys, "--registry", str(registry), "pin", "png", "acme:png-strict")
    run(capsys, "--registry", str(registry), "install", "png/png")
    assert spec_path(root, "png", "png").is_file()  # png/png is not png


def test_unpinning(capsys, registry, added, root):
    run(capsys, "--registry", str(registry), "pin", "png", "acme:png-strict")
    assert run(capsys, "unpin", "png")[0] == 0
    assert run(capsys, "unpin", "png")[0] == 2
    run(capsys, "--registry", str(registry), "install", "png")
    assert spec_path(root, "png", "png").is_file()
    assert "nothing pinned" in run(capsys, "pin")[1]


@pytest.mark.parametrize("args, complaint", [
    (["png"], "pin what to what"),
    (["png", "png-apng"], "registry you added"),
    (["acme:png", "acme:png-strict"], "already names a registry"),
    (["png", "nope:png"], "no registry called"),
    (["png", "acme:nothing-like-it"], "unknown"),
])
def test_bad_pins_are_refused(capsys, registry, added, args, complaint):
    code, _, err = run(capsys, "--registry", str(registry), "pin", *args)
    assert code == 2 and complaint in err
    assert load_config().pins == {}


def test_removing_a_registry_unpins_what_pointed_at_it(capsys, registry, added):
    run(capsys, "--registry", str(registry), "pin", "png", "acme:png-strict")
    _, out, _ = run(capsys, "--registry", str(registry), "registry", "remove", "acme")
    assert "unpinned png" in out and load_config().pins == {}


# --- ensure(), which is what `fandango -F` calls

def test_ensure_takes_a_registry_prefix(registry, acme, added, root):
    done = ensure("acme:png-strict", root=root)
    assert (done.registry, done.status) == ("acme", "installed")
    assert done.path == root / "acme" / "png" / "png-strict.fan"
    assert done.extensions == ["png"] and done.include_path == "acme/png/png-strict.fan"


def test_ensure_follows_a_pin(registry, acme, added, root):
    from fanbase.config import load_config, save_config

    config = load_config()
    config.pins["png"] = "acme:png/png-strict"
    save_config(config)
    assert ensure("png", root=root).path == root / "acme" / "png" / "png-strict.fan"


def test_ensure_falls_back_to_the_installed_copy_of_an_added_registry(registry, acme, added, root, monkeypatch):
    ensure("acme:png-strict", root=root)
    # the registry goes away: the copy that is installed is used
    monkeypatch.setenv(ENV_CONFIG, str(load_config.__globals__["config_path"]()))
    config = load_config()
    config.registries["acme"] = type(config.registries["acme"])("acme", "http://127.0.0.1:9")
    from fanbase.config import save_config

    save_config(config)
    done = ensure("acme:png-strict", root=root)
    assert done.status == "offline" and done.registry == "acme"
    assert installed_copy("acme:png-strict", root).registry == "acme"


def test_ensure_with_an_unknown_registry_is_an_error(registry, root):
    from fanbase.registry import Registry

    with pytest.raises(RegistryError, match="no registry called"):
        ensure("nope:png", Registry(registry), root)


# --- what a registry tells us is not trusted on the terminal

def test_control_characters_from_a_registry_are_not_printed(capsys, registry, root):
    import yaml as _yaml

    meta = registry / "specs/png/png/metadata.yml"
    data = _yaml.safe_load(meta.read_text())
    data["description"] = "nice\x1b[2J\x1b]0;owned\x07 png"
    meta.write_text(_yaml.safe_dump(data))
    from fanbase.manifest import INDEX_FILENAME, dump_index, reindex
    from fanbase.registry import Registry

    rows, _, _ = reindex(Registry(registry))
    (registry / INDEX_FILENAME).write_text(dump_index(rows))
    run(capsys, "--registry", str(registry), "install", "png")
    _, out, _ = run(capsys, "--registry", str(registry), "list", "png")
    assert "\x1b" not in out and "\x07" not in out and "nice" in out
    _, out, _ = run(capsys, "list", "--installed")
    assert "\x1b" not in out and "\x07" not in out


# --- a token for a private registry

def test_a_registry_can_name_the_variable_that_holds_its_token(capsys, registry, acme, monkeypatch):
    monkeypatch.delenv("ACME_TOKEN", raising=False)
    code, _, err = run(capsys, "--registry", str(registry), "registry", "add", str(acme), "--trust", "--token-env", "ACME_TOKEN")
    assert code == 0 and "$ACME_TOKEN is not set" in err
    config = load_config()
    assert config.registries["acme"].token_env == "ACME_TOKEN" and config.registries["acme"].token() is None
    monkeypatch.setenv("ACME_TOKEN", "secret")
    assert load_config().registries["acme"].token() == "secret"
    assert "secret" not in open(load_config.__globals__["config_path"]()).read()  # the file holds the name, never the token


def test_the_token_variable_has_to_be_a_variable_name(capsys, registry, acme):
    from fanbase.config import config_path

    code, _, err = run(capsys, "--registry", str(registry), "registry", "add", str(acme), "--trust", "--token-env", "not a name")
    assert code == 2 or load_config().registries == {}
    config_path().write_text(yaml.safe_dump({"registries": {"x": {"url": "u", "token_env": "no good"}}}))
    with pytest.raises(RegistryError, match="token_env"):
        load_config()


def test_a_broken_config_file_is_an_error_that_names_it(capsys, registry):
    from fanbase.config import config_path

    config_path().write_text("registries: [not, a, mapping]\n")
    code, _, err = run(capsys, "--registry", str(registry), "list")
    assert code == 2
    config_path().write_text(yaml.safe_dump({"pins": {"png": "no-prefix"}}))
    code, _, err = run(capsys, "--registry", str(registry), "list")
    assert code == 2 and "pin 'png'" in err


# --- a registry added by URL

def test_a_registry_added_by_url_installs_over_http(capsys, acme, root, tmp_path):
    import functools
    import http.server
    import threading

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(acme))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        code, out, _ = run(capsys, "registry", "add", url, "--trust")
        assert code == 0 and load_config().registries["acme"].url == url  # named by the index
        code, out, _ = run(capsys, "--registry", str(acme), "install", "acme:png-strict")
        assert code == 0 and spec_path(root, "png", "png-strict", "acme").is_file()
        # and it is checked against the hash in the index
        (acme / "specs/png/png-strict/png-strict.fan").write_text("<start> ::= 'tampered'\n")
        run(capsys, "uninstall", "acme:png-strict")
        code, _, err = run(capsys, "--registry", str(acme), "install", "acme:png-strict")
        assert code == 2 and "does not match index.yml" in err
    finally:
        server.shutdown()
        server.server_close()
