"""Finding the registry a command should talk to."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from fanbase.manifest import INDEX_FILENAME
from fanbase.registry import Registry, RegistryBase, RegistryError
from fanbase.remote import RemoteRegistry
from fanbase.signing import DEFAULT_SIGNERS, Key, parse_key

ENV_REGISTRY = "FANBASE_REGISTRY"

# Used when nothing else is given or found: the public registry.
DEFAULT_REGISTRY = "https://github.com/fandango-fuzzer/fanbase"


def is_official(url: str) -> bool:
    """Is this the public registry, at any ref (`.../tree/<tag>`)?"""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "github.com":
        return False
    segments = [seg for seg in parts.path.split("/") if seg]
    if len(segments) < 2:
        return False
    official = [seg.lower() for seg in urlsplit(DEFAULT_REGISTRY).path.split("/") if seg]
    return [segments[0].lower(), segments[1].removesuffix(".git").lower()] == official


def signers_for(url: str) -> tuple[Key, ...]:
    """The keys the index of this registry has to be signed by: the public registry's, if it is that; none otherwise
    (a registry the user added has the ones the user pinned: see config)."""
    return tuple(parse_key(key) for key in DEFAULT_SIGNERS) if is_official(url) else ()


def moves(url: str) -> bool:
    """Does reading this registry give different answers over time? A branch does; a tag or a
    commit does not, as far as the URL tells."""
    parts = urlsplit(url)
    if parts.netloc != "github.com":
        return True  # a local path, or a host we know nothing about
    segments = [seg for seg in parts.path.split("/") if seg]
    if len(segments) >= 4 and segments[2] == "tree":
        ref = "/".join(segments[3:])
        looks_like_a_commit = len(ref) >= 7 and all(c in "0123456789abcdef" for c in ref.lower())
        return not looks_like_a_commit and ref in ("main", "master", "dev", "develop", "HEAD")
    return True


@dataclass(frozen=True)
class Location:
    """Where the registry is: a local checkout (`path`) or a URL to read it from (`url`)."""

    kind: str  # "path" or "url"
    value: str

    def __str__(self) -> str:
        return self.value


def locate_registry(explicit: str | None = None, *, local: bool = False) -> Location:
    """Say where the registry is, in this order:

    1. `explicit` (the --registry flag)
    2. $FANBASE_REGISTRY
    3. the current directory or one of its parents, if it is a registry checkout
    4. the public registry, DEFAULT_REGISTRY

    Either of the first two may be a URL instead of a path: a GitHub repo (optionally
    `/tree/<ref>`), or any host serving raw file bytes. It is then read remotely, from its
    `index.yml`, rather than from a local checkout. The first one that is given wins.

    `local=True` is for maintainers' commands that write to a checkout: only a path is
    accepted, and a directory with a `specs/` folder counts even before its first reindex.
    """
    for source in (explicit, os.environ.get(ENV_REGISTRY)):
        if not source:
            continue
        if source.startswith(("http://", "https://")):
            if local:
                raise RegistryError("this command needs a local registry checkout, not a URL")
            return Location("url", source)
        if (Path(source) / "specs").is_dir():
            return Location("path", source)
        raise RegistryError(f"{source} has no specs/ directory")

    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        # Without the marker, any project that happens to have a specs/ folder would match.
        if (candidate / "specs").is_dir() and (local or (candidate / INDEX_FILENAME).is_file()):
            return Location("path", str(candidate))

    if local:
        raise RegistryError("no registry checkout found here; run inside one, or pass --registry PATH")
    return Location("url", DEFAULT_REGISTRY)


def open_registry(location: Location, token: str | None = None) -> RegistryBase:
    if location.kind == "path":
        return Registry(Path(location.value))
    if signers := signers_for(location.value):
        return RemoteRegistry(location.value, token, signers)
    return RemoteRegistry(location.value, token) if token else RemoteRegistry(location.value)


def find_registry(explicit: str | None = None, *, local: bool = False) -> RegistryBase:
    """Locate a registry (see `locate_registry`) and open it."""
    return open_registry(locate_registry(explicit, local=local))
