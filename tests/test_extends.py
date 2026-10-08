import sys

import pytest
import yaml
from conftest import write_spec

from fanbase.cli import main
from fanbase.deps import split_constraint
from fanbase.manager import all_installed, ensure, spec_path
from fanbase.manifest import INDEX_FILENAME, dump_index, reindex
from fanbase.registry import Registry, RegistryError


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("FANDANGO_PATH", str(tmp_path / "fandango"))
    return tmp_path / "fandango"


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def build(root, specs, name=None):
    """A registry checkout: {(format, kind): meta}; the text of each spec is its name."""
    for (fmt, kind), meta in specs.items():
        write_spec(root, fmt, kind, f"<start> ::= '{kind}'\n", meta.pop("description", kind), **meta)
    if name:
        (root / "registry.yml").write_text(yaml.safe_dump({"name": name}))
    reg = Registry(root)
    rows, _, _ = reindex(reg)
    (root / INDEX_FILENAME).write_text(dump_index(rows, reg.info))
    return root


@pytest.fixture
def layered(tmp_path):
    """png <- png-apng <- png-ultra, and png-strict, which needs png in 1.x. gif stands alone."""
    return build(tmp_path / "layered", {
        ("png", "png"): dict(version="1.0", extensions=["png"]),
        ("png", "png-apng"): dict(version="1.0", extensions=["png"], extends=["png"]),
        ("png", "png-ultra"): dict(version="0.3", extensions=["png"], extends=["png-apng"]),
        ("png", "png-strict"): dict(version="2.0", extensions=["png"], extends=["png>=1.0,<2"]),
        ("gif", "gif"): dict(version="1.0"),
    })


# --- reading a constraint

@pytest.mark.parametrize("text, ref, spec", [
    ("png", "png", ""),
    ("png>=1.0,<2", "png", "<2,>=1.0"),
    ("png >= 1.0", "png", ">=1.0"),
    ("png@1.2", "png", "==1.2"),
    ("fanbase:png/png-apng~=1.4", "fanbase:png/png-apng", "~=1.4"),
    ("png!=1.1", "png", "!=1.1"),
])
def test_a_version_range_is_split_from_the_name(text, ref, spec):
    got_ref, got_spec = split_constraint(text)
    assert (got_ref, str(got_spec)) == (ref, spec)


@pytest.mark.parametrize("text", ["png>=", "png>>1", "png@", "png==1.0.*.*", "png<=x y"])
def test_a_range_that_cannot_be_read_is_an_error(text):
    with pytest.raises(RegistryError, match="cannot read the version range"):
        split_constraint(text)


# --- installing what a spec extends

def test_what_a_spec_extends_is_installed_first(capsys, layered, root):
    code, out, _ = run(capsys, "--registry", str(layered), "install", "png-apng")
    assert code == 0
    assert out.index("png/png ->") < out.index("png/png-apng ->")
    assert spec_path(root, "png", "png").is_file() and spec_path(root, "png", "png-apng").is_file()
    assert 'include("png/png.fan")' in out and 'include("png/png-apng.fan")' in out


def test_a_chain_is_followed_all_the_way(capsys, layered, root):
    run(capsys, "--registry", str(layered), "install", "png-ultra")
    assert [str(d) for d in all_installed(root)] == ["png/png", "png/png-apng", "png/png-ultra"]


def test_shared_ground_is_installed_once(capsys, layered, root):
    _, out, _ = run(capsys, "--registry", str(layered), "install", "png-apng", "png-strict")
    assert out.count("png/png ->") == 1 and out.count("installed png/png") == 3


def test_install_all_follows_extends_too(capsys, layered, root):
    _, out, _ = run(capsys, "--registry", str(layered), "install", "--all")
    assert "5 specs: 5 installed" in out


def test_ensure_installs_what_the_spec_extends(layered, root, pip_calls):
    done = ensure("png-ultra", Registry(layered), root)
    assert str(done) == "png/png-ultra"
    assert [str(d) for d in all_installed(root)] == ["png/png", "png/png-apng", "png/png-ultra"]


def test_ensure_installs_the_requirements_of_what_it_extends(layered, root, pip_calls):
    (layered / "specs/png/png/png.fan").write_text(
        "import fanbase_test_module_that_is_not_installed\n<start> ::= 'png'\n"
    , newline="\n")
    rows, _, _ = reindex(Registry(layered))
    (layered / INDEX_FILENAME).write_text(dump_index(rows))
    ensure("png-apng", Registry(layered), root)
    assert pip_calls == [[sys.executable, "-m", "pip", "install", "fanbase_test_module_that_is_not_installed"]]


def test_update_brings_what_a_spec_extends_up_to_date(capsys, layered, root):
    run(capsys, "--registry", str(layered), "install", "png-apng")
    (layered / "specs/png/png/png.fan").write_text("<start> ::= 'new png'\n", newline="\n")
    rows, _, _ = reindex(Registry(layered))
    (layered / INDEX_FILENAME).write_text(dump_index(rows))
    _, out, _ = run(capsys, "--registry", str(layered), "update")
    assert "updated png/png" in out and "up to date png/png-apng" in out


# --- version ranges

def test_a_range_that_the_registry_cannot_meet_installs_nothing(capsys, tmp_path, root):
    # Not through reindex, which would refuse it: a registry that was published like this.
    reg = build(tmp_path / "r", {("png", "png"): dict(version="1.0"), ("png", "png-x"): dict(version="1.0", extends=["png"])})
    meta = reg / "specs/png/png-x/metadata.yml"
    data = yaml.safe_load(meta.read_text())
    data["extends"] = ["png>=2"]
    meta.write_text(yaml.safe_dump(data))
    index = yaml.safe_load((reg / INDEX_FILENAME).read_text())
    next(row for row in index["specs"] if row["kind"] == "png-x")["extends"] = ["png>=2"]
    (reg / INDEX_FILENAME).write_text(yaml.safe_dump(index))
    code, _, err = run(capsys, "--registry", str(reg), "install", "png-x")
    assert code == 2 and "png/png-x: png>=2 needs png/png in >=2, but it is at 1.0" in err
    assert "tag" in err  # and says where an older version may be found
    assert all_installed(root) == []


def test_a_range_on_a_ref_is_a_check(capsys, layered, root):
    assert run(capsys, "--registry", str(layered), "install", "png>=1.0,<2")[0] == 0
    assert run(capsys, "--registry", str(layered), "install", "png@1.0")[0] == 0
    code, _, err = run(capsys, "--registry", str(layered), "install", "png>=2")
    assert code == 2 and "png>=2 needs png/png in >=2, but it is at 1.0" in err
    assert run(capsys, "--registry", str(layered), "install", "png@1.1")[0] == 2
    assert run(capsys, "--registry", str(layered), "install", "png>=")[0] == 2


def test_ensure_checks_a_range_even_offline(layered, root, monkeypatch):
    ensure("png", Registry(layered), root)
    monkeypatch.setenv("FANBASE_REGISTRY", "http://127.0.0.1:9")
    assert ensure("png>=1.0", root=root).status == "offline"
    with pytest.raises(RegistryError, match="png>=2 needs png/png in >=2, but it is at 1.0"):
        ensure("png>=2", root=root)


def test_a_spec_without_a_version_cannot_satisfy_a_range(capsys, tmp_path):
    reg = build(tmp_path / "r", {("png", "png"): dict(description="x")})
    code, _, err = run(capsys, "--registry", str(reg), "install", "png>=1")
    assert code == 2 and "it has no version" in err


# --- what reindex refuses

def reindex_errors(capsys, registry):
    code, _, err = run(capsys, "--registry", str(registry), "reindex")
    assert code == 2
    return err


def set_extends(registry, kind, extends):
    fmt = kind.split("-")[0]
    path = registry / "specs" / fmt / kind / "metadata.yml"
    data = yaml.safe_load(path.read_text())
    data["extends"] = extends
    path.write_text(yaml.safe_dump(data))


@pytest.mark.parametrize("extends, complaint", [
    (["nope"], "png/png-apng: extends nope: unknown format or spec: nope"),
    (["png-apng"], "png/png-apng: extends itself"),
    (["png>=3"], "png>=3 needs png/png in >=3, but it is at 1.0"),
    (["png>="], "cannot read the version range"),
    ([""], "is not a spec name"),
    ([5], "is not a spec name"),
    (["acme:png"], "the default registry's specs can only extend its own"),
    (["png@x.y.z"], "cannot read the version range|needs png/png"),
])
def test_reindex_refuses_an_extends_that_cannot_work(capsys, layered, extends, complaint):
    set_extends(layered, "png-apng", extends)
    err = reindex_errors(capsys, layered)
    assert "invalid metadata" in err
    import re

    assert re.search(complaint, err)


def test_reindex_refuses_a_circle_and_names_it_once(capsys, layered):
    set_extends(layered, "png", ["png-ultra"])  # png <- png-apng <- png-ultra <- png
    err = reindex_errors(capsys, layered)
    assert err.count("extend each other in a circle") == 1
    assert "png/png -> png/png-ultra -> png/png-apng -> png/png" in err


def test_a_default_registry_may_say_fanbase_colon_for_itself(capsys, layered):
    set_extends(layered, "png-apng", ["fanbase:png"])
    assert run(capsys, "--registry", str(layered), "reindex")[0] == 0


def test_version_has_to_be_a_version_number(capsys, layered):
    for bad in (1.0, "one", "1.x", None):
        path = layered / "specs/png/png/metadata.yml"
        data = yaml.safe_load(path.read_text())
        data["version"] = bad
        path.write_text(yaml.safe_dump(data))
        err = reindex_errors(capsys, layered)
        assert "png/png: version" in err and "in quotes" in err


def test_the_check_notices_a_range_that_stopped_being_met(capsys, layered):
    path = layered / "specs/png/png/metadata.yml"
    data = yaml.safe_load(path.read_text())
    data["version"] = "2.0"  # png-strict wants png<2
    path.write_text(yaml.safe_dump(data))
    code, _, err = run(capsys, "--registry", str(layered), "reindex", "--check")
    assert code == 2 and "png/png-strict: extends png>=1.0,<2" in err


# --- a registry of someone else's

@pytest.fixture
def mine(tmp_path, layered, capsys):
    """The `layered` registry is the default; an `acme` registry extends its png."""
    reg = build(tmp_path / "acme", {
        ("png", "png-acme"): dict(version="1.0", extends=["fanbase:png>=1.0"]),
        ("png", "png-twice"): dict(version="1.0", extends=["acme:png-acme", "png-acme"]),
    }, name="acme")
    assert run(capsys, "--registry", str(layered), "registry", "add", str(reg), "--trust")[0] == 0
    return reg


def test_a_third_party_spec_can_extend_the_default_registrys(capsys, layered, mine, root):
    code, out, _ = run(capsys, "--registry", str(layered), "install", "acme:png-twice")
    assert code == 0
    assert [str(d) for d in all_installed(root)] == ["png/png", "acme:png/png-acme", "acme:png/png-twice"]
    assert 'include("acme/png/png-acme.fan")' in out


def test_the_default_registry_cannot_extend_a_third_party_one(capsys, layered, mine):
    set_extends(layered, "png-apng", ["acme:png-acme"])
    assert "can only extend its own" in reindex_errors(capsys, layered)


def test_a_third_party_registry_cannot_name_a_third_registry(capsys, tmp_path, layered):
    reg = build(tmp_path / "acme2", {("png", "png-x"): dict(version="1.0")}, name="acme2")
    set_extends(reg, "png-x", ["other:png"])
    err = reindex_errors(capsys, reg)
    assert "can extend its own and the default registry's" in err


def test_a_third_party_registry_is_indexed_with_its_own_name_as_itself(capsys, mine):
    # `fanbase:png` is the default registry's, which this checkout cannot look up: not an error.
    assert run(capsys, "--registry", str(mine), "reindex", "--check")[0] == 0


def test_the_alias_a_user_gives_a_registry_still_finds_its_own_name(capsys, tmp_path, layered, root):
    reg = build(tmp_path / "acme", {
        ("png", "png-acme"): dict(version="1.0"),
        ("png", "png-twice"): dict(version="1.0", extends=["acme:png-acme"]),
    }, name="acme")
    assert run(capsys, "--registry", str(layered), "registry", "add", str(reg), "--trust", "--name", "ours")[0] == 0
    code, out, _ = run(capsys, "--registry", str(layered), "install", "ours:png-twice")
    assert code == 0
    assert [str(d) for d in all_installed(root)] == ["ours:png/png-acme", "ours:png/png-twice"]


# --- circles that got past reindex

def test_a_circle_in_a_published_index_is_an_error_not_a_hang(capsys, tmp_path, root):
    import functools
    import http.server
    import threading

    reg = build(tmp_path / "r", {("png", "png"): dict(version="1.0"), ("png", "png-x"): dict(version="1.0")})
    index = yaml.safe_load((reg / INDEX_FILENAME).read_text())
    for row in index["specs"]:
        row["extends"] = ["png-x"] if row["kind"] == "png" else ["png"]
    (reg / INDEX_FILENAME).write_text(yaml.safe_dump(index))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(reg))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        code, _, err = run(capsys, "--registry", url, "install", "png")
        assert code == 2 and "extend each other in a circle: png/png -> png/png-x -> png/png" in err
        assert all_installed(root) == []
        code, out, _ = run(capsys, "--registry", url, "deps", "png")
        assert code == 0 and "(circular)" in out
    finally:
        server.shutdown()
        server.server_close()


# --- not removing what others stand on

def test_a_spec_that_others_extend_is_not_uninstalled(capsys, layered, root):
    run(capsys, "--registry", str(layered), "install", "png-ultra")
    code, _, err = run(capsys, "uninstall", "png")
    assert code == 2 and "png/png is extended by png/png-apng" in err and "--force" in err
    assert spec_path(root, "png", "png").is_file()


def test_removing_a_spec_with_what_extends_it_is_fine(capsys, layered, root):
    run(capsys, "--registry", str(layered), "install", "png-ultra")
    assert run(capsys, "uninstall", "png", "png-apng", "png-ultra")[0] == 0
    assert all_installed(root) == []


def test_removing_the_top_first_then_the_rest(capsys, layered, root):
    run(capsys, "--registry", str(layered), "install", "png-ultra")
    assert run(capsys, "uninstall", "png-ultra")[0] == 0
    assert run(capsys, "uninstall", "png")[0] == 2  # png-apng is still there
    assert run(capsys, "uninstall", "png-apng")[0] == 0
    assert run(capsys, "uninstall", "png")[0] == 0


def test_force_removes_it_anyway(capsys, layered, root):
    run(capsys, "--registry", str(layered), "install", "png-apng")
    assert run(capsys, "uninstall", "png", "--force")[0] == 0
    assert [str(d) for d in all_installed(root)] == ["png/png-apng"]


def test_a_third_party_spec_holds_up_the_default_registrys(capsys, layered, mine, root):
    run(capsys, "--registry", str(layered), "install", "acme:png-acme")
    code, _, err = run(capsys, "uninstall", "png")
    assert code == 2 and "png/png is extended by acme:png/png-acme" in err


# --- looking at the layers

def test_deps_shows_what_a_spec_stands_on(capsys, layered):
    code, out, _ = run(capsys, "--registry", str(layered), "deps", "png-ultra")
    assert code == 0
    assert out.splitlines() == [
        "png/png-ultra  0.3",
        "└── png/png-apng  1.0",
        "    └── png/png  1.0",
    ]


def test_deps_reverse_shows_what_stands_on_a_spec(capsys, layered):
    code, out, _ = run(capsys, "--registry", str(layered), "deps", "png", "--reverse")
    assert code == 0
    assert out.splitlines() == [
        "png/png  1.0",
        "├── png/png-apng  1.0",
        "│   └── png/png-ultra  0.3",
        "└── png/png-strict  2.0",
    ]


def test_deps_reverse_looks_into_the_registries_you_added(capsys, layered, mine):
    _, out, _ = run(capsys, "--registry", str(layered), "deps", "png", "--reverse")
    assert "acme:png/png-acme  1.0" in out and "acme:png/png-twice  1.0" in out


def test_a_spec_that_stands_on_nothing(capsys, layered):
    _, out, _ = run(capsys, "--registry", str(layered), "deps", "gif")
    assert out.splitlines() == ["gif/gif  1.0"]


def test_deps_says_what_cannot_be_found(capsys, tmp_path):
    reg = build(tmp_path / "r", {("png", "png"): dict(version="1.0")})
    index = yaml.safe_load((reg / INDEX_FILENAME).read_text())
    index["specs"][0]["extends"] = ["ghost"]
    meta = reg / "specs/png/png/metadata.yml"
    data = yaml.safe_load(meta.read_text())
    data["extends"] = ["ghost"]
    meta.write_text(yaml.safe_dump(data))
    _, out, _ = run(capsys, "--registry", str(reg), "deps", "png")
    assert "ghost  (cannot be found: unknown format or spec: ghost)" in out
