import os
import subprocess

import pytest
from conftest import build_registry, republish

from fanbase import contrib, publish
from fanbase.cli import main
from fanbase.publish import changed_specs

# The suite replaces subprocess.run for every test (see conftest); this is the real one.
REAL_RUN = subprocess.run

IDENTITY = {"GIT_AUTHOR_NAME": "Test User", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test User", "GIT_COMMITTER_EMAIL": "test@example.com"}


@pytest.fixture(autouse=True)
def real_git(monkeypatch):
    monkeypatch.setattr(subprocess, "run", REAL_RUN)
    monkeypatch.setattr(contrib, "git_user", lambda: "Test User")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for key, value in IDENTITY.items():
        monkeypatch.setenv(key, value)


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def clone(tmp_path):
    """A registry checkout that is a clone of a remote, with its first commit pushed."""
    origin = tmp_path / "origin.git"
    sh("git", "init", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    sh("git", "clone", "-q", str(origin), str(work))
    build_registry(work, {
        ("png", "png"): dict(version="1.0", extensions=["png"], description="PNG", authors=["a"], license="MIT"),
        ("gif", "gif"): dict(version="1.0", description="GIF", authors=["a"], license="MIT"),
    })
    sh("git", "add", "-A", cwd=work)
    sh("git", "commit", "-q", "-m", "first", cwd=work)
    sh("git", "push", "-q", "-u", "origin", "main", cwd=work)
    return work


def remote_branches(clone):
    return sh("git", "ls-remote", "--heads", "origin", cwd=clone)


# --- reading git's listing

@pytest.mark.parametrize("status, added, updated, removed", [
    ("?? specs/gif/gif-new/gif-new.fan\0?? specs/gif/gif-new/metadata.yml\0 M index.yml\0", ["gif/gif-new"], [], []),
    (" M specs/png/png/png.fan\0 M specs/png/png/metadata.yml\0", [], ["png/png"], []),
    (" M specs/png/png/metadata.yml\0", [], ["png/png"], []),
    (" D specs/png/png/png.fan\0 D specs/png/png/metadata.yml\0", [], [], ["png/png"]),
    ("A  specs/gif/x/x.fan\0A  specs/gif/x/metadata.yml\0", ["gif/x"], [], []),
    ("?? specs/a/a/a.fan\0 M specs/b/b/b.fan\0 D specs/c/c/c.fan\0", ["a/a"], ["b/b"], ["c/c"]),
    ("R  specs/png/png-b/png-b.fan\0specs/png/png-a/png-a.fan\0", [], ["png/png-b"], []),
    (" M index.yml\0 M registry.yml\0", [], [], []),
    ("", [], [], []),
])
def test_git_status_into_added_updated_removed(status, added, updated, removed):
    plan = changed_specs(status)
    assert (plan.added, plan.updated, plan.removed) == (added, updated, removed)


# --- with a real git

def test_publish_makes_a_branch_commits_and_pushes(capsys, clone):
    run(capsys, "--registry", str(clone), "new", "gif-animated", "--description", "Animated GIFs")
    (clone / "notes.txt").write_text("not part of the change\n")
    before = sh("git", "rev-parse", "main", cwd=clone)

    code, out, _ = run(capsys, "--registry", str(clone), "publish", "--yes", "--no-pr", "--no-generate")
    assert code == 0
    assert "add     gif/gif-animated" in out and "pushed fanbase/add-gif-gif-animated" in out
    assert "refs/heads/fanbase/add-gif-gif-animated" in remote_branches(clone)
    assert sh("git", "branch", "--show-current", cwd=clone) == "fanbase/add-gif-gif-animated"

    assert sh("git", "log", "-1", "--format=%s", cwd=clone) == "Add gif/gif-animated"
    assert sh("git", "log", "-1", "--format=%an <%ae> | %cn <%ce>", cwd=clone) == "Test User <test@example.com> | Test User <test@example.com>"
    message = sh("git", "log", "-1", "--format=%B", cwd=clone).lower()
    assert "claude" not in message and "co-authored" not in message
    changed = sh("git", "show", "--name-only", "--format=", "HEAD", cwd=clone).splitlines()
    assert sorted(changed) == ["index.yml", "specs/gif/gif-animated/gif-animated.fan", "specs/gif/gif-animated/metadata.yml"]
    assert "?? notes.txt" in sh("git", "status", "--porcelain", cwd=clone)  # what is not the registry's is left alone
    assert sh("git", "rev-parse", "main", cwd=clone) == before  # and main is where it was


def test_publish_an_update_and_a_removal(capsys, clone):
    republish(clone, "png", "<start> ::= 'png 2'\n", version="1.1")
    import shutil

    shutil.rmtree(clone / "specs" / "gif")
    republish(clone, "png")
    code, out, _ = run(capsys, "--registry", str(clone), "publish", "--yes", "--no-pr", "--no-generate")
    assert code == 0 and "update  png/png" in out and "remove  gif/gif" in out
    assert sh("git", "log", "-1", "--format=%s", cwd=clone) == "Specs: update 1, remove 1"


def test_publish_stays_on_the_branch_you_are_on(capsys, clone):
    sh("git", "switch", "-q", "-c", "my-work", cwd=clone)
    run(capsys, "--registry", str(clone), "new", "gif-animated", "--description", "x")
    code, out, _ = run(capsys, "--registry", str(clone), "publish", "--yes", "--no-pr", "--no-generate", "--message", "Animated GIFs")
    assert code == 0 and "my-work (the one you are on)" in out
    assert "refs/heads/my-work" in remote_branches(clone)
    assert sh("git", "log", "-1", "--format=%s", cwd=clone) == "Animated GIFs"


def test_publish_dry_run_commits_nothing(capsys, clone):
    run(capsys, "--registry", str(clone), "new", "gif-animated", "--description", "x")
    code, out, _ = run(capsys, "--registry", str(clone), "publish", "--dry-run", "--no-generate")
    assert code == 0 and "publish plan:" in out and "(dry run: nothing was done)" in out
    assert sh("git", "branch", "--show-current", cwd=clone) == "main"
    assert sh("git", "rev-list", "--count", "HEAD", cwd=clone) == "1"
    assert "fanbase/" not in remote_branches(clone)


def test_publish_asks_first(capsys, clone, monkeypatch):
    import io
    import sys

    run(capsys, "--registry", str(clone), "new", "gif-animated", "--description", "x")
    code, _, err = run(capsys, "--registry", str(clone), "publish", "--no-generate")
    assert code == 2 and "pass --yes" in err

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Terminal(""))
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    code, _, err = run(capsys, "--registry", str(clone), "publish", "--no-generate")
    assert code == 2 and "not published" in err
    assert sh("git", "branch", "--show-current", cwd=clone) == "main" and "fanbase/" not in remote_branches(clone)
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    assert run(capsys, "--registry", str(clone), "publish", "--no-pr", "--no-generate")[0] == 0


def test_publish_with_nothing_changed(capsys, clone):
    code, _, err = run(capsys, "--registry", str(clone), "publish", "--yes", "--no-generate")
    assert code == 2 and "nothing to publish" in err


def test_publish_needs_a_git_checkout_with_an_origin(capsys, tmp_path):
    plain = build_registry(tmp_path / "plain", {("gif", "gif"): dict(version="1.0", description="x")})
    code, _, err = run(capsys, "--registry", str(plain), "publish", "--yes")
    assert code == 2 and "is not a git checkout" in err
    sh("git", "init", "-q", "-b", "main", str(plain))
    code, _, err = run(capsys, "--registry", str(plain), "publish", "--yes")
    assert code == 2 and "no remote called origin" in err


def test_publish_does_not_publish_what_check_would_refuse(capsys, clone, monkeypatch):
    run(capsys, "--registry", str(clone), "new", "gif-animated", "--description", "x")
    real = contrib._run

    def fandango_fails(cmd, **kwargs):
        if cmd[0] == "/fake/fandango":
            return subprocess.CompletedProcess(cmd, 1, "", "no good")
        return real(cmd, **kwargs)

    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", fandango_fails)
    code, _, err = run(capsys, "--registry", str(clone), "publish", "--yes", "--no-pr")
    assert code == 2 and "not publishing" in err and "fandango failed (1): no good" in err
    assert sh("git", "branch", "--show-current", cwd=clone) == "main" and "fanbase/" not in remote_branches(clone)


# --- the pull request

@pytest.fixture
def gh(monkeypatch):
    """A `gh` that is installed and opens pull requests; everything else is run for real."""
    calls = []
    real = contrib._run

    def run_(cmd, **kwargs):
        if cmd[0] == "gh":
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, "https://github.com/o/r/pull/7\n", "")
        return real(cmd, **kwargs)

    monkeypatch.setattr(contrib, "_run", run_)
    monkeypatch.setattr(publish.shutil, "which", lambda name: "/fake/gh" if name == "gh" else None)
    return calls


def test_publish_opens_the_pull_request(capsys, clone, gh):
    run(capsys, "--registry", str(clone), "new", "gif-animated", "--description", "Animated GIFs")
    code, out, _ = run(capsys, "--registry", str(clone), "publish", "--yes", "--no-generate")
    assert code == 0 and out.strip().endswith("https://github.com/o/r/pull/7")
    (cmd, kwargs), = gh
    assert cmd[:3] == ["gh", "pr", "create"]
    flags = dict(zip(cmd[3::2], cmd[4::2]))
    assert flags["--base"] == "main" and flags["--head"] == "fanbase/add-gif-gif-animated" and flags["--title"] == "Add gif/gif-animated"
    body = flags["--body"]
    assert "- Add `gif/gif-animated`" in body and "fanbase check" in body and "Opened with `fanbase publish`." in body
    assert "claude" not in body.lower() and kwargs["cwd"] == str(clone)


def test_publish_without_gh_says_where_to_open_the_pull_request(capsys, clone, monkeypatch):
    monkeypatch.setattr(publish.shutil, "which", lambda name: None)
    run(capsys, "--registry", str(clone), "new", "gif-animated", "--description", "x")
    code, out, _ = run(capsys, "--registry", str(clone), "publish", "--yes", "--no-generate")
    assert code == 0 and "gh is not installed" in out and "open one from fanbase/add-gif-gif-animated" in out
    assert "refs/heads/fanbase/add-gif-gif-animated" in remote_branches(clone)


def test_a_failing_push_is_an_error_that_says_why(capsys, clone, monkeypatch):
    run(capsys, "--registry", str(clone), "new", "gif-animated", "--description", "x")
    sh("git", "remote", "set-url", "origin", "/nonexistent/repo.git", cwd=clone)
    code, _, err = run(capsys, "--registry", str(clone), "publish", "--yes", "--no-pr", "--no-generate")
    assert code == 2 and "git push failed" in err
