"""Finding the registry a command should talk to."""

from __future__ import annotations

import os
from pathlib import Path

from fanbase.manifest import INDEX_FILENAME
from fanbase.registry import Registry, RegistryBase, RegistryError
from fanbase.remote import RemoteRegistry

ENV_REGISTRY = "FANBASE_REGISTRY"

# Used when nothing else is given or found: the public registry.
DEFAULT_REGISTRY = "https://github.com/fandango-fuzzer/fanbase"


def find_registry(explicit: str | None = None, *, local: bool = False) -> RegistryBase:
    """Locate a registry, in this order:

    1. `explicit` (the --registry flag)
    2. $FANBASE_REGISTRY
    3. the current directory or one of its parents, if it is a registry checkout
    4. the public registry, DEFAULT_REGISTRY

    Either of the first two may be a URL instead of a path: a GitHub repo (optionally
    `/tree/<ref>`), or any host serving raw file bytes. It is then read remotely, from its
    `index.yml`, rather than from a local checkout.

    `local=True` is for maintainers' commands that write to a checkout: only a path is
    accepted, and a directory with a `specs/` folder counts even before its first reindex.
    """
    sources = [explicit, os.environ.get(ENV_REGISTRY)]
    for source in sources:
        if source and source.startswith(("http://", "https://")):
            if local:
                raise RegistryError("this command needs a local registry checkout, not a URL")
            return RemoteRegistry(source)

    for source in sources:
        if source:
            if (Path(source) / "specs").is_dir():
                return Registry(Path(source))
            raise RegistryError(f"{source} has no specs/ directory")

    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        # Without the marker, any project that happens to have a specs/ folder would match.
        if (candidate / "specs").is_dir() and (local or (candidate / INDEX_FILENAME).is_file()):
            return Registry(candidate)

    if local:
        raise RegistryError("no registry checkout found here; run inside one, or pass --registry PATH")
    return RemoteRegistry(DEFAULT_REGISTRY)
