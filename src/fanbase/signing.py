"""Signatures on a registry's index.

A registry's specs are code that runs inside Fandango, and the one thing that says which files a registry has is its
`index.yml`, which holds the SHA-256 of every spec. If the index is signed by a key that the user has chosen to trust,
then every spec that matches it is what the registry's maintainers published, whoever else got to the repository, the
host or the network on the way. The signature is `index.yml.sig`, next to the index.

Signatures are SSH signatures: `ssh-keygen -Y sign` makes them and `ssh-keygen -Y verify` checks them, so that nothing
has to be installed besides OpenSSH (8.0 or later), and a key can be an ordinary one or one on a hardware token. They are
made in the namespace `fanbase`, so that a signature the same key made for anything else (a git commit, say) is not an
index signature, and the other way round.

What a signature does not say: that the index is the newest. An older index, signed, stays signed. To read a registry as
it was at a release, point at the release (`.../tree/<tag>`), which does not move.

The keys to trust are the user's choice, never the registry's (a registry that said which keys to trust would be saying
so in the very files that are in question): they are pinned with `fanbase registry add --signer`, and the public
registry's are in `DEFAULT_SIGNERS`, and apply to a release or a commit of it (`.../tree/<tag>`), never to `main`, the
default, which moves with every merge while a signature does not.
"""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fanbase.output import clean
from fanbase.registry import RegistryError

SIGNATURE_FILENAME = "index.yml.sig"
NAMESPACE = "fanbase"
MAX_SIGNATURE = 16 * 1024
_ARMOR = "-----BEGIN SSH SIGNATURE-----"

# The public keys that sign the public registry's index, as `ssh-keygen -y` or a .pub file writes them. While there are none,
# the public registry is not checked (and when there are, only at a release or a commit, not at main): a maintainer who signs puts the public key here, in the CLI, which is how it
# gets to the users (the key has to come from somewhere other than the registry it signs).
DEFAULT_SIGNERS: tuple[str, ...] = ()

# What may sign: modern keys only (no RSA, no DSA).
_KEY = re.compile(
    r"(ssh-ed25519|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|"
    r"sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) ([A-Za-z0-9+/]+={0,2})(?: [^\r\n]*)?")


class SigningError(RegistryError):
    """A signature could not be made, or is not good."""


@dataclass(frozen=True)
class Key:
    kind: str
    body: str

    @property
    def text(self) -> str:
        return f"{self.kind} {self.body}"

    @property
    def fingerprint(self) -> str:
        """As `ssh-keygen -l` writes it: SHA256: and the unpadded base64 of the hash of the key."""
        digest = hashlib.sha256(base64.b64decode(self.body)).digest()
        return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def parse_key(text: str) -> Key:
    """A public key, as a line of a .pub file; refused if it is anything else, or an old kind of key."""
    line = text.strip()
    match = _KEY.fullmatch(line) if "\n" not in line and "\r" not in line else None
    if match is None:
        raise SigningError(f"not a public key that can sign a registry's index: {clean(line)[:60]!r} (an ed25519, ecdsa or security-key "
                           "line, as in a .pub file)")
    try:
        base64.b64decode(match.group(2), validate=True)
    except ValueError:
        raise SigningError("that public key is not valid base64") from None
    return Key(match.group(1), match.group(2))


def read_keys(given: str) -> list[Key]:
    """Public keys from what a person gave: a key itself, or the path of a file of them (a .pub file)."""
    path = Path(given).expanduser()
    if not given.startswith(("ssh-", "ecdsa-", "sk-")) and path.is_file():
        try:
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        except OSError as exc:
            raise SigningError(f"{path}: {exc.strerror or exc}") from None
        if not lines:
            raise SigningError(f"{path} has no public key in it")
        return [parse_key(line) for line in lines]
    return [parse_key(given)]


def _ssh_keygen() -> str:
    found = shutil.which("ssh-keygen")
    if found is None:
        raise SigningError("signatures are made and checked with `ssh-keygen` (OpenSSH 8.0 or later), which is not installed")
    return found


@dataclass(frozen=True)
class Verified:
    fingerprint: str
    kind: str


_GOOD = re.compile(r'Good "fanbase" signature(?: for \S+)? with (\S+) key (SHA256:[A-Za-z0-9+/]+)')


def verify(data: bytes, signature: bytes, signers: list[Key] | tuple[Key, ...]) -> Verified:
    """Check that `data` was signed, in the namespace of fanbase, by one of `signers`."""
    if not signers:
        raise SigningError("no keys to check the signature against")
    if len(signature) > MAX_SIGNATURE or not signature.lstrip().startswith(_ARMOR.encode()):
        raise SigningError("the signature is not an SSH signature")
    ssh_keygen = _ssh_keygen()
    with tempfile.TemporaryDirectory(prefix="fanbase-sig-") as tmp:
        allowed, sig = Path(tmp) / "allowed_signers", Path(tmp) / "index.sig"
        allowed.write_text("".join(f'fanbase namespaces="{NAMESPACE}" {key.text}\n' for key in signers), encoding="utf-8")
        sig.write_bytes(signature)
        done = subprocess.run([ssh_keygen, "-Y", "verify", "-f", str(allowed), "-I", "fanbase", "-n", NAMESPACE, "-s", str(sig)],
                              input=data, capture_output=True, timeout=60)
    said = (done.stdout + done.stderr).decode("utf-8", errors="replace")
    match = _GOOD.search(said)
    if done.returncode != 0 or match is None:
        raise SigningError("the index is not signed by a key you trust (the signature is not good, or is by another key)")
    return Verified(match.group(2), match.group(1))


def sign(path: Path, key_file: str) -> Path:
    """Sign a file with the private key `key_file`; the signature is written next to it as `<name>.sig`.

    ssh-keygen does it, and asks for a passphrase or a touch of a token itself: the key is never read here."""
    ssh_keygen = _ssh_keygen()
    sig = path.with_name(path.name + ".sig")
    sig.unlink(missing_ok=True)
    done = subprocess.run([ssh_keygen, "-Y", "sign", "-f", key_file, "-n", NAMESPACE, str(path)], stderr=subprocess.PIPE, timeout=600)
    if done.returncode != 0 or not sig.is_file():
        raise SigningError(f"ssh-keygen could not sign: {clean(done.stderr.decode('utf-8', errors='replace').strip())[:200]}")
    return sig


def who_signed(data: bytes, signature: bytes) -> Verified:
    """Whose key made a signature, without saying whether that key is to be trusted: it is checked to be a good signature,
    which tells who made it. For a person to read what they have just done."""
    with tempfile.TemporaryDirectory(prefix="fanbase-sig-") as tmp:
        sig = Path(tmp) / "index.sig"
        sig.write_bytes(signature)
        done = subprocess.run([_ssh_keygen(), "-Y", "check-novalidate", "-n", NAMESPACE, "-s", str(sig)], input=data,
                              capture_output=True, timeout=60)
    said = (done.stdout + done.stderr).decode("utf-8", errors="replace")
    match = re.search(r'Good "fanbase" signature with (\S+) key (SHA256:[A-Za-z0-9+/]+)', said)
    if done.returncode != 0 or match is None:
        raise SigningError("that is not a good signature")
    return Verified(match.group(2), match.group(1))


# --- the commands

def cmd_sign(args, ctx) -> int:
    """Sign the index of the registry checkout, with a private key the maintainer has."""
    from fanbase.contrib import checkout
    from fanbase.manifest import INDEX_FILENAME, index_is_stale, reindex

    reg = checkout(ctx)
    rows, _, _ = reindex(reg, write=False)
    if index_is_stale(reg, rows):
        raise RegistryError(f"{INDEX_FILENAME} is out of date: run `fanbase reindex` first, so that what is signed is what is in the registry")
    index = reg.root / INDEX_FILENAME
    signature = sign(index, args.key)
    who = who_signed(index.read_bytes(), signature.read_bytes())
    print(f"signed {INDEX_FILENAME} with the {who.kind} key {who.fingerprint}: {signature.name}")
    print("commit it with the index; users check it with `fanbase registry add URL --signer PUBLIC_KEY`")
    return 0


def cmd_verify(args, ctx) -> int:
    """Is the index of a registry signed by a key that is trusted?"""
    from fanbase.manifest import INDEX_FILENAME
    from fanbase.registry import Registry
    from fanbase.source import locate_registry, signers_for

    keys: list[Key] = [k for given in args.signer or [] for k in read_keys(given)]
    where = args.registry or None
    if args.name:
        configured = ctx.config.registries.get(args.name)
        if configured is None:
            raise RegistryError(f"no registry called {args.name!r}")
        where = configured.url
        keys = keys or [parse_key(k) for k in configured.signers]
    location = locate_registry(where, local=False)
    if location.kind == "url" and not keys:
        keys = list(signers_for(location.value))
    if not keys:
        raise SigningError("no keys to check against: give --signer PUBLIC_KEY (a key, or a .pub file), or pin some with "
                           "`fanbase registry add --signer`")
    if location.kind == "path":
        root = Path(location.value)
        try:
            data, signature = (root / INDEX_FILENAME).read_bytes(), (root / SIGNATURE_FILENAME).read_bytes()
        except OSError as exc:
            raise SigningError(f"{root}: {exc.strerror or exc}") from None
        verified = verify(data, signature, keys)
        Registry(root)  # (and it is a registry)
    else:
        from fanbase.remote import RemoteRegistry

        opened = RemoteRegistry(location.value, None, tuple(keys))
        assert opened.signed_by is not None
        verified = opened.signed_by
    print(f"{INDEX_FILENAME} of {clean(location.value)} is signed by the {verified.kind} key {verified.fingerprint}, which you trust")
    return 0
