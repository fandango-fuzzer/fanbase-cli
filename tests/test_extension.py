"""The files evaluate and check ask Fandango for have the extension of their format, as they do under `fandango -F`."""

import subprocess

import pytest
import yaml
from conftest import build_registry
from helpers import REAL_RUN, STRICT, FakeFandango, settle, target

from fanbase import contrib, quality
from fanbase.cli import main


@pytest.fixture(autouse=True)
def real_subprocess(monkeypatch):
    monkeypatch.setattr(subprocess, "run", REAL_RUN)


@pytest.fixture
def fake(monkeypatch):
    fandango = FakeFandango()
    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", fandango)
    monkeypatch.setattr("fanbase.evaluate.find_fandango", lambda: "/fake/fandango")
    return fandango


@pytest.fixture
def reg(tmp_path):
    root = build_registry(tmp_path / "reg", {
        ("png", "png"): dict(version="1.0", description="PNG", extensions=["png"]),
        ("jpeg", "jpeg"): dict(version="1.0", description="JPEG", extensions=[".jpg", "jpeg"]),
        ("txt", "txt"): dict(version="1.0", description="Plain text"),
    })
    target(root, "strict", STRICT, formats=("png", "jpeg", "txt"))
    (root / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["strict"]}))  # evaluate needs a target to judge by
    settle(root)
    return root


def asked(fake, capsys, reg, spec, *extra):
    fake.calls.clear()
    code = main(["--registry", str(reg), "check", spec, "--no-requirements", *extra])
    capsys.readouterr()
    assert code == 0 and fake.calls
    return fake.calls[0]["cmd"]


def test_fandango_is_asked_for_files_with_the_extension_of_the_format(fake, capsys, reg):
    cmd = asked(fake, capsys, reg, "png")
    assert cmd[cmd.index("-x") + 1] == ".png"


def test_the_first_extension_is_used_and_a_dot_in_front_of_it_is_not_doubled(fake, capsys, reg):
    cmd = asked(fake, capsys, reg, "jpeg")
    assert cmd[cmd.index("-x") + 1] == ".jpg"


def test_a_format_without_an_extension_gets_none_asked_for(fake, capsys, reg):
    assert "-x" not in asked(fake, capsys, reg, "txt")


def test_with_a_budget_every_batch_asks_for_the_extension(fake, capsys, reg):
    # evaluate works in batches within a budget; each of them is a run of fandango fuzz
    fake.calls.clear()
    code = main(["--registry", str(reg), "evaluate", "png", "-n", "20", "--budget", "30"])
    capsys.readouterr()
    assert code in (0, 1) and fake.calls
    assert all(call["cmd"][call["cmd"].index("-x") + 1] == ".png" for call in fake.calls)


@pytest.mark.parametrize("per_second, shown", [(163.35, "163"), (41.2, "41"), (10.0, "10"), (9.96, "10.0"), (1.36, "1.4"), (1.0, "1.0"),
                                              (0.9, "0.90"), (0.05, "0.05")])
def test_a_slow_spec_does_not_read_as_zero(per_second, shown):
    assert quality.speed(per_second) == shown
    assert quality.summary({"per_second": per_second}).endswith(f"{shown}/s")
