import pytest

from fanbase.manager import ensure, install, installed_copy, installed_specs, spec_path
from fanbase.registry import Registry, RegistryError, RegistryUnavailable
from fanbase.remote import RemoteRegistry


@pytest.fixture(params=["local", "remote"])
def reg(request, registry, served):
    return Registry(registry) if request.param == "local" else RemoteRegistry(served)


def test_install_then_current(reg, tmp_path):
    root = tmp_path / "fandango"
    first = install(reg, reg.resolve("png"), root)
    assert first.status == "installed"
    assert first.path == root / "png" / "png.fan"
    assert first.path.read_text() == "<start> ::= 'png'\n"
    assert install(reg, reg.resolve("png"), root).status == "current"


def test_install_updates_a_changed_spec(registry, tmp_path):
    root = tmp_path / "fandango"
    reg = Registry(registry)
    install(reg, reg.resolve("png"), root)
    (registry / "specs/png/png/png.fan").write_text("<start> ::= 'new'\n")
    again = install(reg, reg.resolve("png"), root)
    assert again.status == "updated"
    assert again.path.read_text() == "<start> ::= 'new'\n"


def test_metadata_is_kept_next_to_the_spec(reg, tmp_path):
    root = tmp_path / "fandango"
    install(reg, reg.resolve("png-apng"), root)
    have = installed_copy("png-apng", root)
    assert have.extensions == ["png"]
    assert have.meta["description"] == "animated png"


def test_installed_specs(reg, tmp_path):
    root = tmp_path / "fandango"
    install(reg, reg.resolve("gif"), root)
    assert [str(e) for e in installed_specs(reg, root)] == ["gif/gif"]


def test_ensure_uses_the_registry(served, tmp_path, monkeypatch):
    monkeypatch.setenv("FANBASE_REGISTRY", served)
    done = ensure("png-apng", root=tmp_path / "fandango")
    assert (done.format, done.kind, done.status) == ("png", "png-apng", "installed")


def test_ensure_falls_back_to_the_installed_copy(served, tmp_path, monkeypatch):
    root = tmp_path / "fandango"
    monkeypatch.setenv("FANBASE_REGISTRY", served)
    ensure("png", root=root)
    monkeypatch.setenv("FANBASE_REGISTRY", "http://127.0.0.1:9")  # nothing listens here
    done = ensure("png", root=root)
    assert done.status == "offline"
    assert done.extensions == ["png"]


def test_ensure_offline_without_a_copy_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("FANBASE_REGISTRY", "http://127.0.0.1:9")
    with pytest.raises(RegistryUnavailable):
        ensure("png", root=tmp_path / "fandango")


def test_ensure_unknown_spec_is_not_masked_by_an_installed_copy(served, tmp_path, monkeypatch):
    monkeypatch.setenv("FANBASE_REGISTRY", served)
    with pytest.raises(RegistryError):
        ensure("png/nope", root=tmp_path / "fandango")
