"""Signatures on a registry's index, made and checked with the real ssh-keygen (with throwaway keys made here)."""

import functools
import http.server
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
import yaml
from conftest import build_registry
from helpers import REAL_RUN

from fanbase import config as config_module, signing, source
from fanbase.cli import main
from fanbase.registry import RegistryError
from fanbase.remote import RemoteRegistry
from fanbase.signing import SigningError, parse_key, read_keys

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is not installed")


@pytest.fixture(autouse=True)
def real_subprocess(monkeypatch):
    monkeypatch.setattr(subprocess, "run", REAL_RUN)


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def keypair(tmp_path, name="key", comment="maintainer@test"):
    private = tmp_path / name
    REAL_RUN(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(private)], check=True, capture_output=True)
    return str(private), (tmp_path / f"{name}.pub").read_text().strip()


@pytest.fixture
def key(tmp_path):
    return keypair(tmp_path)


@pytest.fixture
def reg(tmp_path):
    return build_registry(tmp_path / "reg", {("png", "png"): dict(version="1.0", description="PNG"),
                                              ("png", "png-top"): dict(version="1.0", description="Top", extends=["png"])}, name="acme")


def signed(capsys, reg, key):
    code, out, err = run(capsys, "--registry", str(reg), "sign", "--key", key[0])
    assert code == 0, err
    return out


@pytest.fixture
def served(tmp_path, reg):
    """The registry, as a copy that can be changed, on a server on this machine."""
    shutil.copytree(reg, tmp_path / "web")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path / "web"))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", tmp_path / "web"
    server.shutdown()
    server.server_close()


def sync(reg, web):
    shutil.copy(reg / "index.yml", web / "index.yml")
    if (reg / "index.yml.sig").exists():
        shutil.copy(reg / "index.yml.sig", web / "index.yml.sig")


# --- keys

def test_a_fingerprint_is_what_ssh_keygen_says(key, tmp_path):
    said = REAL_RUN(["ssh-keygen", "-l", "-f", f"{key[0]}.pub"], check=True, capture_output=True, text=True).stdout.split()[1]
    assert parse_key(key[1]).fingerprint == said


@pytest.mark.parametrize("text", [
    "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7 old", "ssh-dss AAAAB3NzaC1kc3M= old", "not a key", "", "ssh-ed25519",
    "ssh-ed25519 AAAA\nssh-ed25519 BBBB", "ssh-ed25519 not*base64!", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA\rinjected",
])
def test_only_modern_keys_in_one_line_are_keys(text):
    with pytest.raises(SigningError):
        parse_key(text)


def test_a_key_may_have_a_comment_which_is_not_kept(key):
    assert parse_key(key[1]).text == " ".join(key[1].split()[:2])
    assert "maintainer" not in parse_key(key[1]).text


def test_keys_can_be_given_as_text_or_a_file(tmp_path, key):
    other = keypair(tmp_path, "other")
    path = tmp_path / "keys.txt"
    path.write_text(f"# the maintainers\n\n{key[1]}\n{other[1]}\n")
    assert [k.text for k in read_keys(str(path))] == [parse_key(key[1]).text, parse_key(other[1]).text]
    assert read_keys(key[1])[0].text == parse_key(key[1]).text
    assert read_keys(f"{key[0]}.pub")[0].text == parse_key(key[1]).text
    (tmp_path / "empty.txt").write_text("# nothing\n")
    with pytest.raises(SigningError, match="no public key"):
        read_keys(str(tmp_path / "empty.txt"))


# --- signing

def test_a_maintainer_signs_the_index(capsys, reg, key):
    out = signed(capsys, reg, key)
    assert f"signed index.yml with the ED25519 key {parse_key(key[1]).fingerprint}: index.yml.sig" in out
    sig = (reg / "index.yml.sig").read_text()
    assert sig.startswith("-----BEGIN SSH SIGNATURE-----")


def test_an_index_that_is_out_of_date_is_not_signed(capsys, reg, key):
    (reg / "specs/png/png/png.fan").write_text("<start> ::= 'changed'\n")  # not reindexed
    code, _, err = run(capsys, "--registry", str(reg), "sign", "--key", key[0])
    assert code == 2 and "index.yml is out of date" in err and not (reg / "index.yml.sig").exists()


def test_a_key_that_is_not_there_does_not_sign(capsys, reg, tmp_path):
    code, _, err = run(capsys, "--registry", str(reg), "sign", "--key", str(tmp_path / "nokey"))
    assert code == 2 and "could not sign" in err and not (reg / "index.yml.sig").exists()


def test_signing_needs_the_key_named():
    with pytest.raises(SystemExit):
        main(["sign"])


def test_without_ssh_keygen_it_is_said(capsys, reg, key, monkeypatch):
    monkeypatch.setattr(signing.shutil, "which", lambda name, *a, **k: None)
    code, _, err = run(capsys, "--registry", str(reg), "sign", "--key", key[0])
    assert code == 2 and "`ssh-keygen` (OpenSSH 8.0 or later), which is not installed" in err


# --- reading a registry that is signed

def test_a_registry_signed_by_a_key_you_trust_opens(capsys, reg, key, served):
    url, web = served
    signed(capsys, reg, key)
    sync(reg, web)
    opened = RemoteRegistry(url, None, [parse_key(key[1])])
    assert opened.signed_by.fingerprint == parse_key(key[1]).fingerprint and opened.formats() == ["png"]


def test_one_of_several_keys_is_enough(capsys, reg, key, served, tmp_path):
    url, web = served
    signed(capsys, reg, key)
    sync(reg, web)
    other = keypair(tmp_path, "other")
    assert RemoteRegistry(url, None, [parse_key(other[1]), parse_key(key[1])]).signed_by is not None


def test_another_key_is_refused(capsys, reg, key, served, tmp_path):
    url, web = served
    signed(capsys, reg, key)
    sync(reg, web)
    other = keypair(tmp_path, "other")
    with pytest.raises(SigningError, match="not signed by a key you trust"):
        RemoteRegistry(url, None, [parse_key(other[1])])


def test_an_index_that_was_changed_after_it_was_signed_is_refused(capsys, reg, key, served):
    url, web = served
    signed(capsys, reg, key)
    sync(reg, web)
    index = (web / "index.yml").read_text()
    (web / "index.yml").write_text(index.replace("sha256: ", "sha256: 0", 1))  # a different hash for a spec: a different file
    with pytest.raises(SigningError, match="not signed by a key you trust"):
        RemoteRegistry(url, None, [parse_key(key[1])])


def test_no_signature_is_refused_when_one_is_asked_for(capsys, reg, key, served):
    url, web = served
    sync(reg, web)  # (the index, not signed)
    with pytest.raises(SigningError, match="is not signed"):
        RemoteRegistry(url, None, [parse_key(key[1])])
    RemoteRegistry(url)  # (without keys to trust, nothing is asked)


def test_a_signature_that_is_not_one_is_refused(capsys, reg, key, served):
    url, web = served
    sync(reg, web)
    for garbage in (b"hello", b"-----BEGIN SSH SIGNATURE-----\nnonsense\n-----END SSH SIGNATURE-----\n", b"-----BEGIN SSH SIGNATURE-----" + b"x" * 20000):
        (web / "index.yml.sig").write_bytes(garbage)
        with pytest.raises(SigningError):
            RemoteRegistry(url, None, [parse_key(key[1])])


def test_a_signature_made_for_something_else_is_not_an_index_signature(capsys, reg, key, served):
    url, web = served
    sync(reg, web)
    # the same key, the same file, a different namespace: what a git signature would be
    REAL_RUN(["ssh-keygen", "-Y", "sign", "-f", key[0], "-n", "git", str(web / "index.yml")], check=True, capture_output=True)
    with pytest.raises(SigningError, match="not signed by a key you trust"):
        RemoteRegistry(url, None, [parse_key(key[1])])


def test_a_signature_of_another_file_is_refused(capsys, reg, key, served, tmp_path):
    url, web = served
    sync(reg, web)
    (tmp_path / "other.txt").write_text("not the index\n")
    sig = signing.sign(tmp_path / "other.txt", key[0])
    shutil.copy(sig, web / "index.yml.sig")
    with pytest.raises(SigningError):
        RemoteRegistry(url, None, [parse_key(key[1])])


def test_nothing_is_believed_before_the_signature_is_(capsys, reg, key, served, tmp_path):
    url, web = served
    signed(capsys, reg, key)
    sync(reg, web)
    (web / "index.yml").write_text("not even yaml: [")
    with pytest.raises(SigningError):  # (the signature fails first; the index is never read as an index)
        RemoteRegistry(url, None, [parse_key(key[1])])


# --- pinning a key for a registry

def test_a_key_is_pinned_when_a_registry_is_added(capsys, reg, key, served, tmp_path):
    url, web = served
    signed(capsys, reg, key)
    sync(reg, web)
    code, out, _ = run(capsys, "registry", "add", url, "--trust", "--signer", key[1])
    assert code == 0 and f"its index is signed by {parse_key(key[1]).fingerprint}" in out
    saved = yaml.safe_load(Path(config_module.config_path()).read_text())
    assert saved["registries"]["acme"]["signers"] == [parse_key(key[1]).text]
    _, out, _ = run(capsys, "registry", "list")
    assert f"signed by {parse_key(key[1]).fingerprint}" in out


def test_a_registry_that_is_not_signed_by_that_key_is_not_added(capsys, reg, key, served, tmp_path):
    url, web = served
    signed(capsys, reg, key)
    sync(reg, web)
    other = keypair(tmp_path, "other")
    code, _, err = run(capsys, "registry", "add", url, "--trust", "--signer", other[1])
    assert code == 2 and "not signed by a key you trust" in err and not Path(config_module.config_path()).exists()


def test_what_changes_afterwards_without_a_signature_is_refused(capsys, reg, key, served, tmp_path):
    url, web = served
    signed(capsys, reg, key)
    sync(reg, web)
    run(capsys, "registry", "add", url, "--trust", "--signer", key[1])
    assert run(capsys, "list", "acme:png")[0] == 0
    (web / "specs/png/png/png.fan").write_text("import os\nos.system('echo owned')\n<start> ::= 'x'\n")  # what a thief would put there
    (web / "index.yml").write_text((web / "index.yml").read_text().replace("sha256: ", "sha256: 0", 1))
    code, _, err = run(capsys, "list", "acme:png")
    assert code == 2 and "not signed by a key you trust" in err


def test_a_spec_of_a_signed_registry_installs(capsys, reg, key, served, tmp_path):
    url, web = served
    signed(capsys, reg, key)
    sync(reg, web)
    run(capsys, "registry", "add", url, "--trust", "--signer", key[1])
    code, out, _ = run(capsys, "install", "acme:png/png-top", "--into", str(tmp_path / "lib"))
    assert code == 0 and (tmp_path / "lib/acme/png/png-top.fan").is_file() and (tmp_path / "lib/acme/png/png.fan").is_file()


def test_a_checkout_on_this_machine_is_not_signed_for(capsys, reg, key):
    code, _, err = run(capsys, "registry", "add", str(reg), "--trust", "--signer", key[1])
    assert code == 2 and "--signer is for a registry read over the network" in err


def test_adding_again_with_other_keys_is_not_the_same(capsys, reg, key, served, tmp_path):
    url, web = served
    signed(capsys, reg, key)
    sync(reg, web)
    run(capsys, "registry", "add", url, "--trust", "--signer", key[1])
    assert "already added" in run(capsys, "registry", "add", url, "--trust", "--signer", key[1])[1]
    code, _, err = run(capsys, "registry", "add", url, "--trust")
    assert code == 2 and "already added" in err and "remove it first" in err


def test_a_config_with_a_bad_key_is_refused(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump({"registries": {"acme": {"url": "https://example.org/acme", "signers": ["ssh-rsa AAAA old"]}}}))
    with pytest.raises(RegistryError, match="registry 'acme': signers"):
        config_module.load_config(path)
    path.write_text(yaml.safe_dump({"registries": {"acme": {"url": "https://example.org/acme", "signers": "nope"}}}))
    with pytest.raises(RegistryError, match="signers has to be a list"):
        config_module.load_config(path)


# --- fanbase verify

def test_verify_a_checkout(capsys, reg, key):
    signed(capsys, reg, key)
    code, out, _ = run(capsys, "--registry", str(reg), "verify", "--signer", key[1])
    assert code == 0 and f"is signed by the ED25519 key {parse_key(key[1]).fingerprint}, which you trust" in out


def test_verify_with_the_wrong_key_or_no_signature(capsys, reg, key, tmp_path):
    other = keypair(tmp_path, "other")
    code, _, err = run(capsys, "--registry", str(reg), "verify", "--signer", other[1])
    assert code == 2 and "No such file or directory" in err  # (not signed yet)
    signed(capsys, reg, key)
    code, _, err = run(capsys, "--registry", str(reg), "verify", "--signer", other[1])
    assert code == 2 and "not signed by a key you trust" in err


def test_verify_over_the_network_and_by_name(capsys, reg, key, served):
    url, web = served
    signed(capsys, reg, key)
    sync(reg, web)
    assert run(capsys, "--registry", url, "verify", "--signer", key[1])[0] == 0
    run(capsys, "registry", "add", url, "--trust", "--signer", key[1])
    code, out, _ = run(capsys, "verify", "acme")
    assert code == 0 and "which you trust" in out
    assert run(capsys, "verify", "nosuch")[0] == 2


def test_verify_needs_keys(capsys, reg, key):
    signed(capsys, reg, key)
    code, _, err = run(capsys, "--registry", str(reg), "verify")
    assert code == 2 and "no keys to check against" in err


# --- the public registry's keys

def test_the_public_registry_is_checked_at_a_release_when_it_has_keys(monkeypatch, key):
    official = "https://github.com/fandango-fuzzer/fanbase"
    assert source.signers_for(f"{official}/tree/v2026.11.01") == ()  # (no keys yet: nothing is checked)
    monkeypatch.setattr(source, "DEFAULT_SIGNERS", (key[1],))
    expected = [parse_key(key[1]).text]
    assert [k.text for k in source.signers_for(f"{official}/tree/v2026.11.01")] == expected  # a release
    assert [k.text for k in source.signers_for(f"{official}/tree/0123456789abcdef0123456789abcdef01234567")] == expected  # a commit
    assert source.signers_for(official) == () and source.signers_for(f"{official}/") == ()  # the default moves with every merge
    assert source.signers_for(f"{official}/tree/main") == () and source.signers_for(f"{official}/tree/HEAD") == ()
    assert source.signers_for("https://github.com/someone/else/tree/v1") == ()
    assert source.signers_for("https://example.org/fanbase") == ()


def test_a_release_is_not_opened_if_it_is_not_signed_and_main_is(capsys, reg, key, served, monkeypatch):
    url, web = served
    sync(reg, web)  # not signed
    monkeypatch.setattr(source, "DEFAULT_SIGNERS", (key[1],))
    monkeypatch.setattr(source, "is_official", lambda u: True)
    monkeypatch.setattr(source, "moves", lambda u: False)  # (as a release is)
    with pytest.raises(SigningError, match="is not signed"):
        source.open_registry(source.Location("url", url))
    monkeypatch.setattr(source, "moves", lambda u: True)  # (as main is)
    assert source.open_registry(source.Location("url", url)).formats() == ["png"]


def test_a_release_that_is_signed_opens_with_the_built_in_keys(capsys, reg, key, served, monkeypatch):
    url, web = served
    signed(capsys, reg, key)
    sync(reg, web)
    monkeypatch.setattr(source, "DEFAULT_SIGNERS", (key[1],))
    monkeypatch.setattr(source, "is_official", lambda u: True)
    monkeypatch.setattr(source, "moves", lambda u: False)
    opened = source.open_registry(source.Location("url", url))
    assert opened.signed_by.fingerprint == parse_key(key[1]).fingerprint
