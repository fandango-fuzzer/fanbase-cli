import shutil
import subprocess
import sys

import pytest
import yaml

from fanbase import manager
from fanbase.cli import main
from fanbase.manager import (
    RequirementsError,
    declared_requirements,
    ensure,
    install,
    install_requirements,
    requirements_to_install,
)
from fanbase.registry import Registry

MISSING = "fanbase_test_module_that_is_not_installed"


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("FANDANGO_PATH", str(tmp_path / "fandango"))
    return tmp_path / "fandango"


def needs(registry, kind, **meta):
    """Add keys to a spec's metadata.yml, as a registry maintainer would."""
    fmt = kind.split("-")[0]
    path = registry / "specs" / fmt / kind / "metadata.yml"
    data = yaml.safe_load(path.read_text())
    data.update(meta)
    path.write_text(yaml.safe_dump(data))


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_what_is_missing_comes_from_requires():
    assert requirements_to_install({}) == []
    assert requirements_to_install({"requires": ["json", "os.path"]}) == []
    assert requirements_to_install({"requires": ["json", MISSING]}) == [MISSING]


def test_a_pip_list_replaces_the_module_names():
    meta = {"requires": ["yaml"], "pip": ["pyyaml>=6", "not-installed-anywhere>=1.0"]}
    # yaml is importable, but pip says what to install: pyyaml is there, the other is not
    assert requirements_to_install(meta) == ["not-installed-anywhere>=1.0"]


@pytest.mark.parametrize("spec", [
    "pyyaml>=6", "a", "b>=2,<3", "numpy[extra]==1.2.*", "zope.interface~=5.0", "name-with_all.chars>=1.0.post1",
])
def test_plain_package_requirements_are_accepted(spec):
    assert declared_requirements({"pip": [spec]}) == [spec]


@pytest.mark.parametrize("spec", [
    "--index-url=http://example.org/simple",
    "--extra-index-url http://example.org/simple",
    "-r requirements.txt",
    "-e .",
    "git+https://example.org/x.git",
    "pkg @ https://example.org/pkg.zip",
    "https://example.org/pkg.whl",
    "./local/pkg",
    "/abs/pkg",
    "pkg; python_version > '3'",
    "pkg --hash=sha256:00",
    "two words",
    "pkg\n--index-url=http://example.org/simple",
    "",
    5,
    None,
])
def test_anything_but_a_package_requirement_is_refused(spec):
    with pytest.raises(RequirementsError, match="refusing requirement"):
        declared_requirements({"pip": [spec]})


@pytest.mark.parametrize("module", ["--index-url=x", "a b", "x;y", "pkg>=1", "git+https://x", "", 5])
def test_a_module_name_has_to_be_a_module_name(module):
    with pytest.raises(RequirementsError, match="refusing requirement"):
        declared_requirements({"requires": [module]})


def test_a_dotted_module_asks_for_its_top_level_package():
    # `numpy.random` is a module of numpy; asking pip for a package of that name is a different one.
    assert declared_requirements({"requires": ["numpy.random", "numpy.linalg", "yaml"]}) == ["numpy", "yaml"]
    assert requirements_to_install({"requires": [MISSING + ".sub"]}) == [MISSING]


def test_pip_is_used_when_there_is_pip(pip_calls):
    install_requirements(["a", "b>=2"])
    assert pip_calls == [[sys.executable, "-m", "pip", "install", "a", "b>=2"]]


def test_nothing_is_run_for_nothing(pip_calls):
    install_requirements([])
    assert pip_calls == []


def test_uv_is_used_when_there_is_no_pip(pip_calls, monkeypatch):
    real = manager.importlib.util.find_spec
    monkeypatch.setattr(manager.importlib.util, "find_spec", lambda n, *a: None if n == "pip" else real(n, *a))
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/uv" if name == "uv" else None)
    install_requirements(["a"])
    assert pip_calls == [["/opt/uv", "pip", "install", "--python", sys.executable, "a"]]


def test_neither_pip_nor_uv_is_an_error(monkeypatch):
    real = manager.importlib.util.find_spec
    monkeypatch.setattr(manager.importlib.util, "find_spec", lambda n, *a: None if n == "pip" else real(n, *a))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(RequirementsError, match="neither pip nor uv"):
        install_requirements(["a"])


def test_a_failing_pip_is_an_error_that_says_why(monkeypatch):
    def failing(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "ERROR: no matching distribution for a")

    monkeypatch.setattr(manager.subprocess, "run", failing)
    with pytest.raises(RequirementsError, match="no matching distribution"):
        install_requirements(["a"])


def test_install_installs_what_a_spec_needs(capsys, registry, root, pip_calls):
    needs(registry, "png-apng", requires=[MISSING])
    code, out, _ = run(capsys, "--registry", str(registry), "install", "png", "png-apng")
    assert code == 0
    assert pip_calls == [[sys.executable, "-m", "pip", "install", MISSING]]
    assert f"installed requirement {MISSING}" in out


def test_install_does_not_ask_for_what_is_there(capsys, registry, root, pip_calls):
    needs(registry, "png-apng", requires=["json"])
    run(capsys, "--registry", str(registry), "install", "png-apng")
    assert pip_calls == []


def test_no_requirements_only_says_what_is_needed(capsys, registry, root, pip_calls):
    needs(registry, "png-apng", requires=[MISSING])
    code, out, _ = run(capsys, "--registry", str(registry), "install", "png-apng", "--no-requirements")
    assert code == 0 and pip_calls == []
    assert f"requires: pip install {MISSING}" in out


def test_install_all_installs_every_requirement_once(capsys, registry, root, pip_calls):
    needs(registry, "png-apng", requires=[MISSING])
    needs(registry, "gif", requires=[MISSING], pip=["somepackage-for-tests>=1.0"])
    code, out, _ = run(capsys, "--registry", str(registry), "install", "--all")
    assert code == 0
    assert out.count("installed requirement") == 2
    assert sorted(c[-1] for c in pip_calls) == sorted([MISSING, "somepackage-for-tests>=1.0"])


def test_update_installs_what_a_newer_spec_needs(capsys, registry, root, pip_calls):
    run(capsys, "--registry", str(registry), "install", "png")
    assert pip_calls == []
    needs(registry, "png", requires=[MISSING])
    code, out, _ = run(capsys, "--registry", str(registry), "update", "png")
    assert code == 0 and pip_calls == [[sys.executable, "-m", "pip", "install", MISSING]]


def test_a_failed_install_of_requirements_is_an_error(capsys, registry, root, monkeypatch):
    needs(registry, "png", requires=[MISSING])
    monkeypatch.setattr(
        manager.subprocess, "run",
        lambda command, **kw: subprocess.CompletedProcess(command, 1, "", "ERROR: boom"),
    )
    code, _, err = run(capsys, "--registry", str(registry), "install", "png")
    assert code == 2 and "could not install" in err and "boom" in err


def test_ensure_installs_requirements_by_default(registry, root, pip_calls):
    needs(registry, "png-apng", requires=[MISSING])
    done = ensure("png-apng", Registry(registry), root)
    assert done.status == "installed"
    assert pip_calls == [[sys.executable, "-m", "pip", "install", MISSING]]


def test_install_refuses_an_unsafe_requirement_and_runs_no_pip(capsys, registry, root, pip_calls):
    needs(registry, "png-apng", pip=["--index-url=http://example.org/simple"])
    code, _, err = run(capsys, "--registry", str(registry), "install", "png-apng")
    assert code == 2
    assert "png/png-apng" in err and "refusing requirement" in err
    assert pip_calls == []


def test_ensure_refuses_an_unsafe_requirement_and_runs_no_pip(registry, root, pip_calls):
    # This is the path `fandango -F` takes.
    needs(registry, "png-apng", pip=["git+https://example.org/x.git"])
    with pytest.raises(RequirementsError, match="refusing requirement"):
        ensure("png-apng", Registry(registry), root)
    assert pip_calls == []


def test_the_hint_never_prints_an_unsafe_requirement(capsys, registry, root):
    # With --no-requirements the user is told what to pip install, and may paste it.
    needs(registry, "png-apng", pip=["pkg; echo hacked"])
    code, out, err = run(capsys, "--registry", str(registry), "install", "png-apng", "--no-requirements")
    assert code == 2
    assert "echo hacked" not in out
    assert "refusing requirement" in err


def test_ensure_can_leave_requirements_alone(registry, root, pip_calls):
    needs(registry, "png-apng", requires=[MISSING])
    ensure("png-apng", Registry(registry), root, requirements=False)
    assert pip_calls == []


def test_ensure_installs_requirements_for_the_offline_copy(registry, root, pip_calls, monkeypatch):
    needs(registry, "png-apng", requires=[MISSING])
    install(Registry(registry), Registry(registry).resolve("png-apng"), root)
    monkeypatch.setenv("FANBASE_REGISTRY", "http://127.0.0.1:9")
    done = ensure("png-apng", root=root)
    assert done.status == "offline"
    assert pip_calls == [[sys.executable, "-m", "pip", "install", MISSING]]
