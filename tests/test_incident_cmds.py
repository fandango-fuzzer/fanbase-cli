import io
import json
import os
import shutil
import smtplib
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml
from conftest import build_registry
from helpers import REAL_RUN, STRICT, FakeAge, FakeFandango, settle, target

from fanbase import contrib, incident_cmds, incidents
from fanbase.cli import main
from fanbase.incidents import BUNDLE_NAME

posix = pytest.mark.skipif(sys.platform == "win32", reason="signals and modes are POSIX")
needs_age = pytest.mark.skipif(shutil.which("age") is None or shutil.which("age-keygen") is None, reason="age is not installed")

SECRET_STDERR = "SECRET-STDERR-MARKER the parser said this"
CRASH = f"import os, signal, sys\nprint({SECRET_STDERR!r}, file=sys.stderr, flush=True)\nos.kill(os.getpid(), signal.SIGSEGV)\n"

HOST, USER, PASSWORD, FROM, TO = "smtp.secret-host.example", "secret-user", "hunter2-PASSWORD", "sender@secret-from.example", "reader@secret-to.example"


@pytest.fixture(autouse=True)
def real_subprocess(monkeypatch):
    monkeypatch.setattr(subprocess, "run", REAL_RUN)


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def tar_of(members: dict[str, bytes], pad: int = 0) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue() + b"\0" * pad


@pytest.fixture
def age(monkeypatch):
    fake = FakeAge()
    real_which = shutil.which
    for module in (incidents, incident_cmds):
        monkeypatch.setattr(module, "_run", fake)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/fake/age" if name == "age" else real_which(name, *a, **k))
    return fake


@pytest.fixture
def key(tmp_path):
    path = tmp_path / "key.txt"
    path.write_text("# a private key\nAGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ\n")
    return path


def sealed(tmp_path, members, name=BUNDLE_NAME, pad=0):
    """A record as the fake age writes it."""
    path = tmp_path / name
    path.write_bytes(FakeAge.HEADER + tar_of(members, pad))
    return path


def open_bundle(capsys, bundle, key, *more):
    return run(capsys, "incidents", "open", str(bundle), "--identity", str(key), *more)


RECORD = {
    "manifest.json": json.dumps({"incidents": [{"id": "x"}, {"id": "y"}]}).encode(),
    "REPORT.md": b"# the record\n",
    "crasher-crash-1a2b3c4d/REPORT.md": b"# crash\n",
    "crasher-crash-1a2b3c4d/stderr.txt": b"it said this",
    "crasher-crash-1a2b3c4d/input-1.bin": b"\x00\x01\x02",
}


# --- open

@posix
def test_a_record_opens_into_a_folder_only_you_can_read(capsys, age, key, tmp_path):
    bundle = sealed(tmp_path, RECORD, pad=4096)
    code, out, _ = open_bundle(capsys, bundle, key, "--into", str(tmp_path / "opened"))
    assert code == 0 and "5 file(s), 2 incident(s)" in out
    folder = tmp_path / "opened"
    assert (folder / "crasher-crash-1a2b3c4d" / "input-1.bin").read_bytes() == b"\x00\x01\x02"
    assert (folder / "REPORT.md").read_bytes() == b"# the record\n"
    modes = {p: p.stat().st_mode & 0o777 for p in [folder, *folder.rglob("*")]}
    assert all(mode == (0o700 if p.is_dir() else 0o600) for p, mode in modes.items())
    assert age.calls[-1][:2] == ["age", "-d"] and str(key) in age.calls[-1]


def test_a_record_of_a_quiet_run_says_so(capsys, age, key, tmp_path):
    bundle = sealed(tmp_path, {"manifest.json": b'{"incidents": []}', "REPORT.md": b"Nothing broke.\n"})
    _, out, _ = open_bundle(capsys, bundle, key, "--into", str(tmp_path / "o"))
    assert "nothing broke" in out


def test_without_a_folder_it_is_named_after_the_record(capsys, age, key, tmp_path, monkeypatch):
    bundle = sealed(tmp_path, RECORD)
    monkeypatch.chdir(tmp_path)
    code, _, _ = open_bundle(capsys, bundle, key)
    assert code == 0 and (tmp_path / "evaluation-private" / "REPORT.md").is_file()
    other = sealed(tmp_path, RECORD, name="download")
    assert open_bundle(capsys, other, key)[0] == 0 and (tmp_path / "download.d" / "REPORT.md").is_file()


def test_a_folder_with_something_in_it_is_left_alone(capsys, age, key, tmp_path):
    bundle = sealed(tmp_path, RECORD)
    folder = tmp_path / "busy"
    folder.mkdir()
    (folder / "mine.txt").write_text("keep")
    code, _, err = open_bundle(capsys, bundle, key, "--into", str(folder))
    assert code == 2 and "is not empty" in err
    assert [p.name for p in folder.iterdir()] == ["mine.txt"] and (folder / "mine.txt").read_text() == "keep"


def test_a_file_where_the_folder_should_be(capsys, age, key, tmp_path):
    bundle = sealed(tmp_path, RECORD)
    (tmp_path / "file").write_text("x")
    assert open_bundle(capsys, bundle, key, "--into", str(tmp_path / "file"))[0] == 2


@posix
def test_an_empty_folder_is_used_and_made_private(capsys, age, key, tmp_path):
    bundle = sealed(tmp_path, RECORD)
    folder = tmp_path / "empty"
    folder.mkdir(mode=0o755)
    assert open_bundle(capsys, bundle, key, "--into", str(folder))[0] == 0
    assert folder.stat().st_mode & 0o777 == 0o700 and (folder / "REPORT.md").is_file()


@posix
def test_a_symbolic_link_is_not_followed(capsys, age, key, tmp_path):
    bundle = sealed(tmp_path, RECORD)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "link").symlink_to(elsewhere)
    code, _, err = open_bundle(capsys, bundle, key, "--into", str(tmp_path / "link"))
    assert code == 2 and "symbolic link" in err and list(elsewhere.iterdir()) == []


def symlink(name, target_):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = tarfile.SYMTYPE, target_
    return info


def hardlink(name, target_):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = tarfile.LNKTYPE, target_
    return info


def raw_tar(*infos_and_data):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for info, data in infos_and_data:
            if data is not None:
                info.size = len(data)
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def named(name, data=b"x"):
    return tarfile.TarInfo(name), data


def folder_entry(name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info, None


BAD_RECORDS = {
    "up": raw_tar(named("../escaped")),
    "up-inside": raw_tar(named("a/../../escaped")),
    "absolute": raw_tar(named("/tmp/escaped")),
    "dot-dot-in-a-name": raw_tar(named("a/./b")),
    "backslash": raw_tar(named("a\\b")),
    "hidden": raw_tar(named(".hidden")),
    "drive": raw_tar(named("C:evil")),
    "spaces": raw_tar(named("a b")),
    "too-deep": raw_tar(named("a/b/c/d/e/f")),
    "symlink": raw_tar((symlink("link", "/etc/passwd"), None)),
    "hardlink": raw_tar(named("real"), (hardlink("hard", "real"), None)),
    "folder": raw_tar(folder_entry("a")),
    "twice": raw_tar(named("same"), named("same")),
    "file-and-folder": raw_tar(named("a"), named("a/b")),
    "after-the-good": raw_tar(named("good"), named("../escaped")),
}


@pytest.mark.parametrize("name", sorted(BAD_RECORDS))
def test_nothing_but_plain_files_under_plain_names_is_unpacked(capsys, age, key, tmp_path, name):
    bundle = tmp_path / "bad.age"
    bundle.write_bytes(FakeAge.HEADER + BAD_RECORDS[name])
    code, _, err = open_bundle(capsys, bundle, key, "--into", str(tmp_path / "out"))
    assert code == 2 and "not opening it" in err, err
    assert not (tmp_path / "out").exists() and not (tmp_path / "escaped").exists() and not Path("/tmp/escaped").exists()


def test_what_is_not_a_record_is_refused(capsys, age, key, tmp_path):
    for content in (b"just some text, not a tar", b"\x1f\x8b\x08" + b"\0" * 200):  # (and not a compressed one)
        bundle = tmp_path / "junk.age"
        bundle.write_bytes(FakeAge.HEADER + content)
        code, _, err = open_bundle(capsys, bundle, key, "--into", str(tmp_path / "out"))
        assert code == 2 and "not a record" in err and not (tmp_path / "out").exists()


def test_a_record_that_claims_more_than_it_holds(capsys, age, key, tmp_path):
    whole = tar_of({"big.bin": b"x" * 4096})
    bundle = tmp_path / "short.age"
    bundle.write_bytes(FakeAge.HEADER + whole[:1024 + 100])  # the header of the file, and a part of what it says it has
    code, _, err = open_bundle(capsys, bundle, key, "--into", str(tmp_path / "out"))
    assert code == 2 and not (tmp_path / "out").exists()


def test_too_many_files(capsys, age, key, tmp_path, monkeypatch):
    monkeypatch.setattr(incident_cmds, "MAX_FILES", 3)
    bundle = sealed(tmp_path, {f"f{i}": b"x" for i in range(4)})
    code, _, err = open_bundle(capsys, bundle, key, "--into", str(tmp_path / "out"))
    assert code == 2 and "more than 3 files" in err and not (tmp_path / "out").exists()


def test_a_wrong_key_opens_nothing(capsys, key, tmp_path, monkeypatch):
    monkeypatch.setattr(incident_cmds, "_run", FakeAge(fail=True))
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/fake/age" if name == "age" else real_which(name, *a, **k))
    bundle = sealed(tmp_path, RECORD)
    code, _, err = open_bundle(capsys, bundle, key, "--into", str(tmp_path / "out"))
    assert code == 2 and "age could not open" in err and not (tmp_path / "out").exists()


def test_something_age_did_not_make(capsys, age, key, tmp_path):
    bundle = tmp_path / "plain.age"
    bundle.write_bytes(tar_of(RECORD))
    code, _, err = open_bundle(capsys, bundle, key, "--into", str(tmp_path / "out"))
    assert code == 2 and "age could not open" in err and not (tmp_path / "out").exists()


def test_a_record_or_a_key_that_is_not_there(capsys, age, key, tmp_path):
    bundle = sealed(tmp_path, RECORD)
    assert "no such file" in open_bundle(capsys, tmp_path / "nope.age", key)[2]
    assert "private key" in open_bundle(capsys, bundle, tmp_path / "nokey.txt")[2]


def test_without_age_nothing_is_opened(capsys, key, tmp_path, monkeypatch):
    bundle = sealed(tmp_path, RECORD)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    code, _, err = open_bundle(capsys, bundle, key, "--into", str(tmp_path / "out"))
    assert code == 2 and "not installed" in err and not (tmp_path / "out").exists()


def test_a_record_too_large_to_be_ours(capsys, age, key, tmp_path, monkeypatch):
    monkeypatch.setattr(incident_cmds, "MAX_OPEN", 10)
    bundle = sealed(tmp_path, RECORD)
    code, _, err = open_bundle(capsys, bundle, key, "--into", str(tmp_path / "out"))
    assert code == 2 and "not a record fanbase made" in err and age.calls == []


@posix
def test_a_write_that_fails_leaves_nothing_behind(capsys, age, key, tmp_path, monkeypatch):
    bundle = sealed(tmp_path, RECORD)
    real_open, count = os.open, {"n": 0}

    def failing(path, *args, **kwargs):
        count["n"] += 1
        if count["n"] == 3:
            raise OSError(28, "No space left on device")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing)
    code, _, err = open_bundle(capsys, bundle, key, "--into", str(tmp_path / "out"))
    assert code == 2 and "could not unpack" in err and "No space" in err
    assert not (tmp_path / "out").exists()


# --- send

class Mailbox:
    """Stands in for a mail server, and keeps what happened to it in order."""

    def __init__(self):
        self.events, self.sent, self.context = [], [], None
        self.connect_error = self.starttls_error = self.login_error = self.send_error = None

    def connection(self, implicit_tls):
        box = self

        class Connection:
            def __init__(self, host, port, timeout=None, local_hostname=None, context=None):
                if box.connect_error:
                    raise box.connect_error
                box.events.append(("connect", "tls" if implicit_tls else "plain", host, port, timeout, local_hostname))
                box.context = context

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                box.events.append("quit")

            def ehlo(self):
                box.events.append("ehlo")

            def starttls(self, context=None):
                if box.starttls_error:
                    raise box.starttls_error
                box.events.append("starttls")
                box.context = context

            def login(self, user, password):
                if box.login_error:
                    raise box.login_error
                box.events.append(("login", user, password))

            def send_message(self, message):
                if box.send_error:
                    raise box.send_error
                box.events.append("send")
                box.sent.append(message)

        return Connection

    @property
    def kinds(self):
        return [e if isinstance(e, str) else e[0] for e in self.events]


@pytest.fixture
def mailbox(monkeypatch):
    box = Mailbox()
    monkeypatch.setattr(smtplib, "SMTP", box.connection(False))
    monkeypatch.setattr(smtplib, "SMTP_SSL", box.connection(True))
    return box


@pytest.fixture
def smtp(monkeypatch):
    for name in ("HOST", "PORT", "USER", "PASSWORD", "FROM"):
        monkeypatch.delenv(f"FANBASE_SMTP_{name}", raising=False)
    monkeypatch.setenv("FANBASE_SMTP_HOST", HOST)
    monkeypatch.setenv("FANBASE_SMTP_USER", USER)
    monkeypatch.setenv("FANBASE_SMTP_PASSWORD", PASSWORD)
    monkeypatch.setenv("FANBASE_SMTP_FROM", FROM)
    return monkeypatch


@pytest.fixture
def record(tmp_path):
    path = tmp_path / BUNDLE_NAME
    path.write_bytes(b"age-encryption.org/v1\n-> X25519 abc\n--- def\n" + os.urandom(4096))
    return path


def send(capsys, record, to=TO):
    return run(capsys, "incidents", "send", str(record), "--to", to)


def test_the_record_is_mailed_over_tls_and_nothing_secret_is_printed(capsys, mailbox, smtp, record):
    code, out, err = send(capsys, record)
    assert code == 0 and "mailed evaluation-private.age" in out
    for secret in (HOST, USER, PASSWORD, FROM, TO):
        assert secret not in out + err, secret
    assert mailbox.events[0] == ("connect", "tls", HOST, 465, incident_cmds.SMTP_TIMEOUT, "localhost")
    assert mailbox.kinds == ["connect", "login", "send", "quit"] and ("login", USER, PASSWORD) in mailbox.events
    assert mailbox.context.verify_mode.name == "CERT_REQUIRED" and mailbox.context.check_hostname  # the server is who it says


def test_the_mail_carries_the_file_and_nothing_else(capsys, mailbox, smtp, record):
    send(capsys, record)
    message, = mailbox.sent
    assert (message["From"], message["To"]) == (FROM, TO) and message["Subject"] == "Fanbase evaluation record"
    attachment, = message.iter_attachments()
    assert attachment.get_filename() == "evaluation-private.age" and attachment.get_content_type() == "application/octet-stream"
    assert attachment.get_content() == record.read_bytes()
    body = message.get_body().get_content()
    for word in ("crash", "hang", "vulnerab", "bug", "found", "broke"):
        assert word not in body.lower()


def test_the_mail_says_the_same_whatever_the_record_holds(capsys, mailbox, smtp, tmp_path):
    for n, size in enumerate((10, 5000)):
        path = tmp_path / f"r{n}.age"
        path.write_bytes(b"age-encryption.org/v1\n" + os.urandom(size))
        assert send(capsys, path)[0] == 0
    first, second = mailbox.sent
    assert first.get_body().get_content() == second.get_body().get_content() and first["Subject"] == second["Subject"]


def test_a_name_other_than_the_default_is_sent_as_the_default(capsys, mailbox, smtp, tmp_path):
    odd = tmp_path / "artifact-download-1234.bin"
    odd.write_bytes(b"age-encryption.org/v1\nrest")
    send(capsys, odd)
    attachment, = mailbox.sent[0].iter_attachments()
    assert attachment.get_filename() == "evaluation-private.age"


def test_on_another_port_it_starts_in_the_clear_and_must_upgrade_before_logging_in(capsys, mailbox, smtp, record):
    smtp.setenv("FANBASE_SMTP_PORT", "587")
    code, out, err = send(capsys, record)
    assert code == 0
    assert mailbox.events[0][:4] == ("connect", "plain", HOST, 587)
    assert mailbox.kinds == ["connect", "ehlo", "starttls", "ehlo", "login", "send", "quit"]  # TLS before the password
    assert mailbox.context.check_hostname and PASSWORD not in out + err


def test_a_server_that_cannot_upgrade_gets_nothing(capsys, mailbox, smtp, record):
    smtp.setenv("FANBASE_SMTP_PORT", "25")
    mailbox.starttls_error = smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")
    code, out, err = send(capsys, record)
    assert code == 2 and "SMTPNotSupportedError" in err
    assert "login" not in mailbox.kinds and "send" not in mailbox.kinds  # not the password, not the file
    for secret in (HOST, USER, PASSWORD, FROM, TO):
        assert secret not in out + err


def test_a_wrong_password_is_not_printed(capsys, mailbox, smtp, record):
    mailbox.login_error = smtplib.SMTPAuthenticationError(535, f"5.7.8 {PASSWORD} rejected for {USER} at {HOST}".encode())
    code, out, err = send(capsys, record)
    assert code == 2 and "SMTPAuthenticationError, code 535" in err and "still where it was written" in err
    for secret in (HOST, USER, PASSWORD, FROM, TO):
        assert secret not in out + err, secret
    assert "send" not in mailbox.kinds


def test_a_server_that_cannot_be_reached_is_not_named(capsys, mailbox, smtp, record):
    mailbox.connect_error = ConnectionRefusedError(111, f"Connection refused by {HOST}:465")
    code, out, err = send(capsys, record)
    assert code == 2 and "ConnectionRefusedError" in err
    for secret in (HOST, USER, PASSWORD, FROM, TO):
        assert secret not in out + err, secret


def test_a_refused_recipient_is_not_named(capsys, mailbox, smtp, record):
    mailbox.send_error = smtplib.SMTPRecipientsRefused({TO: (550, b"no such user")})
    code, out, err = send(capsys, record)
    assert code == 2 and TO not in out + err


def test_only_what_age_made_is_mailed(capsys, mailbox, smtp, tmp_path):
    for name, content in (("tar.bin", tar_of(RECORD)), ("text.txt", b"hello"), ("empty.age", b""),
                          ("armored.age", b"-----BEGIN AGE ENCRYPTED FILE-----\nYWdl\n-----END AGE ENCRYPTED FILE-----\n"),
                          ("late.age", b" age-encryption.org/v1\n")):
        path = tmp_path / name
        path.write_bytes(content)
        code, _, err = send(capsys, path)
        assert code == 2 and "not an age-encrypted file" in err, name
    assert mailbox.events == []  # no connection was even made


def test_a_file_that_is_too_large_is_not_mailed(capsys, mailbox, smtp, tmp_path):
    big = tmp_path / "big.age"
    with big.open("wb") as handle:
        handle.write(b"age-encryption.org/v1\n")
        handle.truncate(incident_cmds.MAX_MAIL + 1)
    code, _, err = send(capsys, big)
    assert code == 2 and "more than the 20 MB" in err and "artifact" in err and mailbox.events == []


def test_a_file_just_under_the_limit_is_mailed(capsys, mailbox, smtp, tmp_path, monkeypatch):
    monkeypatch.setattr(incident_cmds, "MAX_MAIL", 2048)
    ok = tmp_path / "ok.age"
    ok.write_bytes(b"age-encryption.org/v1\n" + b"x" * (2048 - 22))
    assert ok.stat().st_size == 2048 and send(capsys, ok)[0] == 0


def test_a_file_that_is_not_there(capsys, mailbox, smtp, tmp_path):
    code, _, err = send(capsys, tmp_path / "nope.age")
    assert code == 2 and "no such file" in err and mailbox.events == []


@pytest.mark.parametrize("address", [
    "not-an-address", "a@b", "Reader <reader@example.org>", "reader@example.org, other@example.org", "reader@example.org\nBcc: x@y.z",
    "reader@example.org\r\nBcc: x@y.z", "<reader@example.org>", "", "@example.org", "reader@", "re ader@example.org",
])
def test_an_address_that_is_not_one_address_is_refused_unechoed(capsys, mailbox, smtp, record, address):
    code, out, err = send(capsys, record, address)
    assert code == 2 and "is not an email address" in err and mailbox.events == []
    assert address.strip() == "" or address.strip() not in out + err


def test_without_a_server_nothing_is_sent(capsys, mailbox, smtp, record):
    smtp.delenv("FANBASE_SMTP_HOST")
    code, _, err = send(capsys, record)
    assert code == 2 and "FANBASE_SMTP_HOST" in err and mailbox.events == []


def test_without_a_sender_the_user_is_one_if_it_is_an_address(capsys, mailbox, smtp, record):
    smtp.delenv("FANBASE_SMTP_FROM")
    assert "FANBASE_SMTP_FROM" in send(capsys, record)[2]  # (a name is not an address)
    smtp.setenv("FANBASE_SMTP_USER", "login@example.org")
    assert send(capsys, record)[0] == 0 and mailbox.sent[0]["From"] == "login@example.org"


def test_a_user_without_a_password_or_the_reverse(capsys, mailbox, smtp, record):
    smtp.delenv("FANBASE_SMTP_PASSWORD")
    assert "go together" in send(capsys, record)[2]
    smtp.setenv("FANBASE_SMTP_PASSWORD", PASSWORD)
    smtp.delenv("FANBASE_SMTP_USER")
    code, _, err = send(capsys, record)
    assert code == 2 and "go together" in err and PASSWORD not in err and mailbox.events == []


def test_a_relay_that_wants_no_login(capsys, mailbox, smtp, record):
    smtp.delenv("FANBASE_SMTP_USER")
    smtp.delenv("FANBASE_SMTP_PASSWORD")
    assert send(capsys, record)[0] == 0 and "login" not in mailbox.kinds


@pytest.mark.parametrize("port", ["abc", "0", "70000", "-1", "4 65"])
def test_a_port_that_is_not_one(capsys, mailbox, smtp, record, port):
    smtp.setenv("FANBASE_SMTP_PORT", port)
    code, _, err = send(capsys, record)
    assert code == 2 and "FANBASE_SMTP_PORT" in err and mailbox.events == []


def test_a_sender_that_is_not_an_address(capsys, mailbox, smtp, record):
    smtp.setenv("FANBASE_SMTP_FROM", "Me <me@example.org>")
    code, _, err = send(capsys, record)
    assert code == 2 and "FANBASE_SMTP_FROM is not an email address" in err and "me@example.org" not in err and mailbox.events == []


# --- with the real age

@needs_age
@posix
def test_an_evaluation_is_opened_with_the_private_key_and_mailed_as_it_is(capsys, tmp_path, monkeypatch, mailbox, smtp):
    root = build_registry(tmp_path / "reg", {("png", "png"): dict(version="1.0", description="PNG")})
    target(root, "strict", STRICT)
    target(root, "crasher", CRASH)
    (root / "specs/png/format.yml").write_text(yaml.safe_dump({"targets": ["strict", "crasher"]}))
    settle(root)
    fandango = FakeFandango(content=lambda i: b"INPUT-%d" % i)
    monkeypatch.setattr(contrib, "find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr("fanbase.evaluate.find_fandango", lambda: "/fake/fandango")
    monkeypatch.setattr(contrib, "_run", fandango)

    keys = tmp_path / "key.txt"
    REAL_RUN(["age-keygen", "-o", str(keys)], check=True, capture_output=True)
    recipients = tmp_path / "recipients.txt"
    recipients.write_text(REAL_RUN(["age-keygen", "-y", str(keys)], check=True, capture_output=True, text=True).stdout)
    code, out, err = run(capsys, "--registry", str(root), "evaluate", "png", "-n", "4", "--incidents", str(tmp_path / "private"),
                         "--incident-recipients", str(recipients))
    assert code == 0 and SECRET_STDERR not in out + err
    bundle = tmp_path / "private" / BUNDLE_NAME

    # mailed exactly as it is: the file age made passes the check, and arrives untouched
    assert send(capsys, bundle)[0] == 0
    attachment, = mailbox.sent[0].iter_attachments()
    assert attachment.get_content() == bundle.read_bytes()
    assert SECRET_STDERR.encode() not in attachment.get_content()

    # and opened with the key
    code, out, _ = open_bundle(capsys, bundle, keys, "--into", str(tmp_path / "opened"))
    assert code == 0 and "1 incident(s)" in out
    manifest = json.loads((tmp_path / "opened" / "manifest.json").read_text())
    incident, = manifest["incidents"]
    assert incident["signal"] == "SIGSEGV" and incident["occurrences"] == 4
    assert SECRET_STDERR in (tmp_path / "opened" / incident["id"] / "stderr.txt").read_text()
    assert (tmp_path / "opened" / incident["id"] / "input-1.bin").read_bytes().startswith(b"INPUT-")
    assert (tmp_path / "opened" / "REPORT.md").is_file()
    assert (tmp_path / "opened").stat().st_mode & 0o777 == 0o700

    # a key that is not the one it was made for opens nothing
    other = tmp_path / "other.txt"
    REAL_RUN(["age-keygen", "-o", str(other)], check=True, capture_output=True)
    code, _, err = open_bundle(capsys, bundle, other, "--into", str(tmp_path / "nope"))
    assert code == 2 and "age could not open" in err and not (tmp_path / "nope").exists()


@needs_age
def test_what_real_age_makes_passes_the_check_and_what_it_armors_does_not(capsys, tmp_path, mailbox, smtp):
    keys = tmp_path / "key.txt"
    REAL_RUN(["age-keygen", "-o", str(keys)], check=True, capture_output=True)
    public = REAL_RUN(["age-keygen", "-y", str(keys)], check=True, capture_output=True, text=True).stdout.strip()
    binary, armored = tmp_path / "binary.age", tmp_path / "armored.age"
    REAL_RUN(["age", "-r", public, "-o", str(binary)], input=b"secret", check=True, capture_output=True)
    REAL_RUN(["age", "-a", "-r", public, "-o", str(armored)], input=b"secret", check=True, capture_output=True)
    assert send(capsys, binary)[0] == 0
    assert send(capsys, armored)[0] == 2
