"""Copies of the specs that were forked, kept so that `fanbase rebase` has a common ancestor.

Named by their hash, so that a copy cannot be taken for another one: what is read back is
checked against its name.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

ENV_CACHE = "FANBASE_CACHE"


def cache_dir() -> Path:
    """Where copies of the specs that were forked are kept, named by their hash: $FANBASE_CACHE,
    else $XDG_CACHE_HOME/fanbase, else ~/.cache/fanbase."""
    if explicit := os.environ.get(ENV_CACHE):
        return Path(explicit)
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "fanbase"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_base(data: bytes) -> str:
    """Keep a copy of a spec's file, so that it can be the common ancestor of a merge later.
    Returns its hash, which is its name."""
    digest = sha256(data)
    path = cache_dir() / "bases" / f"{digest}.fan"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{digest[:8]}.")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return digest


def load_base(digest: str) -> bytes | None:
    """The copy kept for this hash, if there is one and it still has that hash."""
    path = cache_dir() / "bases" / f"{digest}.fan"
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data if sha256(data) == digest else None
