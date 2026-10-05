import pytest

from fanbase.registry import Registry, RegistryError
from fanbase.remote import RemoteRegistry


@pytest.fixture(params=["local", "remote"])
def reg(request, registry, served):
    return Registry(registry) if request.param == "local" else RemoteRegistry(served)


def test_formats_and_kinds(reg):
    assert reg.formats() == ["gif", "png"]
    assert [e.kind for e in reg.kinds("png")] == ["png", "png-apng"]


def test_resolve_default_kind(reg):
    assert str(reg.resolve("png")) == "png/png"


def test_resolve_explicit_and_bare_kind(reg):
    assert str(reg.resolve("png/png-apng")) == "png/png-apng"
    assert str(reg.resolve("png-apng")) == "png/png-apng"


def test_resolve_unknown(reg):
    with pytest.raises(RegistryError):
        reg.resolve("nope")
    with pytest.raises(RegistryError):
        reg.resolve("png/nope")


def test_metadata_is_exposed(reg):
    entry = reg.resolve("png-apng")
    assert entry.meta["description"] == "animated png"
    assert entry.requires == []


def test_remote_checks_hash(registry, served):
    reg = RemoteRegistry(served)
    entry = reg.resolve("gif")
    (registry / entry.path).write_text("<start> ::= 'tampered'\n")
    with pytest.raises(RegistryError, match="does not match"):
        reg.read(entry)
