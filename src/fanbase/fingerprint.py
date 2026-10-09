"""What an evaluation of a spec depends on, as one hash.

Evaluating a spec means asking Fandango for inputs and asking the parsers of its format about them. If the spec, what it
extends, the parsers, Fandango, fanbase and the settings are the same as when it was last evaluated, the result can only be
the same (but for how fast it went, which is the machine's), and `fanbase evaluate --reuse` need not do it again.

What is in the fingerprint:

- the spec and everything it extends (`png-apng` builds on `png`: when `png` changes, so does the fingerprint of `png-apng`),
  each by the hash of its file;
- each target that judges it: every file of its folder (`target.yml`, its harness), and the version it reports (a parser that is
  upgraded, or that is not there any more);
- Fandango's version and fanbase's;
- the settings that change what comes out: the number of inputs, the seed, the time Fandango gets, whether coverage is measured
  and after how many inputs.

What is not: the Python packages that a spec imports (a package that changes its output changes it without the spec changing),
and anything about the machine that no version string says. That is what a full evaluation without `--reuse` at each release
is for.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fanbase.deps import closure
from fanbase.manager import entry_sha
from fanbase.targets import Target, unavailable, version_of

MAX_TARGET_BYTES = 4 * 1024 * 1024  # a target's folder is a few small files; past this, only its size is counted


def directory_digest(directory: Path) -> str:
    """A hash of the files of a folder, by name and content (not of hidden files or caches)."""
    digest = hashlib.sha256()
    left = MAX_TARGET_BYTES
    files = sorted(p for p in directory.rglob("*")
                   if p.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in p.relative_to(directory).parts))
    for path in files:
        digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
        size = path.stat().st_size
        if size <= left:
            digest.update(path.read_bytes())
            left -= size
        else:
            digest.update(f"[{size} bytes]".encode())
        digest.update(b"\0")
    return digest.hexdigest()


def of(reg, entry, ctx, targets: list[Target], settings: dict) -> str:
    """The fingerprint of evaluating `entry` against `targets` with these settings."""
    parts = {
        "specs": sorted((str(e), entry_sha(r, e)) for r, e in closure(ctx, reg, entry)),
        "targets": sorted(
            (t.name, directory_digest(t.directory), "unavailable" if unavailable(t) is not None else version_of(t))
            for t in targets),
        "settings": settings,
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()
