"""Installing specs from a registry into Fandango's data directory, and keeping them current.

An installed spec is `<root>/<format>/<kind>.fan`, next to a small `<kind>.yml` copy of its
metadata. The root is one of the directories Fandango searches for `include()`, so an
installed spec can be included, or given to `fandango -F`, straight away.

Nothing here needs the whole registry: a spec is fetched when it is asked for, and
re-fetched only when the registry says it changed.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from fanbase.registry import (
    Entry,
    RegistryBase,
    RegistryError,
    RegistryUnavailable,
    read_metadata,
)

SPEC_SUFFIX = ".fan"
META_SUFFIX = ".yml"


class RequirementsError(RegistryError):
    """The Python packages a spec needs could not be installed."""


@dataclass(frozen=True)
class Installed:
    """A spec on disk, and what had to happen to get it there."""

    format: str
    kind: str
    path: Path
    meta: dict
    status: str  # "installed", "updated", "current", or "offline" (registry unreachable)

    def __str__(self) -> str:
        return f"{self.format}/{self.kind}"

    @property
    def requires(self) -> list[str]:
        return list(self.meta.get("requires") or [])

    @property
    def extensions(self) -> list[str]:
        return list(self.meta.get("extensions") or [])


def install_root() -> Path:
    """Where Fandango's `include()` will find installed specs.

    Mirrors Fandango's own search order, so an installed spec is importable with no
    further configuration.
    """
    if fandango_path := os.environ.get("FANDANGO_PATH"):
        first = fandango_path.split(os.pathsep)[0]
        if first:
            return Path(first)
    if xdg := os.environ.get("XDG_DATA_HOME"):
        return Path(xdg) / "fandango"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Fandango"
    return Path.home() / ".local" / "share" / "fandango"


def spec_path(root: Path, fmt: str, kind: str) -> Path:
    return root / fmt / f"{kind}{SPEC_SUFFIX}"


def _meta_path(path: Path) -> Path:
    return path.with_suffix(META_SUFFIX)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _entry_sha(reg: RegistryBase, entry: Entry) -> str:
    """The hash of the registry's copy; from the index when it has one, else by reading."""
    return entry.sha256 or _sha256(reg.read(entry))


def install(reg: RegistryBase, entry: Entry, root: Path | None = None) -> Installed:
    """Put one spec under `root`, unless the copy there is already the registry's."""
    root = root or install_root()
    target = spec_path(root, entry.format, entry.kind)
    meta_file = _meta_path(target)

    local = target.read_bytes() if target.is_file() else None
    if local is not None and _sha256(local) == _entry_sha(reg, entry):
        status = "current"
        raw = local
    else:
        raw = reg.read(entry)
        _write_atomic(target, raw)
        status = "installed" if local is None else "updated"

    meta = dict(entry.meta)
    meta.update(format=entry.format, kind=entry.kind, sha256=_sha256(raw))
    text = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
    if not meta_file.is_file() or meta_file.read_text(encoding="utf-8") != text:
        _write_atomic(meta_file, text.encode("utf-8"))
    return Installed(entry.format, entry.kind, target, meta, status)


def installed_copy(ref: str, root: Path | None = None) -> Installed | None:
    """An installed spec by name, without asking any registry. `png`, `png/png-apng`, `png-apng`."""
    root = root or install_root()
    if "/" in ref:
        fmt, _, kind = ref.partition("/")
    else:
        fmt, kind = ref.split("-", 1)[0], ref
    path = spec_path(root, fmt, kind)
    if not path.is_file():
        return None
    return Installed(fmt, kind, path, read_metadata(_meta_path(path)), "offline")


def installed_specs(reg: RegistryBase, root: Path | None = None) -> list[Entry]:
    """Registry entries that are installed under `root`."""
    root = root or install_root()
    return [
        e for fmt in reg.formats() for e in reg.kinds(fmt)
        if spec_path(root, e.format, e.kind).is_file()
    ]


def requirements_to_install(meta: dict) -> list[str]:
    """What still has to be installed, as arguments for pip, for a spec with this metadata.

    A spec's `requires` lists the modules it imports, which is not always the name of the
    package that provides them (`yaml` comes from `pyyaml`). If a spec's metadata has a
    `pip` list, those requirements are installed instead; without one, the module names are
    taken to be package names. Only what is not installed yet is returned.
    """
    if pip_specs := meta.get("pip"):
        return [spec for spec in pip_specs if not _distribution_installed(spec)]
    return [
        module for module in meta.get("requires") or []
        if importlib.util.find_spec(module.split(".")[0]) is None
    ]


def _distribution_installed(requirement: str) -> bool:
    name = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement.strip())
    if name is None:
        return False
    try:
        importlib.metadata.distribution(name.group())
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def install_requirements(requirements: list[str]) -> None:
    """Install packages into the environment fanbase runs in, which is Fandango's too."""
    if not requirements:
        return
    if importlib.util.find_spec("pip") is not None:
        command = [sys.executable, "-m", "pip", "install", *requirements]
    elif uv := shutil.which("uv"):
        command = [uv, "pip", "install", "--python", sys.executable, *requirements]
    else:
        raise RequirementsError(
            f"cannot install {' '.join(requirements)}: this environment has neither pip nor uv"
        )
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()[-3:]
        raise RequirementsError(
            f"could not install {' '.join(requirements)}: " + " | ".join(detail)
            + f" (command: {' '.join(command)})"
        )


def ensure_requirements(done: Installed) -> list[str]:
    """Install the packages a spec needs, if they are missing. Returns what was installed."""
    missing = requirements_to_install(done.meta)
    install_requirements(missing)
    return missing


def ensure(
    ref: str,
    reg: RegistryBase | None = None,
    root: Path | None = None,
    requirements: bool = True,
) -> Installed:
    """The spec `ref`, installed and current, with the Python packages it needs.
    This is what `fandango -F` calls.

    If the registry cannot be reached but the spec is already installed, the installed
    copy is used (status "offline"), so a fuzzing run does not depend on the network.
    Packages the spec needs are installed if they are missing, unless `requirements` is
    False.
    """
    root = root or install_root()
    try:
        if reg is None:
            from fanbase.source import find_registry

            reg = find_registry(None)
        done = install(reg, reg.resolve(ref), root)
    except RegistryUnavailable:
        done = installed_copy(ref, root)
        if done is None:
            raise
    if requirements:
        ensure_requirements(done)
    return done
