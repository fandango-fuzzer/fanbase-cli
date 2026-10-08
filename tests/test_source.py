import pytest

from fanbase.manifest import INDEX_FILENAME
from fanbase.registry import RegistryError
from fanbase.source import DEFAULT_REGISTRY, Location, locate_registry


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.delenv("FANBASE_REGISTRY", raising=False)
    monkeypatch.chdir(tmp_path)


def checkout(root):
    (root / "specs").mkdir(parents=True)
    return str(root)


def test_the_public_registry_is_the_default():
    assert locate_registry() == Location("url", DEFAULT_REGISTRY)


def test_a_checkout_around_the_current_directory_is_found(tmp_path, monkeypatch):
    root = tmp_path / "reg"
    checkout(root)
    (root / INDEX_FILENAME).write_text("specs: []\n")
    inside = root / "specs"
    monkeypatch.chdir(inside)
    assert locate_registry() == Location("path", str(root))


def test_the_flag_comes_before_the_environment(tmp_path, monkeypatch):
    path = checkout(tmp_path / "flag")
    monkeypatch.setenv("FANBASE_REGISTRY", "https://example.org/env")
    # An explicit path used to lose to a URL in the environment.
    assert locate_registry(path) == Location("path", path)
    monkeypatch.setenv("FANBASE_REGISTRY", checkout(tmp_path / "env"))
    assert locate_registry("https://example.org/flag") == Location("url", "https://example.org/flag")


def test_the_environment_comes_before_the_current_directory(tmp_path, monkeypatch):
    here = tmp_path / "here"
    checkout(here)
    (here / INDEX_FILENAME).write_text("specs: []\n")
    monkeypatch.chdir(here)
    monkeypatch.setenv("FANBASE_REGISTRY", "https://example.org/env")
    assert locate_registry() == Location("url", "https://example.org/env")


def test_a_path_without_specs_is_an_error(tmp_path):
    with pytest.raises(RegistryError, match="has no specs/ directory"):
        locate_registry(str(tmp_path))


def test_a_maintainer_command_wants_a_checkout(tmp_path):
    with pytest.raises(RegistryError, match="not a URL"):
        locate_registry("https://example.org/x", local=True)
    with pytest.raises(RegistryError, match="no registry checkout found"):
        locate_registry(local=True)
    checkout(tmp_path / "fresh")  # no index.yml yet: fine for a maintainer
    assert locate_registry(str(tmp_path / "fresh"), local=True).kind == "path"
