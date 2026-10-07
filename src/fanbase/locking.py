"""Making a lock, and holding the registries to it."""

from __future__ import annotations

import logging
import sys

from fanbase.deps import closure
from fanbase.lock import Lock, LockedSpec
from fanbase.manager import Installed, entry_sha
from fanbase.registry import Entry, RegistryBase, RegistryError
from fanbase.remote import RemoteRegistry
from fanbase.source import is_official

LOG = logging.getLogger("fanbase")


def where(ctx, reg: RegistryBase) -> str:
    """Where a registry is read from, as the lock records it."""
    if reg.name:
        return ctx.config.registries[reg.name].url
    return getattr(reg, "url", None) or str(reg.root)


def build_lock(ctx, refs: list[str]) -> Lock:
    """A lock for these refs and everything they extend, as the registries have them now."""
    pairs = [ctx.resolve(ref) for ref in refs]
    requested = {(reg.name, entry.format, entry.kind) for reg, entry in pairs}
    lock = Lock()
    for reg, entry in pairs:
        for dep_reg, dep in closure(ctx, reg, entry):
            key = (dep_reg.name, dep.format, dep.kind)
            if lock.find(*key) is not None:
                continue
            version = dep.meta.get("version")
            lock.specs.append(
                LockedSpec(dep_reg.name, dep.format, dep.kind, None if version is None else str(version),
                           entry_sha(dep_reg, dep), key in requested)
            )
            lock.registries.setdefault(dep_reg.name, where(ctx, dep_reg))
    return lock


def compare(old: Lock, new: Lock) -> list[str]:
    """What differs between two locks, one line each."""
    lines = []
    for spec in new.specs:
        before = old.find(*spec.key)
        if before is None:
            lines.append(f"  added    {spec} {spec.version or ''}".rstrip())
        elif before.sha256 != spec.sha256:
            lines.append(f"  changed  {spec}: {before.version or '-'} ({before.sha256[:8]}) -> {spec.version or '-'} ({spec.sha256[:8]})")
    for spec in old.specs:
        if new.find(*spec.key) is None:
            lines.append(f"  removed  {spec}")
    return lines


def _changed(spec: LockedSpec, entry: Entry, sha: str) -> str:
    now = entry.meta.get("version")
    return (
        f"{spec} was locked at {spec.version or 'no version'} ({spec.sha256[:8]}), "
        f"and the registry now has {now if now is not None else 'no version'} ({sha[:8]})"
    )


def check_locked(lock: Lock, reg: RegistryBase, entry: Entry, hint: bool = True) -> None:
    """Raise unless the registry's copy of this spec is the one the lock says. A spec the lock
    does not have is not held to anything."""
    spec = lock.find(reg.name, entry.format, entry.kind)
    if spec is None:
        return
    sha = entry_sha(reg, entry)
    if sha != spec.sha256:
        raise RegistryError(_changed(spec, entry, sha) + ("; `fanbase lock` updates the lock on purpose" if hint else ""))


def check_installed_locked(lock: Lock, done: Installed) -> None:
    spec = lock.find(done.registry, done.format, done.kind)
    if spec is not None and done.meta.get("sha256") != spec.sha256:
        raise RegistryError(
            f"the installed {done} is not the one fanbase.lock says ({spec.sha256[:8]}), "
            "and the registry cannot be reached to fetch it"
        )


def warn_if_unlocked(lock: Lock, installed: list[Installed]) -> None:
    for done in installed:
        if lock.find(done.registry, done.format, done.kind) is None:
            LOG.warning("Fanbase: %s is not in the lock, so it is not held to a version", done)


def locked_registry(ctx, lock: Lock, name: str) -> RegistryBase:
    """The registry a lock names, opened the way the user allows."""
    url = lock.registries.get(name)
    if name:
        configured = ctx.config.registries.get(name)
        if configured is None:
            raise RegistryError(
                f"fanbase.lock uses the registry {name} ({url}), which you have not added; "
                f"if you trust it: fanbase registry add {url} --name {name}"
            )
        if url and configured.url != url:
            print(f"warning: you added {name} as {configured.url}, and the lock read it from {url}", file=sys.stderr)
        return ctx.registry(name)
    if ctx.default_is_explicit or url is None:
        return ctx.default
    if is_official(url):
        return ctx.use_default(RemoteRegistry(url))
    if url == where(ctx, ctx.default):
        return ctx.default
    raise RegistryError(
        f"fanbase.lock reads the default registry from {url}, which is not the public registry; "
        f"pass --registry {url} to accept that"
    )
