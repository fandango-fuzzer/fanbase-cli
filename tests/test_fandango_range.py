import importlib.metadata
import logging

import pytest
import yaml

from fanbase import manager
from fanbase.cli import main
from fanbase.manager import ensure, fandango_mismatch
from fanbase.registry import Registry


def installed_fandango(monkeypatch, version):
    """Pretend this version of Fandango (or none, with None) is installed."""
    real = importlib.metadata.version

    def fake(name):
        if name == manager.FANDANGO_DISTRIBUTION:
            if version is None:
                raise importlib.metadata.PackageNotFoundError(name)
            return version
        return real(name)

    monkeypatch.setattr(manager.importlib.metadata, "version", fake)


def require(registry, kind, range_):
    fmt = kind.split("-")[0]
    path = registry / "specs" / fmt / kind / "metadata.yml"
    data = yaml.safe_load(path.read_text())
    data["fandango"] = range_
    path.write_text(yaml.safe_dump(data))


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("FANDANGO_PATH", str(tmp_path / "fandango"))
    return tmp_path / "fandango"


@pytest.mark.parametrize("have, range_, ok", [
    ("1.3.0", ">=1.3", True),
    ("1.3.1", ">=1.3", True),
    ("2.0", ">=1.3", True),
    ("1.2.0", ">=1.3", False),
    ("1.3.0rc1", ">=1.3.0rc1", True),
    ("1.4", ">=1.3,<1.4", False),
    ("1.3.9", ">=1.3,<1.4", True),
])
def test_the_installed_fandango_is_compared_with_the_range(monkeypatch, have, range_, ok):
    installed_fandango(monkeypatch, have)
    problem = fandango_mismatch({"fandango": range_})
    assert (problem is None) is ok
    if not ok:
        assert range_ in problem and have in problem


def test_nothing_is_said_without_fandango_or_without_a_range(monkeypatch):
    installed_fandango(monkeypatch, None)
    assert fandango_mismatch({"fandango": ">=99"}) is None
    installed_fandango(monkeypatch, "1.0")
    assert fandango_mismatch({}) is None
    assert fandango_mismatch({"fandango": ""}) is None


def test_an_unreadable_range_is_reported_not_raised(monkeypatch):
    installed_fandango(monkeypatch, "1.3")
    assert "cannot read" in fandango_mismatch({"fandango": "newer than 1.3 or so"})


def test_install_warns_and_still_installs(capsys, registry, root, monkeypatch):
    installed_fandango(monkeypatch, "1.2.0")
    require(registry, "png", ">=1.3")
    require(registry, "gif", ">=1.0")
    code = main(["--registry", str(registry), "install", "png", "gif"])
    out = capsys.readouterr()
    assert code == 0
    assert "installed png/png" in out.out
    assert "warning: png/png is written for fandango >=1.3" in out.err
    assert "fandango 1.2.0 is installed" in out.err
    assert "gif/gif" not in out.err  # 1.2.0 suits its range
    assert (root / "png" / "png.fan").is_file()


def test_install_all_and_update_warn_too(capsys, registry, root, monkeypatch):
    installed_fandango(monkeypatch, "1.2.0")
    main(["--registry", str(registry), "install", "--all"])
    assert capsys.readouterr().err.count("warning:") == 3
    main(["--registry", str(registry), "update"])
    assert capsys.readouterr().err.count("warning:") == 3


def test_no_warning_when_fandango_suits(capsys, registry, root, monkeypatch):
    installed_fandango(monkeypatch, "1.3.0")
    main(["--registry", str(registry), "install", "--all"])
    assert "warning" not in capsys.readouterr().err


def test_ensure_logs_a_warning_for_fandango_dash_f(registry, root, monkeypatch, caplog):
    installed_fandango(monkeypatch, "1.2.0")
    with caplog.at_level(logging.WARNING, logger="fanbase"):
        done = ensure("png", Registry(registry), root)
    assert done.status == "installed"  # a warning, not a refusal
    assert "Fanbase: png/png is written for fandango >=1.3" in caplog.text
