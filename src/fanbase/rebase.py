"""`fanbase rebase`: bring a fork up to date with the spec it was forked from.

A fork is a copy of a spec, changed. When the original changes, you want its changes without
losing yours. That is a three-way merge of the fork, the original now, and the original as it
was when the fork was made, the common ancestor. `fanbase fork` records the hash of the
original in `derived_sha256` and keeps a copy of it; the merge itself is done by
`git merge-file`, so conflicts look like the ones git makes.

Where the common ancestor comes from, in this order, always checked against `derived_sha256`:
the copy kept when the fork was made; the fork's own git history; an older release of the
registry the original is in, given with --base-registry.

The fork's metadata is merged the same way, for the few keys a fork usually keeps from its original
(`extends`, `fandango`, `pip`, `extensions`): `fork` records what the original said then, in the fork's
`derived_meta`, so what the original changed since is known, and can be added to the fork's own: a list
gets the original's additions and loses what the original dropped, and a value is taken if only the
original changed it, and said to be in conflict if both did. It is offered, not done behind your back.
"""

from __future__ import annotations

import copy
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from fanbase import contrib
from fanbase.bases import load_base, save_base, sha256
from fanbase.config import Config
from fanbase.context import Context
from fanbase.contrib import _requalified, checkout, open_base, refresh
from fanbase.deps import identity, split_constraint
from fanbase.manifest import MERGEABLE_KEYS
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
_LISTS = ("extends", "pip", "extensions")


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


@dataclass
class MetaChange:
    key: str
    text: str  # what it is, for the person
    adopt: bool = True  # False: a conflict, which is only said
    value: object = None  # what the fork's metadata would hold for `key` (None: no such key)


def _as_list(value: object) -> list:
    return list(value) if isinstance(value, list) else ([] if value is None else [value])


def _translate(key: str, items: list, upstream: RegistryBase, owner: str) -> list:
    """The original's `extends`, written as they must be in the registry the fork is in."""
    return _requalified(items, upstream, owner) if key == "extends" and items else items


def merge_metadata(entry: Entry, upstream: RegistryBase, original: Entry, owner: str,
                   take_all: set[str]) -> tuple[list[MetaChange], list[str]]:
    """What the original changed in the metadata a fork keeps, as changes to the fork's own, and notes
    for what could not be worked out. Nothing is written."""
    changes: list[MetaChange] = []
    notes: list[str] = []
    recorded = entry.meta.get("derived_meta")
    for key in MERGEABLE_KEYS:
        theirs, ours = original.meta.get(key), entry.meta.get(key)
        try:
            if key in take_all:
                value = _translate(key, _as_list(theirs), upstream, owner) if key in _LISTS else theirs
                value = value or None
                if value != ours:
                    changes.append(MetaChange(key, f"{key}: takes the original's {value!r} (the fork had {ours!r})", True, value))
                continue
            if not isinstance(recorded, dict):
                continue  # forked before the fork kept what the original said: it is only said what differs
            base = recorded.get(key)
            if theirs == base:
                continue
            if key in _LISTS:
                added = [x for x in _as_list(theirs) if x not in _as_list(base)]
                dropped = [x for x in _as_list(base) if x not in _as_list(theirs)]
                added, dropped = _translate(key, added, upstream, owner), _translate(key, dropped, upstream, owner)
                merged = [x for x in _as_list(ours) if x not in dropped] + [x for x in added if x not in _as_list(ours)]
                if merged != _as_list(ours):
                    said = ", ".join([f"adds {x!r}" for x in added if x not in _as_list(ours)] +
                                     [f"drops {x!r}" for x in dropped if x in _as_list(ours)])
                    changes.append(MetaChange(key, f"{key}: the original now {said}", True, merged or None))
            elif ours == base:
                changes.append(MetaChange(key, f"{key}: the original changed it from {base!r} to {theirs!r}, and you did not", True, theirs))
            elif ours != theirs:
                changes.append(MetaChange(
                    key, f"{key}: the original changed it from {base!r} to {theirs!r} and the fork has {ours!r}: "
                         "metadata.yml is yours to settle", False))
        except RegistryError as exc:
            notes.append(f"{key}: {exc}")
    return changes, notes


def _ask(question: str) -> bool:
    return input(question).strip().lower() in ("y", "yes")


def cmd_rebase(args, ctx: Context) -> int:
    """Merge what changed in the spec a fork was made from into the fork."""
    reg = checkout(ctx)
    entry = reg.resolve(args.ref)
    derived = entry.meta.get("derived_from")
    if not derived:
        raise RegistryError(f"{entry} is not a fork: it has no derived_from (`fanbase fork` makes one)")
    take_all = {k.strip() for k in (args.adopt or "").split(",") if k.strip()}
    if unknown := take_all - set(MERGEABLE_KEYS):
        raise RegistryError(f"--adopt takes some of {', '.join(MERGEABLE_KEYS)}, not {', '.join(sorted(unknown))}")
    upstream, original, ref = find_upstream(reg, ctx, str(derived), args.upstream)
    if upstream is reg and original == entry:
        raise RegistryError(f"{entry} was made from itself")

    theirs = upstream.read(original)
    new_digest = sha256(theirs)
    old_digest = entry.meta.get("derived_sha256")
    version = original.meta.get("version")
    named = f"{ref}@{version}" if version is not None else ref
    changes, trouble = merge_metadata(entry, upstream, original, identity(reg), take_all)
    if old_digest == new_digest and not changes and not trouble:
        print(f"{entry} is up to date with {named}")
        return 0
    file_changed = old_digest != new_digest

    fan = reg.root / entry.path
    ours = fan.read_bytes()
    _, rest = split_prefix(ref)
    merged, conflicts, how, source = ours, 0, "", ""
    if file_changed:
        base, source = find_base(reg.root, entry.path, old_digest, args.base_registry, rest)
        if ours == base:
            merged, conflicts, how = theirs, 0, "you had changed nothing, so it is the original's now"
        else:
            merged, conflicts = merge3(
                ours, base, theirs, (f"{entry.kind}.fan (yours)", f"{ref}@{str(derived).partition('@')[2] or '?'} (what you started from)", f"{named} (now)")
            )
            how = "merged cleanly" if not conflicts else f"{conflicts} conflict(s): look for <<<<<<< in {entry.path}"
    was = str(derived).partition("@")[2]

    legacy = not isinstance(entry.meta.get("derived_meta"), dict)
    notes = _metadata_notes(reg, entry, upstream, original, only=("mime", "license") if not legacy else None) + trouble
    if not was:
        print(f"{entry} onto {named}")
    elif was == str(version):
        print(f"{entry} onto {named}, which has changed but is still at version {was}")
    else:
        print(f"{entry} onto {named}, which it was made from at {was}")
    if file_changed:
        print(f"  common ancestor: {source}")
        print(f"  {how}")
    else:
        print("  its file is the original's file as it was: only the metadata differs")
    for change in changes:
        print(clean(f"  metadata: {change.text}" + ("" if change.adopt else " (a conflict: not touched)")))
    for note in notes:
        print(clean(f"  note: {note}"))

    adoptable = [c for c in changes if c.adopt]
    adopt = bool(adoptable) and (bool(take_all) or args.metadata == "adopt")
    held = False  # changes the person has not decided about
    if adoptable and not adopt and args.metadata == "ask" and not args.dry_run:
        if sys.stdin.isatty():
            adopt = _ask("Adopt these changes in metadata.yml? [y/N] ")
        else:
            held = True
    if args.dry_run:
        print("(dry run: nothing was written)")
        return 1 if conflicts else 0

    folder = fan.parent
    meta_file = folder / "metadata.yml"
    before = (ours, meta_file.read_text(encoding="utf-8"))
    meta = yaml.safe_load(before[1]) or {}
    meta["derived_from"] = named
    meta["derived_sha256"] = new_digest
    if adopt:
        for change in adoptable:
            if change.value is None:
                meta.pop(change.key, None)
            else:
                meta[change.key] = change.value
    if not held:  # what the original says now is what the next rebase starts from
        meta["derived_meta"] = {k: copy.deepcopy(original.meta[k]) for k in MERGEABLE_KEYS if original.meta.get(k) is not None}
    try:
        fan.write_bytes(merged)
        meta_file.write_text(yaml.safe_dump(meta, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n")
        refresh(reg)
    except RegistryError as exc:  # e.g. an adopted `extends` names a spec this registry does not have
        fan.write_bytes(before[0])
        meta_file.write_text(before[1], encoding="utf-8", newline="\n")
        raise RegistryError(f"{exc}; nothing was changed") from None
    if file_changed:
        save_base(theirs)  # the next rebase starts from here
    print(f"updated {entry.path} and its metadata.yml (derived_from, derived_sha256)" if file_changed
          else "updated its metadata.yml")
    if adopt and adoptable:
        print(f"adopted from the original: {', '.join(c.key for c in adoptable)}")
    if held:
        print("the metadata changes above are not adopted yet: `fanbase rebase "
              f"{entry.kind} --metadata adopt` takes them, `--metadata keep` leaves them out")
    elif adoptable and not adopt:
        print("left as they are: the fork's metadata is its own")
    if conflicts:
        print("fix the conflicts, then `fanbase check`; it refuses a spec that still has them")
    if file_changed or adopt:
        print("give the fork a new `version` before you publish it")
    return 1 if conflicts else 0


def _metadata_notes(reg: Registry, entry: Entry, upstream: RegistryBase, original: Entry,
                    only: tuple[str, ...] | None = None) -> list[str]:
    """What the original's metadata says that the fork's does not, for the keys that are not merged (all of
    them, for a fork that did not record what the original said)."""
    notes = []
    if only is None:
        try:
            theirs = _requalified(list(original.meta.get("extends") or []), upstream, identity(reg))
        except RegistryError:
            theirs = None
        ours = list(entry.meta.get("extends") or [])
        if theirs is not None and theirs != ours:
            notes.append(f"the original now extends {theirs or 'nothing'}, and the fork extends {ours or 'nothing'}; metadata.yml is yours to change")
    for key in only if only is not None else _WATCHED:
        if original.meta.get(key) != entry.meta.get(key) and key in original.meta:
            notes.append(f"the original's {key} is {original.meta.get(key)!r}, the fork's is {entry.meta.get(key)!r}")
    return notes
