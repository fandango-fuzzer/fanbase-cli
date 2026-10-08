"""`fanbase rebase`: bring a fork up to date with the spec it was forked from.

A fork is a copy of a spec, changed. When the original changes, you want its changes without
losing yours. That is a three-way merge of the fork, the original now, and the original as it
was when the fork was made, the common ancestor. `fanbase fork` records the hash of the
original in `derived_sha256` and keeps a copy of it; the merge itself is done by
`git merge-file`, so conflicts look like the ones git makes.

Where the common ancestor comes from, in this order, always checked against `derived_sha256`:
the copy kept when the fork was made; the fork's own git history; an older release of the
registry the original is in, given with --base-registry.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from fanbase import contrib
from fanbase.bases import load_base, save_base, sha256
from fanbase.config import Config
from fanbase.context import Context
from fanbase.contrib import _requalified, checkout, open_base, refresh
from fanbase.deps import identity, split_constraint
from fanbase.output import clean
from fanbase.registry import (
    DEFAULT_REGISTRY_NAME,
    Entry,
    Registry,
    RegistryBase,
    RegistryError,
    split_prefix,
)
from fanbase.source import DEFAULT_REGISTRY, ENV_REGISTRY

# Metadata that a fork usually keeps from the original; if the original's changed, say so.
_WATCHED = ("fandango", "pip", "extensions", "mime", "license")


def _git(root: Path, *args: str):
    try:
        return contrib._run(["git", *args], cwd=str(root), capture_output=True)
    except OSError:
        raise RegistryError("rebase needs git, which it uses to merge (`git merge-file`)") from None


def find_upstream(reg: Registry, ctx: Context, derived: str, override: str | None) -> tuple[RegistryBase, Entry, str]:
    """The spec a fork was made from, as the registries have it now. Also the ref, without its version."""
    ref, _ = split_constraint(derived)
    prefix, rest = split_prefix(ref)
    here = identity(reg)
    if override:
        upstream = open_base(override)
    elif prefix is None or prefix == (here or DEFAULT_REGISTRY_NAME):
        upstream = reg  # the original is in this registry too
    elif prefix == DEFAULT_REGISTRY_NAME:
        upstream = Context(explicit=os.environ.get(ENV_REGISTRY) or DEFAULT_REGISTRY, config=Config()).default
    else:
        upstream = ctx.registry(prefix)
    try:
        return upstream, upstream.resolve(rest), ref
    except RegistryError as exc:
        raise RegistryError(f"cannot find what the fork was made from ({derived}): {exc}") from None


def base_from_git(root: Path, relpath: str, digest: str) -> bytes | None:
    """The version of a file, in the history of the checkout, that has this hash."""
    log = _git(root, "log", "--format=%H", "--", relpath)
    if log.returncode != 0:
        return None
    for commit in log.stdout.decode().split():
        shown = _git(root, "show", f"{commit}:./{relpath}")
        if shown.returncode == 0 and sha256(shown.stdout) == digest:
            return shown.stdout
    return None


def find_base(root: Path, relpath: str, digest: str | None, base_registry: str | None, rest: str) -> tuple[bytes, str]:
    """The common ancestor, and where it came from."""
    if digest:
        if (data := load_base(digest)) is not None:
            return data, "the copy kept when it was forked"
        if (data := base_from_git(root, relpath, digest)) is not None:
            return data, "the git history of the fork"
    if base_registry:
        old = open_base(base_registry)
        try:
            data = old.read(old.resolve(rest))
        except RegistryError as exc:
            raise RegistryError(f"{base_registry} does not have the original: {exc}") from None
        if digest and sha256(data) != digest:
            raise RegistryError(
                f"{base_registry} has {rest} as {sha256(data)[:8]}, but the fork was made from {digest[:8]}; "
                "use the release of the registry from the time of the fork"
            )
        return data, f"the registry {base_registry}"
    if digest:
        raise RegistryError(
            f"the original as it was when the fork was made ({digest[:8]}) is not kept on this machine and "
            "is not in the fork's git history. Point --base-registry at the release of the registry the fork was made "
            "from (a tag: https://github.com/<owner>/<repo>/tree/<tag>)"
        )
    raise RegistryError(
        "the fork does not say what it was made from (it was forked before `derived_sha256`); "
        "point --base-registry at the release of the registry it was made from"
    )


def merge3(ours: bytes, base: bytes, theirs: bytes, labels: tuple[str, str, str]) -> tuple[bytes, int]:
    """Merge two changes of a common ancestor: the merged file, and the number of conflicts."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, data in (("ours", ours), ("base", base), ("theirs", theirs)):
            path = Path(tmp) / name
            path.write_bytes(data)
            paths.append(str(path))
        done = _git(Path(tmp), "merge-file", "-p", "-L", labels[0], "-L", labels[1], "-L", labels[2], *paths)
    if done.returncode > 127:
        raise RegistryError(f"git merge-file failed: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout, done.returncode


def cmd_rebase(args, ctx: Context) -> int:
    """Merge what changed in the spec a fork was made from into the fork."""
    reg = checkout(ctx)
    entry = reg.resolve(args.ref)
    derived = entry.meta.get("derived_from")
    if not derived:
        raise RegistryError(f"{entry} is not a fork: it has no derived_from (`fanbase fork` makes one)")
    upstream, original, ref = find_upstream(reg, ctx, str(derived), args.upstream)
    if upstream is reg and original == entry:
        raise RegistryError(f"{entry} was made from itself")

    theirs = upstream.read(original)
    new_digest = sha256(theirs)
    old_digest = entry.meta.get("derived_sha256")
    version = original.meta.get("version")
    named = f"{ref}@{version}" if version is not None else ref
    if old_digest == new_digest:
        print(f"{entry} is up to date with {named}")
        return 0

    fan = reg.root / entry.path
    ours = fan.read_bytes()
    _, rest = split_prefix(ref)
    base, source = find_base(reg.root, entry.path, old_digest, args.base_registry, rest)
    was = str(derived).partition("@")[2]

    if ours == base:
        merged, conflicts, how = theirs, 0, "you had changed nothing, so it is the original's now"
    else:
        merged, conflicts = merge3(
            ours, base, theirs, (f"{entry.kind}.fan (yours)", f"{ref}@{was or '?'} (what you started from)", f"{named} (now)")
        )
        how = "merged cleanly" if not conflicts else f"{conflicts} conflict(s): look for <<<<<<< in {entry.path}"

    notes = _metadata_notes(reg, entry, upstream, original)
    if not was:
        print(f"{entry} onto {named}")
    elif was == str(version):
        print(f"{entry} onto {named}, which has changed but is still at version {was}")
    else:
        print(f"{entry} onto {named}, which it was made from at {was}")
    print(f"  common ancestor: {source}")
    print(f"  {how}")
    for note in notes:
        print(clean(f"  note: {note}"))
    if args.dry_run:
        print("(dry run: nothing was written)")
        return 1 if conflicts else 0

    fan.write_bytes(merged)
    folder = fan.parent
    meta_file = folder / "metadata.yml"
    meta = yaml.safe_load(meta_file.read_text(encoding="utf-8")) or {}
    meta["derived_from"] = named
    meta["derived_sha256"] = new_digest
    meta_file.write_text(yaml.safe_dump(meta, sort_keys=False, allow_unicode=True), encoding="utf-8")
    save_base(theirs)  # the next rebase starts from here
    refresh(reg)
    print(f"updated {entry.path} and its metadata.yml (derived_from, derived_sha256)")
    if conflicts:
        print("fix the conflicts, then `fanbase check`; it refuses a spec that still has them")
    print("give the fork a new `version` before you publish it")
    return 1 if conflicts else 0


def _metadata_notes(reg: Registry, entry: Entry, upstream: RegistryBase, original: Entry) -> list[str]:
    """What the original's metadata says that the fork's does not: the fork's is its own, so
    this is only said."""
    notes = []
    try:
        theirs = _requalified(list(original.meta.get("extends") or []), upstream, identity(reg))
    except RegistryError:
        theirs = None
    ours = list(entry.meta.get("extends") or [])
    if theirs is not None and theirs != ours:
        notes.append(f"the original now extends {theirs or 'nothing'}, and the fork extends {ours or 'nothing'}; metadata.yml is yours to change")
    for key in _WATCHED:
        if original.meta.get(key) != entry.meta.get(key) and key in original.meta:
            notes.append(f"the original's {key} is {original.meta.get(key)!r}, the fork's is {entry.meta.get(key)!r}")
    return notes
