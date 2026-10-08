import pytest
import yaml
from conftest import build_registry, republish

from fanbase.cli import main
from fanbase.manager import install
from fanbase.manifest import INDEX_FILENAME
from fanbase.registry import Registry, RegistryError


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def format_yml(reg, fmt, **info):
    (reg / "specs" / fmt / "format.yml").write_text(yaml.safe_dump(info))


def target(reg, name):
    folder = reg / "targets" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "target.yml").write_text(yaml.safe_dump({"formats": ["png"], "run": ["true", "{file}"]}))


def raw(reg, kind):
    return yaml.safe_load((reg / "specs" / kind.split("-")[0] / kind / "metadata.yml").read_text())


def index_row(reg, kind):
    index = yaml.safe_load((reg / INDEX_FILENAME).read_text())
    return next(row for row in index["specs"] if row["kind"] == kind)


@pytest.fixture
def reg(tmp_path):
    root = build_registry(tmp_path / "reg", {
        ("png", "png"): dict(version="1.0", description="PNG", extensions=["png"]),  # says its extensions itself
        ("png", "png-apng"): dict(version="1.0", description="APNG"),  # leaves them to the format
        ("gif", "gif"): dict(version="1.0", description="GIF"),
    })
    target(root, "pillow")
    target(root, "ffprobe")
    format_yml(root, "png", title="Portable Network Graphics", mime="image/png", extensions=["png"],
               reference="https://www.w3.org/TR/png-3/", targets=["pillow"])
    return root


def refresh(capsys, reg):
    assert run(capsys, "--registry", str(reg), "reindex")[0] == 0


# --- a spec is what the format says, and what it says itself

def test_a_spec_has_what_its_format_says(reg):
    entry = Registry(reg).resolve("png-apng")
    assert entry.meta["title"] == "Portable Network Graphics" and entry.meta["mime"] == "image/png"
    assert entry.meta["extensions"] == ["png"] and entry.meta["targets"] == ["pillow"]
    assert entry.own is not None and "title" not in entry.own  # and its own metadata.yml says none of it


def test_what_a_spec_says_itself_wins(reg):
    republish(reg, "png-apng", title="Animated PNG", targets=["ffprobe"], extensions=["apng", "png"])
    meta = Registry(reg).resolve("png-apng").meta
    assert (meta["title"], meta["targets"], meta["extensions"]) == ("Animated PNG", ["ffprobe"], ["apng", "png"])
    assert meta["mime"] == "image/png"  # the rest is still the format's


def test_a_format_without_format_yml_is_as_before(reg):
    meta = Registry(reg).resolve("gif").meta
    assert "title" not in meta and "targets" not in meta


def test_a_format_folder_with_only_a_format_yml_is_not_a_format(tmp_path, reg):
    (reg / "specs" / "bmp").mkdir()
    format_yml(reg, "bmp", title="Bitmap")
    assert Registry(reg).formats() == ["gif", "png"]


# --- reindex keeps the two apart

def test_reindex_does_not_copy_the_formats_words_into_every_spec(capsys, reg):
    refresh(capsys, reg)
    assert "title" not in raw(reg, "png-apng") and "mime" not in raw(reg, "png-apng") and "targets" not in raw(reg, "png-apng")
    refresh(capsys, reg)
    assert "title" not in raw(reg, "png-apng")  # also the second time


def test_the_index_says_everything_that_is_true_of_a_spec(capsys, reg):
    refresh(capsys, reg)
    row = index_row(reg, "png-apng")
    assert row["title"] == "Portable Network Graphics" and row["mime"] == "image/png"
    assert row["extensions"] == ["png"] and row["targets"] == ["pillow"]
    assert "title" not in index_row(reg, "gif")


def test_a_client_that_has_only_the_index_gets_the_formats_words_too(capsys, reg):
    import functools
    import http.server
    import threading

    refresh(capsys, reg)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(reg))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        code, out, _ = run(capsys, "--registry", f"http://127.0.0.1:{server.server_address[1]}", "show", "png-apng", "--json")
        import json

        shown = json.loads(out)
        assert code == 0 and shown["extensions"] == ["png"] and shown["title"] == "Portable Network Graphics"
    finally:
        server.shutdown()
        server.server_close()


def test_what_is_installed_knows_its_extensions_though_the_spec_does_not_say(capsys, reg, tmp_path):
    refresh(capsys, reg)
    done = install(Registry(reg), Registry(reg).resolve("png-apng"), tmp_path / "lib")
    assert done.extensions == ["png"]  # which is what `fandango -F` names the files by


def test_reindex_is_stable(capsys, reg):
    refresh(capsys, reg)
    code, out, _ = run(capsys, "--registry", str(reg), "reindex", "--check")
    assert code == 0


def test_a_change_to_the_format_changes_the_index(capsys, reg):
    refresh(capsys, reg)
    format_yml(reg, "png", title="PNG, renamed", mime="image/png", extensions=["png"], targets=["pillow"])
    assert run(capsys, "--registry", str(reg), "reindex", "--check")[0] == 1
    refresh(capsys, reg)
    assert index_row(reg, "png-apng")["title"] == "PNG, renamed"


# --- what is refused

@pytest.mark.parametrize("info, complaint", [
    ({"title": 5}, "title has to be text"),
    ({"mime": ""}, "mime has to be text"),
    ({"reference": ["x"]}, "reference has to be text"),
    ({"extensions": "png"}, "extensions has to be a list"),
    ({"extensions": []}, "extensions has to be a list"),
    ({"extensions": [1]}, "extensions has to be a list"),
    ({"targets": "pillow"}, "targets has to be a list of target names"),
    ({"targets": ["../x"]}, "targets has to be a list of target names"),
    ({"targets": ["pillow", "pillow"]}, "each once"),
])
def test_a_format_yml_of_the_wrong_shape(capsys, reg, info, complaint):
    format_yml(reg, "png", **info)
    code, _, err = run(capsys, "--registry", str(reg), "reindex")
    assert code == 2 and complaint in err and "format.yml" in err


def test_a_format_yml_that_is_not_a_mapping(capsys, reg):
    (reg / "specs/png/format.yml").write_text("- a\n- list\n")
    code, _, err = run(capsys, "--registry", str(reg), "reindex")
    assert code == 2 and "expected a mapping" in err


def test_a_target_the_registry_does_not_define(capsys, reg):
    format_yml(reg, "png", targets=["pillow", "pilow"])
    code, _, err = run(capsys, "--registry", str(reg), "reindex")
    assert code == 2 and "png/format.yml: targets pilow not defined in targets/" in err


def test_a_spec_may_not_name_a_target_that_does_not_exist_either(capsys, reg):
    republish_raw = reg / "specs/png/png-apng/metadata.yml"
    data = yaml.safe_load(republish_raw.read_text())
    data["targets"] = ["ghost"]
    republish_raw.write_text(yaml.safe_dump(data))
    code, _, err = run(capsys, "--registry", str(reg), "reindex")
    assert code == 2 and "png/png-apng: targets ghost not defined in targets/" in err


def test_targets_without_any_targets_folder(capsys, tmp_path):
    root = build_registry(tmp_path / "r", {("png", "png"): dict(version="1.0", description="x")})
    format_yml(root, "png", targets=["pillow"])
    code, _, err = run(capsys, "--registry", str(root), "reindex")
    assert code == 2 and "not defined in targets/" in err


@pytest.mark.parametrize("value", ["sometimes", True, 1, None])
def test_decodes_is_one_of_four_words(capsys, reg, value):
    path = reg / "specs/png/png-apng/metadata.yml"
    data = yaml.safe_load(path.read_text())
    data["decodes"] = value
    path.write_text(yaml.safe_dump(data))
    code, _, err = run(capsys, "--registry", str(reg), "reindex")
    assert code == 2 and "decodes:" in err and "always, mostly, rarely, never" in err


def test_decodes_and_targets_are_kept_in_their_place(capsys, reg):
    republish(reg, "png-apng", decodes="mostly", targets=["pillow"])
    keys = list(raw(reg, "png-apng"))
    assert keys.index("requires") < keys.index("decodes") < keys.index("targets")  # after the generated keys, in a fixed order
    assert raw(reg, "png-apng")["decodes"] == "mostly"


# --- new specs

def test_new_does_not_copy_what_the_format_already_says(capsys, reg, monkeypatch):
    from fanbase import contrib

    monkeypatch.setattr(contrib, "git_user", lambda: None)
    run(capsys, "--registry", str(reg), "new", "png-fancy", "--extends", "png", "--description", "Fancy")
    assert "extensions" not in raw(reg, "png-fancy")
    assert Registry(reg).resolve("png-fancy").meta["extensions"] == ["png"]
