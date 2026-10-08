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


# --- many specs with the same problem are said once

@pytest.fixture
def many(tmp_path):
    from conftest import build_registry

    return build_registry(tmp_path / "many", {(f"f{i}", f"f{i}"): dict(version="1.0") for i in range(6)})


def warnings_of(err):
    return [line for line in err.splitlines() if line.startswith("warning:")]


def test_more_than_three_specs_with_one_problem_are_said_once(capsys, many, root, monkeypatch):
    installed_fandango(monkeypatch, "1.2.0")
    main(["--registry", str(many), "install", "--all"])
    lines = warnings_of(capsys.readouterr().err)
    assert lines == ["warning: 6 specs are written for fandango >=1.3, and fandango 1.2.0 is installed (f0/f0, f1/f1, f2/f2, ...)"]


def test_three_or_fewer_are_named_one_by_one(capsys, many, root, monkeypatch):
    installed_fandango(monkeypatch, "1.2.0")
    main(["--registry", str(many), "install", "f0", "f1", "f2"])
    lines = warnings_of(capsys.readouterr().err)
    assert lines == [f"warning: f{i}/f{i} is written for fandango >=1.3, and fandango 1.2.0 is installed" for i in range(3)]


def test_different_problems_are_said_separately(capsys, many, root, monkeypatch):
    installed_fandango(monkeypatch, "1.2.0")
    require(many, "f0", ">=2.0")
    for i in range(1, 6):
        require(many, f"f{i}", ">=1.3")
    main(["--registry", str(many), "install", "--all"])
    lines = warnings_of(capsys.readouterr().err)
    assert sorted(lines) == sorted([
        "warning: f0/f0 is written for fandango >=2.0, and fandango 1.2.0 is installed",
        "warning: 5 specs are written for fandango >=1.3, and fandango 1.2.0 is installed (f1/f1, f2/f2, f3/f3, ...)",
    ])


def test_an_unreadable_range_has_a_plural_too(capsys, many, root, monkeypatch):
    installed_fandango(monkeypatch, "1.2.0")
    for i in range(6):
        require(many, f"f{i}", "newer than 1.3 or so")
    main(["--registry", str(many), "install", "--all"])
    assert warnings_of(capsys.readouterr().err) == [
        "warning: 6 specs cannot read their fandango version range 'newer than 1.3 or so' (f0/f0, f1/f1, f2/f2, ...)"
    ]


def test_the_singular_texts_are_what_they_were():
    from fanbase.manager import FandangoMismatch

    assert FandangoMismatch(">=1.3", "1.2.0").describe() == "is written for fandango >=1.3, and fandango 1.2.0 is installed"
    assert FandangoMismatch("x").describe() == "cannot read its fandango version range 'x'"
