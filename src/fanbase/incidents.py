"""The private record of a target that broke on a file.

A parser that crashes, hangs or is killed on a file Fanbase generated may have a bug that is not fixed
yet. What fanbase publishes about it is nothing: a public report counts it as a plain error. The details
go to the one who has to report it, and only to them:

  - the input, what ran, how it ended and what it said, how to reproduce it, and a note to the vendor
    to start from, grouped by cause so that a bug found 40 times is one entry;
  - in a bundle that is *encrypted to a public key* with `age`, so that it is safe in a place anyone can
    read (a public CI artifact, an email) and needs no secret to make;
  - that is the same size, rounded up to a megabyte, whether it holds a crash or nothing, and is always
    written, so that its existence says nothing either.

Without a key (on your own machine) the same record is written, unencrypted, to a directory only you
can read.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from fanbase import __version__
from fanbase.registry import RegistryError
from fanbase.targets import Target, Verdict, normalise_reason, version_of

BUNDLE_NAME = "evaluation-private.age"
PAD = 1 << 20  # the bundle is a whole number of these
MAX_INPUT = 5 * 1024 * 1024  # an input larger than this is described, not kept
KEEP_PER_CAUSE = 3  # inputs kept for each cause
# The record has to stay small enough to be mailed (see incident_cmds), whatever broke: a record that was
# refused for its size would tell whoever reads the log of a public run how much had broken. Inputs are
# kept up to MAX_KEPT in all, causes are written down up to MAX_INCIDENTS (about 20 kB each at most), and
# what is beyond that is counted. The sum stays below 16 MB.
MAX_KEPT = 8 * 1024 * 1024
MAX_INCIDENTS = 300

_run = subprocess.run  # every program started here goes through it, so that tests can stand in for it


@dataclass
class Incident:
    signature: str
    kind: str  # crash, hang or killed
    spec: str
    spec_version: str | None
    spec_sha256: str
    target: str
    target_version: str | None
    returncode: int | None
    argv: tuple[str, ...]
    stderr: bytes
    occurrences: int = 0
    confirmed: int = 0  # how many of them happened again when tried once more
    inputs: list[tuple[bytes | None, str, int]] = field(default_factory=list)  # (data or None if too large, sha256, size)

    @property
    def signal(self) -> str | None:
        if self.returncode is not None and self.returncode < 0:
            try:
                return signal.Signals(-self.returncode).name
            except ValueError:
                return str(-self.returncode)
        return None

    @property
    def id(self) -> str:
        # (a plain name that no other incident has: the same cause in two specs is two entries)
        return f"{self.spec.replace('/', '_')}__{self.target}-{self.kind}-{self.signature[:12]}"


class IncidentLog:
    """The incidents of a run, grouped by cause. Safe to add to from several threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_cause: dict[tuple[str, str, str], Incident] = {}
        self._versions: dict[str, str | None] = {}
        self._kept = 0  # bytes of inputs kept so far
        self.not_itemised = 0  # occurrences of causes beyond MAX_INCIDENTS

    def record(self, *, spec: str, version: str | None, sha256: str, target: Target, file: Path, verdict: Verdict,
               confirmed: bool) -> None:
        text = normalise_reason(verdict.stderr.decode("utf-8", errors="replace"), file)
        cause = hashlib.sha256(f"{verdict.incident}|{target.name}|{verdict.returncode}|{text}".encode()).hexdigest()
        try:
            data = file.read_bytes()
        except OSError:
            data = b""
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            if target.name not in self._versions:
                self._versions[target.name] = version_of(target)
            key = (spec, target.name, cause)
            incident = self._by_cause.get(key)
            if incident is None:
                if len(self._by_cause) >= MAX_INCIDENTS:
                    self.not_itemised += 1
                    return
                incident = self._by_cause[key] = Incident(
                    cause, verdict.incident, spec, version, sha256, target.name, self._versions[target.name],
                    verdict.returncode, verdict.argv, verdict.stderr)
            incident.occurrences += 1
            incident.confirmed += bool(confirmed)
            if len(incident.inputs) < KEEP_PER_CAUSE and digest not in {d for _, d, _ in incident.inputs}:
                keep = len(data) <= MAX_INPUT and self._kept + len(data) <= MAX_KEPT
                self._kept += len(data) if keep else 0
                incident.inputs.append((data if keep else None, digest, len(data)))

    @property
    def incidents(self) -> list[Incident]:
        return sorted(self._by_cause.values(), key=lambda i: (i.spec, i.target, i.kind, i.signature))


# --- what is written

def _report(inc: Incident, meta: dict) -> str:
    ended = {"crash": f"killed by {inc.signal}", "hang": "no answer in time", "killed": f"killed by {inc.signal} (for what it used)"}[inc.kind]
    again = f"{inc.confirmed} of {inc.occurrences}" if inc.occurrences > 1 else ("yes" if inc.confirmed else "no")
    kept = ", ".join(f"input-{n}.bin ({size} bytes, sha256 {digest[:16]})" if data is not None
                     else f"(an input of {size} bytes, sha256 {digest[:16]}, was too large to keep)"
                     for n, (data, digest, size) in enumerate(inc.inputs, 1))
    stderr = inc.stderr.decode("utf-8", errors="replace").strip() or "(nothing)"
    return f"""# {inc.target} {inc.target_version or ''}: {inc.kind} on a file generated from {inc.spec}

**How it ended:** {ended}
**Again when tried once more:** {again}{"  (a hang can also be a busy machine: try it yourself)" if inc.kind == "hang" else ""}
**Seen:** {inc.occurrences} time(s) in this run; cause `{inc.signature[:12]}`
**Kept:** {kept}

## What ran

    {' '.join(inc.argv)}

(`INPUT` is the file; on a machine with {inc.target} installed.)

What it printed (the first {len(inc.stderr)} bytes):

    {stderr.replace(chr(10), chr(10) + '    ')}

## How the file was made

Fanbase {meta['fanbase']}, Fandango {meta['fandango']}, spec `{inc.spec}` version {inc.spec_version or '-'} (sha256 {inc.spec_sha256}),
random seed {meta['seed']}, {meta['count']} inputs asked for. {meta['platform']}; Python {meta['python']}.

## Before you report it

- [ ] Reproduce it with the current release of {inc.target}, and with a build that has AddressSanitizer if you can.
- [ ] Find the project's security contact (a SECURITY.md, a security policy, or its maintainers' address).
- [ ] Report it **privately**. The registry's ETHICS.md says affected parties are notified before anything is
      published. Do not put the input in a public issue, in this repository, or in a CI log.
- [ ] Write down the date you reported it: ______ . A usual deadline is 90 days, or 30 days after a fix is released
      (OSS-Fuzz and Project Zero both use it).
- [ ] When it is fixed: run the input again with the fixed release.
"""


def members(log: IncidentLog, meta: dict) -> list[tuple[str, bytes]]:
    """Everything in the record, as (path, content)."""
    found = log.incidents
    manifest = {
        "schema": 1, **meta, "not_itemised": log.not_itemised,
        "incidents": [
            {"id": i.id, "signature": i.signature, "kind": i.kind, "spec": i.spec, "spec_version": i.spec_version,
             "spec_sha256": i.spec_sha256, "target": i.target, "target_version": i.target_version,
             "returncode": i.returncode, "signal": i.signal, "command": list(i.argv),
             "occurrences": i.occurrences, "confirmed": i.confirmed,
             "inputs": [{"file": f"{i.id}/input-{n}.bin" if data is not None else None, "sha256": d, "size": size}
                        for n, (data, d, size) in enumerate(i.inputs, 1)]}
            for i in found
        ],
    }
    out: list[tuple[str, bytes]] = [("manifest.json", json.dumps(manifest, indent=2).encode())]
    index = ["# Fanbase evaluation: private record", "",
             f"{len(found)} incident(s), made {meta['created']}.", ""]
    index += [f"- `{i.id}`: {i.kind} of {i.target} on {i.spec} ({i.occurrences}x)" for i in found] or ["Nothing broke."]
    if log.not_itemised:
        index += ["", f"{log.not_itemised} further occurrence(s) of other causes happened after {MAX_INCIDENTS} had been written down; "
                      "run it again on your machine with --incidents to see them."]
    out.append(("REPORT.md", "\n".join(index).encode() + b"\n"))
    for i in found:
        out.append((f"{i.id}/REPORT.md", _report(i, meta).encode()))
        out.append((f"{i.id}/stderr.txt", i.stderr))
        out += [(f"{i.id}/input-{n}.bin", data) for n, (data, _, _) in enumerate(i.inputs, 1) if data is not None]
    return out


def build_bundle(log: IncidentLog, meta: dict) -> bytes:
    """The record as a tar archive, followed by zeros up to a whole number of megabytes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in members(log, meta):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o600, int(time.time())
            archive.addfile(info, io.BytesIO(data))
    data = buffer.getvalue()
    return data + b"\0" * (-len(data) % PAD)


def run_meta(seed: int, count: int) -> dict:
    from fanbase.evaluate import _fandango_version

    return {
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "fanbase": __version__,
        "fandango": _fandango_version() or "?", "seed": seed, "count": count,
        "platform": platform.platform(), "python": platform.python_version(),
    }


# --- keeping it private

def check_recipients(recipients: Path) -> None:
    """Refuse, before anything runs, to go on without a way to keep the record private."""
    if shutil.which("age") is None:
        raise RegistryError("the private record is encrypted with `age`, which is not installed (https://age-encryption.org)")
    if not recipients.is_file():
        raise RegistryError(f"{recipients}: no such file; it holds the age public key(s) the record is encrypted to")
    with tempfile.TemporaryDirectory() as tmp:
        done = _run(["age", "-R", str(recipients), "-o", str(Path(tmp) / "probe.age")], input=b"", capture_output=True)
    if done.returncode != 0:
        raise RegistryError(f"{recipients}: age cannot use it: {done.stderr.decode(errors='replace').strip()[:200]}")


def write_private(log: IncidentLog, meta: dict, directory: Path, recipients: Path | None) -> Path | None:
    """Write the record into `directory`, which only the user can read. With `recipients` it is one
    encrypted file, always written; without, a folder in the clear, only if something broke."""
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    if recipients is not None:
        out = directory / BUNDLE_NAME
        partial = out.with_suffix(".age.part")
        done = _run(["age", "-R", str(recipients), "-o", str(partial)], input=build_bundle(log, meta), capture_output=True)
        if done.returncode != 0:
            partial.unlink(missing_ok=True)
            raise RegistryError(f"could not encrypt the private record: {done.stderr.decode(errors='replace').strip()[:200]}")
        os.chmod(partial, 0o600)
        os.replace(partial, out)
        return out
    if not log.incidents:
        return None
    folder = directory / f"incidents-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
    for name, data in members(log, meta):
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        path.write_bytes(data)
        os.chmod(path, 0o600)
    os.chmod(folder, 0o700)
    return folder
