import hashlib
import os
import shutil
import subprocess

import pytest
import yaml
from conftest import build_registry, republish

from fanbase import contrib
from fanbase.bases import cache_dir, load_base, save_base, sha256
from fanbase.cli import main
from fanbase.manifest import INDEX_FILENAME

# The suite replaces subprocess.run for every test (see conftest); this is the real one.
REAL_RUN = subprocess.run


@pytest.fixture(autouse=True)
def real_git(monkeypatch):
    monkeypatch.setattr(subprocess, "run", REAL_RUN)
    monkeypatch.setattr(contrib, "git_user", lambda: "Ada Lovelace")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "Test User")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "test@example.com")


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def spec_text(lines=None):
    """A spec of ten lines you can change one at a time, so that changes either meet or do not."""
    lines = lines or {}
    body = [lines.get(i, f"# line {i}") for i in range(1, 11)]
    return "\n".join(body) + "\n<start> ::= 'png'\n"


def put(reg, kind, text):
    fmt = kind.split("-")[0]
    (reg / "specs" / fmt / kind / f"{kind}.fan").write_bytes(text.encode() if isinstance(text, str) else text)
    republish(reg, kind)


def meta(reg, kind):
    return yaml.safe_load((reg / "specs" / kind.split("-")[0] / kind / "metadata.yml").read_text())


def fan(reg, kind):
    return (reg / "specs" / kind.split("-")[0] / kind / f"{kind}.fan").read_text()


@pytest.fixture
def up(tmp_path):
    """The registry the original is in."""
    reg = build_registry(tmp_path / "up", {("png", "png"): dict(version="1.0", extensions=["png"], description="PNG")})
    put(reg, "png", spec_text())
    return reg


@pytest.fixture
def mine(tmp_path):
    return build_registry(tmp_path / "mine", {("gif", "gif"): dict(version="1.0", description="GIF")}, name="mine")


@pytest.fixture
def forked(capsys, up, mine):
    """png forked into `mine` as png-mine, untouched."""
    assert run(capsys, "--registry", str(up), "fork", "png", "--as", "png-mine", "--into", str(mine))[0] == 0
    return mine


def rebase(capsys, mine, up, *extra):
    return run(capsys, "--registry", str(mine), "rebase", "png-mine", "--upstream", str(up), *extra)


# --- what fork keeps for it

def test_fork_records_the_hash_of_the_original_and_keeps_a_copy(forked, up):
    digest = sha256((up / "specs/png/png/png.fan").read_bytes())
    assert meta(forked, "png-mine")["derived_sha256"] == digest
    assert load_base(digest) == (up / "specs/png/png/png.fan").read_bytes()
    assert (cache_dir() / "bases" / f"{digest}.fan").is_file()


def test_fork_writes_the_file_as_it_is(capsys, up, mine):
    crlf = b"# windows\r\n<start> ::= 'png'\r\n"
    put(up, "png", crlf)
    run(capsys, "--registry", str(up), "fork", "png", "--as", "png-crlf", "--into", str(mine))
    assert (mine / "specs/png/png-crlf/png-crlf.fan").read_bytes() == crlf
    assert meta(mine, "png-crlf")["derived_sha256"] == hashlib.sha256(crlf).hexdigest()


def test_a_kept_copy_is_only_used_if_it_is_what_its_name_says(tmp_path):
    digest = save_base(b"the original")
    (cache_dir() / "bases" / f"{digest}.fan").write_bytes(b"something else")
    assert load_base(digest) is None
    assert load_base("0" * 64) is None


def test_derived_sha256_has_to_be_a_hash(capsys, forked):
    path = forked / "specs/png/png-mine/metadata.yml"
    data = yaml.safe_load(path.read_text())
    data["derived_sha256"] = "not a hash"
    path.write_text(yaml.safe_dump(data))
    code, _, err = run(capsys, "--registry", str(forked), "reindex")
    assert code == 2 and "derived_sha256: expected the 64 hex digits" in err


# --- merging

def test_changes_in_different_places_are_both_kept(capsys, forked, up):
    put(forked, "png-mine", spec_text({2: "# line 2, mine"}))
    put(up, "png", spec_text({9: "# line 9, theirs"}))
    republish(up, "png", version="1.1")
    code, out, _ = rebase(capsys, forked, up)
    assert code == 0
    assert "png/png-mine onto fanbase:png/png@1.1, which it was made from at 1.0" in out
    assert "common ancestor: the copy kept when it was forked" in out and "merged cleanly" in out
    merged = fan(forked, "png-mine")
    assert "# line 2, mine" in merged and "# line 9, theirs" in merged and "<<<<" not in merged
    m = meta(forked, "png-mine")
    assert m["derived_from"] == "fanbase:png/png@1.1"
    assert m["derived_sha256"] == sha256((up / "specs/png/png/png.fan").read_bytes())
    assert "give the fork a new `version`" in out
    assert run(capsys, "--registry", str(forked), "check", "--no-generate")[0] == 0  # and the registry is in order


def test_a_fork_nobody_changed_just_takes_the_new_original(capsys, forked, up):
    put(up, "png", spec_text({9: "# line 9, theirs"}))
    republish(up, "png", version="1.1")
    code, out, _ = rebase(capsys, forked, up)
    assert code == 0 and "you had changed nothing" in out
    assert fan(forked, "png-mine") == fan(up, "png")


def test_when_nothing_changed_there_is_nothing_to_do(capsys, forked, up):
    before = fan(forked, "png-mine")
    code, out, _ = rebase(capsys, forked, up)
    assert code == 0 and "png/png-mine is up to date with fanbase:png/png@1.0" in out
    assert fan(forked, "png-mine") == before


def test_a_second_rebase_starts_from_the_first(capsys, forked, up):
    put(forked, "png-mine", spec_text({2: "# line 2, mine"}))
    put(up, "png", spec_text({9: "# line 9, theirs"}))
    republish(up, "png", version="1.1")
    assert rebase(capsys, forked, up)[0] == 0
    put(up, "png", spec_text({9: "# line 9, theirs", 6: "# line 6, theirs too"}))
    republish(up, "png", version="1.2")
    code, out, _ = rebase(capsys, forked, up)
    assert code == 0 and "made from at 1.1" in out and "merged cleanly" in out
    merged = fan(forked, "png-mine")
    assert "# line 2, mine" in merged and "# line 9, theirs" in merged and "# line 6, theirs too" in merged


# --- conflicts

def conflicting(forked, up):
    put(forked, "png-mine", spec_text({5: "# line 5, mine"}))
    put(up, "png", spec_text({5: "# line 5, theirs"}))
    republish(up, "png", version="1.1")


def test_changes_in_the_same_place_are_a_conflict_to_fix(capsys, forked, up):
    conflicting(forked, up)
    code, out, _ = rebase(capsys, forked, up)
    assert code == 1
    assert "1 conflict(s): look for <<<<<<< in specs/png/png-mine/png-mine.fan" in out
    merged = fan(forked, "png-mine")
    assert "<<<<<<< png-mine.fan (yours)" in merged and "# line 5, mine" in merged
    assert "# line 5, theirs" in merged and ">>>>>>> fanbase:png/png@1.1 (now)" in merged
    assert "fix the conflicts" in out
    assert meta(forked, "png-mine")["derived_from"] == "fanbase:png/png@1.1"  # the next rebase starts from the new original


def test_check_refuses_a_spec_with_a_conflict_left_in_it(capsys, forked, up):
    conflicting(forked, up)
    rebase(capsys, forked, up)
    code, out, _ = run(capsys, "--registry", str(forked), "check", "--no-generate")
    assert code == 1 and "FAILED:  png/png-mine: an unresolved merge conflict" in out
    put(forked, "png-mine", spec_text({5: "# line 5, both"}))  # the conflict is resolved
    assert run(capsys, "--registry", str(forked), "check", "--no-generate")[0] == 0


def test_a_comment_that_looks_like_a_marker_is_not_a_conflict(capsys, forked, up):
    put(forked, "png-mine", spec_text({3: "# <<<<<<< not a conflict, there is no closing marker"}))
    assert run(capsys, "--registry", str(forked), "check", "--no-generate")[0] == 0


# --- dry run

def test_dry_run_says_what_would_happen_and_writes_nothing(capsys, forked, up):
    put(forked, "png-mine", spec_text({2: "# line 2, mine"}))
    put(up, "png", spec_text({9: "# line 9, theirs"}))
    republish(up, "png", version="1.1")
    before, before_meta = fan(forked, "png-mine"), meta(forked, "png-mine")
    code, out, _ = rebase(capsys, forked, up, "--dry-run")
    assert code == 0 and "merged cleanly" in out and "(dry run: nothing was written)" in out
    assert fan(forked, "png-mine") == before and meta(forked, "png-mine") == before_meta


def test_dry_run_of_a_conflict_fails_without_writing(capsys, forked, up):
    conflicting(forked, up)
    before = fan(forked, "png-mine")
    code, out, _ = rebase(capsys, forked, up, "--dry-run")
    assert code == 1 and "1 conflict(s)" in out and fan(forked, "png-mine") == before


# --- where the common ancestor comes from

def test_the_common_ancestor_can_come_from_the_git_history(capsys, forked, up, monkeypatch, tmp_path):
    sh("git", "init", "-q", "-b", "main", cwd=forked)
    sh("git", "add", "-A", cwd=forked)
    sh("git", "commit", "-q", "-m", "fork png", cwd=forked)  # the fork, as it was copied
    put(forked, "png-mine", spec_text({2: "# line 2, mine"}))
    sh("git", "commit", "-q", "-am", "change it", cwd=forked)
    put(up, "png", spec_text({9: "# line 9, theirs"}))
    republish(up, "png", version="1.1")
    monkeypatch.setenv("FANBASE_CACHE", str(tmp_path / "an-empty-cache"))
    code, out, _ = rebase(capsys, forked, up)
    assert code == 0 and "common ancestor: the git history of the fork" in out and "merged cleanly" in out


def test_a_commit_that_is_not_the_original_is_not_taken_for_it(capsys, forked, up, monkeypatch, tmp_path):
    put(forked, "png-mine", spec_text({2: "# line 2, mine"}))  # changed before the first commit
    sh("git", "init", "-q", "-b", "main", cwd=forked)
    sh("git", "add", "-A", cwd=forked)
    sh("git", "commit", "-q", "-m", "fork png and change it", cwd=forked)
    put(up, "png", spec_text({9: "# line 9, theirs"}))
    republish(up, "png", version="1.1")
    monkeypatch.setenv("FANBASE_CACHE", str(tmp_path / "an-empty-cache"))
    code, _, err = rebase(capsys, forked, up)
    assert code == 2 and "is not kept on this machine" in err and "--base-registry" in err


def test_the_common_ancestor_can_come_from_an_older_release_of_the_registry(capsys, forked, up, monkeypatch, tmp_path):
    release = tmp_path / "release"
    shutil.copytree(up, release)
    put(forked, "png-mine", spec_text({2: "# line 2, mine"}))
    put(up, "png", spec_text({9: "# line 9, theirs"}))
    republish(up, "png", version="1.1")
    monkeypatch.setenv("FANBASE_CACHE", str(tmp_path / "an-empty-cache"))
    code, out, _ = rebase(capsys, forked, up, "--base-registry", str(release))
    assert code == 0 and f"common ancestor: the registry {release}" in out and "merged cleanly" in out


def test_a_release_of_the_wrong_time_is_refused(capsys, forked, up, monkeypatch, tmp_path):
    put(up, "png", spec_text({9: "# line 9, theirs"}))
    republish(up, "png", version="1.1")
    release = tmp_path / "later-release"
    shutil.copytree(up, release)  # it already has the change: not the original the fork was made from
    put(forked, "png-mine", spec_text({2: "# line 2, mine"}))
    monkeypatch.setenv("FANBASE_CACHE", str(tmp_path / "an-empty-cache"))
    code, _, err = rebase(capsys, forked, up, "--base-registry", str(release))
    assert code == 2 and "but the fork was made from" in err and "release of the registry from the time of the fork" in err


def test_a_fork_from_before_the_hash_was_kept_needs_the_release(capsys, forked, up, monkeypatch, tmp_path):
    release = tmp_path / "release"
    shutil.copytree(up, release)
    path = forked / "specs/png/png-mine/metadata.yml"
    data = yaml.safe_load(path.read_text())
    del data["derived_sha256"]
    path.write_text(yaml.safe_dump(data))
    put(forked, "png-mine", spec_text({2: "# line 2, mine"}))
    put(up, "png", spec_text({9: "# line 9, theirs"}))
    republish(up, "png", version="1.1")
    code, _, err = rebase(capsys, forked, up)
    assert code == 2 and "does not say what it was made from" in err
    code, out, _ = rebase(capsys, forked, up, "--base-registry", str(release))
    assert code == 0 and "merged cleanly" in out
    assert "derived_sha256" in meta(forked, "png-mine")  # and from now on it does


# --- what it needs

def test_a_spec_that_is_not_a_fork(capsys, forked, up):
    code, _, err = run(capsys, "--registry", str(forked), "rebase", "gif", "--upstream", str(up))
    assert code == 2 and "gif/gif is not a fork" in err


def test_an_original_that_is_gone(capsys, forked, up):
    shutil.rmtree(up / "specs" / "png")
    from fanbase.manifest import dump_index, reindex
    from fanbase.registry import Registry

    (up / "specs/gif/gif").mkdir(parents=True)
    (up / "specs/gif/gif/gif.fan").write_text("<start> ::= 'gif'\n")
    rows, _, _ = reindex(Registry(up))
    (up / INDEX_FILENAME).write_text(dump_index(rows))
    code, _, err = rebase(capsys, forked, up)
    assert code == 2 and "cannot find what the fork was made from" in err


def test_rebase_needs_git(capsys, forked, up, monkeypatch):
    put(forked, "png-mine", spec_text({2: "# line 2, mine"}))
    put(up, "png", spec_text({9: "# line 9, theirs"}))
    republish(up, "png", version="1.1")
    real = contrib._run

    def no_git(cmd, **kwargs):
        if cmd[0] == "git":
            raise FileNotFoundError("git")
        return real(cmd, **kwargs)

    monkeypatch.setattr(contrib, "_run", no_git)
    code, _, err = rebase(capsys, forked, up)
    assert code == 2 and "rebase needs git" in err


# --- the original is in the same registry

def test_a_fork_inside_the_registry_it_was_made_from(capsys, up):
    run(capsys, "--registry", str(up), "fork", "png", "--as", "png-mine")
    put(up, "png-mine", spec_text({2: "# line 2, mine"}))
    put(up, "png", spec_text({9: "# line 9, theirs"}))
    republish(up, "png", version="1.1")
    code, out, _ = run(capsys, "--registry", str(up), "rebase", "png-mine")  # no --upstream: it is right here
    assert code == 0 and "merged cleanly" in out
    merged = fan(up, "png-mine")
    assert "# line 2, mine" in merged and "# line 9, theirs" in merged


# --- what the original's metadata says

def test_it_says_when_the_original_now_extends_something_else(capsys, tmp_path, up, mine):
    run(capsys, "--registry", str(up), "new", "png-base", "--description", "base")
    run(capsys, "--registry", str(up), "fork", "png", "--as", "png-mine", "--into", str(mine))
    put(up, "png", spec_text({9: "# line 9, theirs"}))
    republish(up, "png", version="1.1", extends=["png-base"], fandango=">=1.4")
    code, out, _ = rebase(capsys, mine, up)
    assert code == 0
    assert "note: the original now extends ['fanbase:png-base'], and the fork extends nothing" in out
    assert "note: the original's fandango is '>=1.4'" in out
    assert meta(mine, "png-mine").get("extends") is None  # the fork's metadata is the fork's own


def test_it_says_when_the_original_changed_without_a_new_version(capsys, forked, up):
    put(up, "png", spec_text({9: "# line 9, theirs"}))  # republished, but still at 1.0
    code, out, _ = rebase(capsys, forked, up)
    assert code == 0 and "fanbase:png/png@1.0, which has changed but is still at version 1.0" in out
