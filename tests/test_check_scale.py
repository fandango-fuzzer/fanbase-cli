"""`fanbase check -j N` and `fanbase check --changed --base`: a check that does not grow with the registry."""

import pathlib
import shutil
import subprocess
import threading
import time

import pytest
from conftest import build_registry, republish

from fanbase import contrib
from fanbase.cli import main


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


class SlowFandango:
    """Stands in for `fandango fuzz`: takes a moment for each spec, and keeps count of how many are in progress."""

    def __init__(self, delay=0.15, fail=()):
        self.delay, self.fail = delay, set(fail)
        self.lock = threading.Lock()
        self.now = self.most = 0
        self.specs = []

    def __call__(self, cmd, **kwargs):
        if cmd[0] != "/fake/fandango":
            return subprocess.run(cmd, **kwargs)
        spec = pathlib.Path(cmd[cmd.index("-f") + 1]).stem
        with self.lock:
            self.now += 1
            self.most = max(self.most, self.now)
            self.specs.append(spec)
        try:
            time.sleep(self.delay)
            if spec in self.fail:
                return subprocess.CompletedProcess(cmd, 1, "", f"{spec} did not converge")
            out = pathlib.Path(cmd[cmd.index("-d") + 1])
            out.mkdir(parents=True)
            (out / "fandango-0000.txt").write_text("x")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        finally:
            with self.lock:
                self.now -= 1


@pytest.fixture
def reg(tmp_path):
    return build_registry(tmp_path / "reg", {
        ("png", "png"): dict(version="1.0", description="d", authors=["a"], license="l"),
        ("png", "png-apng"): dict(version="1.0", description="d", authors=["a"], license="l", extends=["png"]),
        ("png", "png-anim"): dict(version="1.0", description="d", authors=["a"], license="l", extends=["png-apng"]),
        ("gif", "gif"): dict(version="1.0", description="d", authors=["a"], license="l"),
        ("bmp", "bmp"): dict(version="1.0", description="d", authors=["a"], license="l"),
        ("tiff", "tiff"): dict(version="1.0", description="d", authors=["a"], license="l"),
    })


@pytest.fixture
def before(tmp_path, reg):
    shutil.copytree(reg, tmp_path / "before")
    return tmp_path / "before"


def slow(monkeypatch, **kwargs):
    fandango = SlowFandango(**kwargs)
    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", fandango)
    return fandango


# --- -j

def test_by_default_one_spec_at_a_time(capsys, reg, monkeypatch):
    fandango = slow(monkeypatch)
    code, out, _ = run(capsys, "--registry", str(reg), "check")
    assert code == 0 and "6 specs looked at, 6 produced inputs; nothing wrong" in out and fandango.most == 1


def test_with_jobs_several_at_a_time_and_faster(capsys, reg, monkeypatch):
    fandango = slow(monkeypatch, delay=0.3)
    started = time.monotonic()
    code, out, _ = run(capsys, "--registry", str(reg), "check", "-j", "6")
    took = time.monotonic() - started
    assert code == 0 and "6 specs looked at, 6 produced inputs; nothing wrong" in out
    assert fandango.most >= 3 and took < 6 * 0.3 * 0.8, (fandango.most, took)  # (not one after the other)


def test_jobs_are_not_more_than_asked(capsys, reg, monkeypatch):
    fandango = slow(monkeypatch)
    run(capsys, "--registry", str(reg), "check", "-j", "2")
    assert 1 <= fandango.most <= 2


def test_the_report_is_the_same_whatever_the_jobs(capsys, reg, monkeypatch):
    slow(monkeypatch, fail=("png-apng", "gif"))
    _, one, _ = run(capsys, "--registry", str(reg), "check", "-j", "1")
    _, many, _ = run(capsys, "--registry", str(reg), "check", "-j", "6")
    failed = lambda text: [line for line in text.splitlines() if line.startswith("FAILED:")]  # noqa: E731
    assert failed(one) == failed(many) == [
        "FAILED:  gif/gif: fandango failed (1): gif did not converge",
        "FAILED:  png/png-apng: fandango failed (1): png-apng did not converge",
    ]
    assert "6 specs looked at, 4 produced inputs; 2 problem(s)" in many


def test_every_progress_line_is_whole(capsys, reg, monkeypatch):
    slow(monkeypatch, delay=0.05)
    code, out, _ = run(capsys, "--registry", str(reg), "check", "-j", "6")
    progress = [line for line in out.splitlines() if line.startswith("  ok")]
    assert code == 0 and sorted(progress) == sorted(
        f"  ok      {s}" for s in ["bmp/bmp", "gif/gif", "png/png", "png/png-anim", "png/png-apng", "tiff/tiff"])


def test_packages_are_installed_one_spec_at_a_time(capsys, reg, monkeypatch):
    slow(monkeypatch, delay=0.05)
    for fmt, kind in (("png", "png"), ("gif", "gif"), ("bmp", "bmp"), ("tiff", "tiff")):
        (reg / "specs" / fmt / kind / f"{kind}.fan").write_text(f"import fanbase_test_module_{kind}\n<start> ::= '{kind}'\n", newline="\n")
        republish(reg, kind)
    state = {"now": 0, "most": 0, "calls": 0}
    lock = threading.Lock()

    def installing(requirements):
        with lock:
            state["now"] += 1
            state["calls"] += 1
            state["most"] = max(state["most"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1

    monkeypatch.setattr(contrib, "install_requirements", installing)
    code, _, _ = run(capsys, "--registry", str(reg), "check", "-j", "6")
    assert code == 0 and state["calls"] >= 4 and state["most"] == 1  # (never two pips at once)


# --- --changed

def test_changed_asks_only_about_what_changed_and_what_builds_on_it(capsys, reg, before, monkeypatch):
    fandango = slow(monkeypatch, delay=0.01)
    republish(reg, "png", "<start> ::= 'png 2'\n", version="1.1")
    code, out, _ = run(capsys, "--registry", str(reg), "check", "--changed", "--base", str(before))
    assert code == 0 and sorted(set(fandango.specs)) == ["png", "png-anim", "png-apng"]  # not gif, bmp or tiff
    assert "note: --changed: 3 of 6 specs are new, different from the base, or build on one that is" in out
    assert "3 specs looked at, 3 produced inputs; nothing wrong" in out


def test_a_new_spec_is_checked_and_nothing_else(capsys, reg, before, monkeypatch):
    fandango = slow(monkeypatch, delay=0.01)
    run(capsys, "--registry", str(reg), "new", "gif-animated", "--description", "Animated")
    code, out, _ = run(capsys, "--registry", str(reg), "check", "--changed", "--base", str(before), "--no-generate")
    assert code == 0 and "1 specs looked at" in out and fandango.specs == []
    run(capsys, "--registry", str(reg), "check", "--changed", "--base", str(before))
    assert set(fandango.specs) == {"gif-animated"}


def test_nothing_changed_is_said_and_is_fine(capsys, reg, before, monkeypatch):
    fandango = slow(monkeypatch)
    code, out, _ = run(capsys, "--registry", str(reg), "check", "--changed", "--base", str(before))
    assert code == 0 and "no spec differs from the base, and none builds on one that does" in out
    assert "0 specs looked at, 0 produced inputs; nothing wrong" in out and fandango.specs == []


def test_changed_still_looks_at_the_whole_registrys_order(capsys, reg, before, monkeypatch):
    slow(monkeypatch)
    (reg / "specs/gif/gif/gif.fan").write_text("<start> ::= 'gif quietly'\n", newline="\n")  # not reindexed; and not what changed in the PR's eyes
    code, out, _ = run(capsys, "--registry", str(reg), "check", "--changed", "--base", str(before), "--no-generate")
    assert code == 1 and "index.yml is out of date" in out


def test_changed_still_wants_a_new_version(capsys, reg, before, monkeypatch):
    slow(monkeypatch, delay=0.01)
    republish(reg, "gif", "<start> ::= 'gif changed'\n")
    code, out, _ = run(capsys, "--registry", str(reg), "check", "--changed", "--base", str(before))
    assert code == 1 and "FAILED:  gif/gif: changed since" in out and "still at version 1.0" in out


def test_changed_needs_a_base_and_does_not_go_with_names(capsys, reg, before, monkeypatch):
    slow(monkeypatch)
    code, _, err = run(capsys, "--registry", str(reg), "check", "--changed")
    assert code == 2 and "--changed needs --base" in err
    code, _, err = run(capsys, "--registry", str(reg), "check", "--changed", "--base", str(before), "png")
    assert code == 2 and "not both" in err


def test_changed_and_jobs_together(capsys, reg, before, monkeypatch):
    fandango = slow(monkeypatch, delay=0.05)
    republish(reg, "png", "<start> ::= 'png 2'\n", version="1.1")
    republish(reg, "gif", "<start> ::= 'gif 2'\n", version="1.1")
    code, out, _ = run(capsys, "--registry", str(reg), "check", "--changed", "--base", str(before), "-j", "4")
    assert code == 0 and sorted(set(fandango.specs)) == ["gif", "png", "png-anim", "png-apng"]
    assert "4 specs looked at, 4 produced inputs; nothing wrong" in out
