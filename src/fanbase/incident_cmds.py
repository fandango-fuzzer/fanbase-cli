"""`fanbase incidents open|send`: reading the private record of an evaluation, and mailing it.

The record `fanbase evaluate --incident-recipients` writes is one file, encrypted with `age`. `open` decrypts
it with your private key and unpacks it into a folder only you can read. `send` mails it (it is encrypted, so
the mail carries nothing anyone else could read) from a machine with the SMTP settings in its environment:
CI, on the runs that are trusted.

Neither prints anything that the record, a key, a password or an address could be read from.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import smtplib
import ssl
import subprocess
import tarfile
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

from fanbase.context import Context
from fanbase.incidents import BUNDLE_NAME
from fanbase.registry import RegistryError

AGE_HEADER = b"age-encryption.org/v1"  # how every age file starts
MAX_MAIL = 20 * 1024 * 1024  # a larger file is not mailed
MAX_OPEN = 256 * 1024 * 1024  # a larger file is not opened: the record is a few megabytes
MAX_FILES = 5000
MAX_DEPTH = 4
SMTP_TIMEOUT = 60
IMPLICIT_TLS_PORT = 465  # TLS from the first byte; any other port starts in the clear and must upgrade

_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")  # what a file or a folder of the record may be called

_run = subprocess.run  # every program started here goes through it, so that tests can stand in for it


# --- open

def _decrypt(bundle: Path, identity: Path) -> bytes:
    if shutil.which("age") is None:
        raise RegistryError("the private record is encrypted with `age`, which is not installed (https://age-encryption.org)")
    if not bundle.is_file():
        raise RegistryError(f"{bundle}: no such file")
    if not identity.is_file():
        raise RegistryError(f"{identity}: no such file; it is the private key (age-keygen made it)")
    if bundle.stat().st_size > MAX_OPEN:
        raise RegistryError(f"{bundle}: larger than {MAX_OPEN >> 20} MB; this is not a record fanbase made")
    done = _run(["age", "-d", "-i", str(identity), str(bundle)], capture_output=True)
    if done.returncode != 0:
        raise RegistryError(f"age could not open {bundle}: {done.stderr.decode(errors='replace').strip()[:300]}")
    return done.stdout


def _plain_name(name: str) -> tuple[str, ...]:
    parts = tuple(name.split("/"))  # (as written: not tidied up, which would hide a name that is not plain)
    if len(parts) > MAX_DEPTH or not all(_PART.fullmatch(part) for part in parts):
        raise RegistryError(f"the record holds a path that is not a plain name inside its folder ({name[:60]!r}); not opening it")
    return parts


def unpack(plaintext: bytes, into: Path) -> list[Path]:
    """Write the files of the record into `into`, which is a folder that is not there yet, or empty.

    Only plain files go in, under plain names, and everything is checked before anything is written."""
    try:
        archive = tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:")
    except tarfile.TarError:
        raise RegistryError("what was decrypted is not a record fanbase made") from None
    files: list[tuple[tuple[str, ...], tarfile.TarInfo]] = []
    with archive:
        try:
            members = archive.getmembers()
        except tarfile.TarError:
            raise RegistryError("what was decrypted is not a record fanbase made") from None
        if len(members) > MAX_FILES:
            raise RegistryError(f"the record holds more than {MAX_FILES} files; not opening it")
        total = 0
        for member in members:
            if not member.isreg() or member.issparse():
                raise RegistryError(f"the record holds something that is not a plain file ({member.name[:60]!r}); not opening it")
            total += member.size
            if total > len(plaintext):
                raise RegistryError("the record claims to hold more than it does; not opening it")
            files.append((_plain_name(member.name), member))
        seen: set[tuple[str, ...]] = set()
        inside: set[tuple[str, ...]] = set()  # the folders that the files are in
        for parts, _ in files:
            if parts in seen:
                raise RegistryError(f"the record names {'/'.join(parts)} twice; not opening it")
            seen.add(parts)
            inside.update(parts[:i] for i in range(1, len(parts)))
        if seen & inside:
            raise RegistryError("the record names something as both a file and a folder; not opening it")

        if into.is_symlink():
            raise RegistryError(f"{into} is a symbolic link; not unpacking into it")
        if into.exists() and (not into.is_dir() or any(into.iterdir())):
            raise RegistryError(f"{into} is not empty; unpack into a folder of its own (--into)")
        made_into = not into.exists()
        into.mkdir(parents=True, exist_ok=True)
        os.chmod(into, 0o700)

        written: list[Path] = []
        folders: list[Path] = []
        try:
            for parts, member in files:
                folder = into
                for part in parts[:-1]:
                    folder = folder / part
                    if not folder.exists():
                        folder.mkdir()
                        os.chmod(folder, 0o700)
                        folders.append(folder)
                path = folder / parts[-1]
                content = archive.extractfile(member)
                if content is None:
                    raise RegistryError("the record holds something that is not a plain file; not opening it")
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    shutil.copyfileobj(content, handle)
                os.chmod(path, 0o600)
                written.append(path)
        except OSError as exc:
            for path in written:
                path.unlink(missing_ok=True)
            for folder in reversed(folders):
                folder.rmdir()
            if made_into:
                into.rmdir()
            raise RegistryError(f"could not unpack into {into}: {exc.strerror or exc.__class__.__name__}") from None
    return written


def cmd_incidents_open(args, ctx: Context) -> int:
    bundle = Path(args.bundle)
    into = Path(args.into) if args.into else Path(bundle.name[: -len(".age")] if bundle.name.endswith(".age") else bundle.name + ".d")
    plaintext = _decrypt(bundle, Path(args.identity))
    written = unpack(plaintext, into)
    count = ""
    for path in written:
        if path == into / "manifest.json":
            try:
                found = len(json.loads(path.read_text(encoding="utf-8"))["incidents"])
                count = f", {found} incident(s)" if found else ", nothing broke"
            except (OSError, ValueError, KeyError, TypeError):
                pass
    print(f"opened {len(written)} file(s){count} into {into}; start with {into / 'REPORT.md'}", flush=True)
    return 0


# --- send

def _settings(environ) -> dict:
    host = environ.get("FANBASE_SMTP_HOST", "").strip()
    user = environ.get("FANBASE_SMTP_USER", "")
    password = environ.get("FANBASE_SMTP_PASSWORD", "")
    sender = environ.get("FANBASE_SMTP_FROM", "").strip() or (user.strip() if "@" in user else "")
    port_text = environ.get("FANBASE_SMTP_PORT", "").strip() or str(IMPLICIT_TLS_PORT)
    missing = [name for name, value in (("FANBASE_SMTP_HOST", host), ("FANBASE_SMTP_FROM", sender)) if not value]
    if missing:
        raise RegistryError(f"to send mail, set {' and '.join(missing)} (and FANBASE_SMTP_PORT, FANBASE_SMTP_USER, FANBASE_SMTP_PASSWORD)")
    if bool(user) != bool(password):
        raise RegistryError("FANBASE_SMTP_USER and FANBASE_SMTP_PASSWORD go together: set both or neither")
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise RegistryError("FANBASE_SMTP_PORT is not a port number")
    return {"host": host, "port": int(port_text), "user": user, "password": password, "sender": sender}


def _address(text: str, what: str) -> str:
    """An address as one word, with nothing that could add a header or a recipient. (The value is not
    echoed: it may be a secret.)"""
    name, address = parseaddr(text)
    local, _, domain = address.partition("@")
    if (name or address != text.strip() or not local or not domain or "." not in domain
            or re.search(r"[\s<>,;\"'()\[\]\\]", address)):
        raise RegistryError(f"{what} is not an email address")
    return address


def _checked_file(path: Path) -> bytes:
    if not path.is_file():
        raise RegistryError(f"{path}: no such file")
    size = path.stat().st_size
    if size > MAX_MAIL:
        raise RegistryError(f"{path}: {size >> 20} MB is more than the {MAX_MAIL >> 20} MB that is mailed; use the artifact instead")
    data = path.read_bytes()
    if not data.startswith(AGE_HEADER):
        raise RegistryError(f"{path} is not an age-encrypted file; only the encrypted record is ever mailed")
    return data


def _say(exc: BaseException) -> str:
    """What went wrong, with nothing that could be a host, an address or a password: only the kind."""
    code = f", code {exc.smtp_code}" if isinstance(exc, smtplib.SMTPResponseException) else ""
    return f"{exc.__class__.__name__}{code}"


def send(data: bytes, to: str, settings: dict) -> None:
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = settings["sender"], to, "Fanbase evaluation record"
    message.set_content("The attached file is encrypted. Open it with:\n\n    fanbase incidents open evaluation-private.age --identity YOUR_PRIVATE_KEY\n")
    message.add_attachment(data, maintype="application", subtype="octet-stream", filename=BUNDLE_NAME)
    context = ssl.create_default_context()  # certificates and host names are checked
    try:
        server: smtplib.SMTP
        if settings["port"] == IMPLICIT_TLS_PORT:
            server = smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=SMTP_TIMEOUT, context=context, local_hostname="localhost")
        else:
            server = smtplib.SMTP(settings["host"], settings["port"], timeout=SMTP_TIMEOUT, local_hostname="localhost")
        with server:
            if settings["port"] != IMPLICIT_TLS_PORT:
                server.ehlo()
                server.starttls(context=context)  # refused, not skipped, if the server cannot
                server.ehlo()
            if settings["user"]:
                server.login(settings["user"], settings["password"])  # only after TLS
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise RegistryError(f"could not send the mail ({_say(exc)}); the encrypted file is still where it was written") from None


def cmd_incidents_send(args, ctx: Context) -> int:
    path = Path(args.file)
    data = _checked_file(path)
    to = _address(args.to, "the address after --to")
    settings = _settings(os.environ)
    _address(settings["sender"], "FANBASE_SMTP_FROM")
    send(data, to, settings)
    print(f"mailed {path.name} ({len(data)} bytes, encrypted)", flush=True)
    return 0
