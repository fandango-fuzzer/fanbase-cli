"""Installing specs from a registry into Fandango's data directory, and keeping them current.

An installed spec is `<root>/<format>/<kind>.fan`, next to a small `<kind>.yml` copy of its
metadata; one from a registry the user added is `<root>/<registry>/<format>/<kind>.fan`. The
root is one of the directories Fandango searches for `include()`, so an installed spec can
be included, or given to `fandango -F`, straight away.

Nothing here needs the whole registry: a spec is fetched when it is asked for, and
re-fetched only when the registry says it changed.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import importlib.util
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet

from fanbase.registry import (
    Entry,
    RegistryBase,
    RegistryError,
    RegistryUnavailable,
    REGISTRY_NAME,
    read_metadata,
    split_registry,
)

SPEC_SUFFIX = ".fan"
META_SUFFIX = ".yml"
FANDANGO_DISTRIBUTION = "fandango-fuzzer"

LOG = logging.getLogger("fanbase")


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
    registry: str = ""  # the name of the registry, if not the default one

    def __str__(self) -> str:
        spec = f"{self.format}/{self.kind}"
        return f"{self.registry}:{spec}" if self.registry else spec

    @property
    def include_path(self) -> str:
        """What a spec writes in `include()` to use this one."""
        prefix = f"{self.registry}/" if self.registry else ""
        return f"{prefix}{self.format}/{self.kind}{SPEC_SUFFIX}"

    @property
    def requires(self) -> list[str]:
        return list(self.meta.get("requires") or [])

    @property
    def extensions(self) -> list[str]:
        return list(self.meta.get("extensions") or [])

    @property
    def fandango_mismatch(self) -> str | None:
        return fandango_mismatch(self.meta)


def fandango_mismatch(meta: dict) -> str | None:
    """Why the Fandango that is installed does not suit a spec, or None if it does.

    A spec's `fandango` is a version range ('>=1.3'). Without Fandango installed (the
    client can be used alone) or without a range there is nothing to compare, so nothing
    is reported. This is a warning, not a refusal: a spec often works on older versions
    than the one it was written for.
    """
    wanted = meta.get("fandango")
    if not wanted:
        return None
    try:
        specifier = SpecifierSet(str(wanted))
    except InvalidSpecifier:
        return f"cannot read its fandango version range {wanted!r}"
    try:
        have = importlib.metadata.version(FANDANGO_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return None
    if specifier.contains(have, prereleases=True):
        return None
    return f"is written for fandango {wanted}, and fandango {have} is installed"


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


def spec_path(root: Path, fmt: str, kind: str, registry: str = "") -> Path:
    """Where a spec is installed under `root`."""
    base = root / registry if registry else root
    return base / fmt / f"{kind}{SPEC_SUFFIX}"


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


def entry_sha(reg: RegistryBase, entry: Entry) -> str:
    """The hash of the registry's copy; from the index when it has one, else by reading."""
    return entry.sha256 or _sha256(reg.read(entry))


def _check_registry_dir(root: Path, registry: str) -> None:
    """A registry's directory must not be the directory of a format of the default registry."""
    folder = root / registry
    if folder.is_dir() and any(folder.glob(f"*{SPEC_SUFFIX}")):
        raise RegistryError(
            f"{folder} holds specs of a format called {registry}, so a registry cannot be "
            "installed under that name"
        )


def install(reg: RegistryBase, entry: Entry, root: Path | None = None) -> Installed:
    """Put one spec under `root`, unless the copy there is already the registry's."""
    root = root or install_root()
    registry = reg.name
    if registry:
        _check_registry_dir(root, registry)
    target = spec_path(root, entry.format, entry.kind, registry)
    meta_file = _meta_path(target)

    local = target.read_bytes() if target.is_file() else None
    if local is not None and _sha256(local) == entry_sha(reg, entry):
        status = "current"
        raw = local
    else:
        raw = reg.read(entry)
        _write_atomic(target, raw)
        status = "installed" if local is None else "updated"

    meta = dict(entry.meta)
    meta.pop("registry", None)  # what a registry says about itself is not for it to decide here
    meta.update(format=entry.format, kind=entry.kind, sha256=_sha256(raw))
    if registry:
        meta["registry"] = registry
    text = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
    if not meta_file.is_file() or meta_file.read_text(encoding="utf-8") != text:
        _write_atomic(meta_file, text.encode("utf-8"))
    return Installed(entry.format, entry.kind, target, meta, status, registry)


def _split_ref(ref: str) -> tuple[str, str, str]:
    """`png` -> ('', png, png); `png/png-apng` and `png-apng` -> ('', png, png-apng);
    `acme:png-strict` -> (acme, png, png-strict)."""
    name, rest = split_registry(ref)
    registry = name or ""
    if "/" in rest:
        fmt, _, kind = rest.partition("/")
        return registry, fmt, kind
    return registry, rest.split("-", 1)[0], rest


def installed_copy(ref: str, root: Path | None = None) -> Installed | None:
    """An installed spec by name, without asking any registry. `png`, `png/png-apng`,
    `png-apng`, `acme:png-strict`."""
    root = root or install_root()
    registry, fmt, kind = _split_ref(ref)
    if registry and not REGISTRY_NAME.fullmatch(registry):
        return None
    path = spec_path(root, fmt, kind, registry)
    if not path.is_file():
        return None
    return Installed(fmt, kind, path, read_metadata(_meta_path(path)), "offline", registry)


def all_installed(root: Path | None = None) -> list[Installed]:
    """Every spec Fanbase installed under `root`, found on disk; no registry is asked.

    A spec counts when the `<kind>.yml` that `install` writes is next to it, and agrees with
    where the spec is. That is what tells it from a spec file someone put in the same
    directory by hand.
    """
    root = root or install_root()
    found = []
    for path in [*root.glob(f"*/*{SPEC_SUFFIX}"), *root.glob(f"*/*/*{SPEC_SUFFIX}")]:
        meta_file = _meta_path(path)
        if not meta_file.is_file():
            continue
        try:
            meta = read_metadata(meta_file)
        except RegistryError:
            continue
        registry = path.parent.parent.name if len(path.relative_to(root).parts) == 3 else ""
        if (
            meta.get("format") == path.parent.name
            and meta.get("kind") == path.stem
            and (meta.get("registry") or "") == registry
        ):
            found.append(Installed(path.parent.name, path.stem, path, meta, "offline", registry))
    return sorted(found, key=lambda done: (done.registry, done.format, done.kind))


def _needed_by(found: list[Installed], root: Path) -> list[tuple[Installed, Installed]]:
    """(spec, installed spec that extends it) for specs about to go that others still need."""
    from fanbase.deps import dependencies, self_names  # noqa: F401  (deps imports the registry only)

    going = {(done.registry, done.format, done.kind): done for done in found}
    blocked = []
    for other in all_installed(root):
        if (other.registry, other.format, other.kind) in going:
            continue
        entry = Entry(other.format, other.kind, "", other.meta)
        try:
            deps = dependencies(entry, other.registry, {other.registry or "fanbase", other.registry})
        except RegistryError:
            continue
        for dep in deps:
            target = going.get((dep.registry, *_split_ref(dep.ref)[1:]))
            if target is not None:
                blocked.append((target, other))
    return blocked


def uninstall(refs: list[str], root: Path | None = None, force: bool = False) -> list[Installed]:
    """Remove installed specs, named as for `install`. Removes all of them, or, if one of
    them is not installed, none. Only specs Fanbase installed are touched.

    A spec that another installed spec extends is not removed, unless `force`: the one that
    extends it would stop working.
    """
    root = root or install_root()
    have = {(done.registry, done.format, done.kind): done for done in all_installed(root)}
    found: list[Installed] = []
    for ref in refs:
        done = have.get(_split_ref(ref))
        if done is None:
            raise RegistryError(f"{ref} is not installed by fanbase under {root}")
        if done not in found:
            found.append(done)
    if not force and (blocked := _needed_by(found, root)):
        needs = "; ".join(f"{spec} is extended by {other}" for spec, other in blocked)
        raise RegistryError(f"{needs}. Remove that one too, or use --force")
    for done in found:
        done.path.unlink(missing_ok=True)
        _meta_path(done.path).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            done.path.parent.rmdir()  # only when nothing else is in it
            if done.registry:
                done.path.parent.parent.rmdir()
    return found


def installed_specs(reg: RegistryBase, root: Path | None = None) -> list[Entry]:
    """Registry entries that are installed under `root`."""
    root = root or install_root()
    return [
        e for fmt in reg.formats() for e in reg.kinds(fmt)
        if spec_path(root, e.format, e.kind, reg.name).is_file()
    ]


# What a spec's metadata may ask pip for: a package name, optional extras, optional version
# specifiers. Nothing else (options, URLs, paths, direct references, markers) is passed on:
# these strings come from a registry, and a registry is not necessarily ours.
_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_SPECIFIER = r"(?:===|==|~=|!=|<=|>=|<|>)[ ]*[A-Za-z0-9.*+!_-]+"
_REQUIREMENT = re.compile(
    rf"{_NAME}(?:\[{_NAME}(?:[ ]*,[ ]*{_NAME})*\])?(?:[ ]*{_SPECIFIER}(?:[ ]*,[ ]*{_SPECIFIER})*)?"
)
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _refuse(value: object, why: str) -> RequirementsError:
    return RequirementsError(f"refusing requirement {value!r} from the spec's metadata: {why}")


def declared_requirements(meta: dict) -> list[str]:
    """What a spec's metadata asks pip for, as arguments for pip, checked.

    A spec's `requires` lists the modules it imports, which is not always the name of the
    package that provides them (`yaml` comes from `pyyaml`). If the metadata has a `pip`
    list, those requirements are what is asked for; without one, the top-level module names
    are taken to be package names.

    Raises RequirementsError for anything that is not a plain package requirement.
    """
    if pip_specs := meta.get("pip"):
        checked = []
        for spec in pip_specs:
            if not isinstance(spec, str) or not _REQUIREMENT.fullmatch(spec.strip()):
                raise _refuse(spec, "only a package name, with optional extras and version "
                                    "specifiers, is allowed")
            checked.append(spec.strip())
        return checked
    names: list[str] = []
    for module in meta.get("requires") or []:
        name = module.split(".")[0] if isinstance(module, str) else module
        if not isinstance(name, str) or not _MODULE.fullmatch(name):
            raise _refuse(module, "not a module name")
        if name not in names:
            names.append(name)
    return names


def requirements_to_install(meta: dict) -> list[str]:
    """What still has to be installed, as arguments for pip, for a spec with this metadata.

    See `declared_requirements`. Only what is not installed yet is returned.
    """
    declared = declared_requirements(meta)
    if meta.get("pip"):
        return [spec for spec in declared if not _distribution_installed(spec)]
    return [name for name in declared if importlib.util.find_spec(name) is None]


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
    try:
        missing = requirements_to_install(done.meta)
    except RequirementsError as exc:
        raise RequirementsError(f"{done}: {exc}") from None
    install_requirements(missing)
    return missing


def install_closure(ctx, reg: RegistryBase, entry: Entry, root: Path | None = None, lock=None) -> list[Installed]:
    """Install a spec and the specs it extends; what it extends comes first, the spec last.

    With a lock, every one of them has to be what the lock says before any is installed.
    """
    from fanbase.deps import closure

    plan = closure(ctx, reg, entry)
    if lock is not None:
        from fanbase.locking import check_locked

        for r, e in plan:
            check_locked(lock, r, e)
    return [install(r, e, root) for r, e in plan]


def ensure(
    ref: str,
    reg: RegistryBase | None = None,
    root: Path | None = None,
    requirements: bool = True,
    ctx=None,
    lock=None,
) -> Installed:
    """The spec `ref`, installed and current, with the Python packages it needs and the
    specs it extends. This is what `fandango -F` calls.

    `ref` may name a registry the user added (`acme:png-strict`), be pinned to one, or carry
    a version range (`png>=1.2`).

    If there is a lock (`fanbase.lock` in the current directory, or the file $FANBASE_LOCK
    names) that has the spec, the spec has to be exactly what the lock says, or this raises:
    a run that was locked is not silently run with a newer spec.

    If the registry cannot be reached but the spec is already installed, the installed
    copy is used (status "offline"), so a fuzzing run does not depend on the network.
    Packages the specs need are installed if they are missing, unless `requirements` is
    False.
    """
    from fanbase.lock import load_lock, lock_path
    from fanbase.locking import check_installed_locked, warn_if_unlocked

    root = root or install_root()
    if ctx is None:
        from fanbase.context import Context

        if lock is None and (path := lock_path()):
            lock = load_lock(path)
        ctx = Context(default=reg)
        if lock is not None:
            ctx = lock_context(lock, reg)
    try:
        registry, entry = ctx.resolve(ref)
        installed = install_closure(ctx, registry, entry, root, lock)
        if lock is not None:
            warn_if_unlocked(lock, installed)
    except RegistryUnavailable:
        done = installed_copy(ctx.pinned(ref), root)
        if done is None:
            raise
        from fanbase.deps import check_version, split_constraint

        check_version(str(done), done.meta, split_constraint(ref)[1], ref.strip())
        if lock is not None:
            check_installed_locked(lock, done)
        installed = [done]
    for each in installed:
        if problem := each.fandango_mismatch:
            LOG.warning("Fanbase: %s %s", each, problem)
        if requirements:
            ensure_requirements(each)
    return installed[-1]


def lock_context(lock, reg: RegistryBase | None):
    """A context that reads the default registry from where the lock says, unless the user
    said otherwise (a registry that is open already, or $FANBASE_REGISTRY)."""
    from fanbase.context import Context
    from fanbase.source import ENV_REGISTRY, is_official

    url = lock.registries.get("")
    if reg is None and url and not os.environ.get(ENV_REGISTRY) and is_official(url):
        return Context(explicit=url)
    return Context(default=reg)
