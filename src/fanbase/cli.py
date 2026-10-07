"""The `fanbase` command.

Browses and installs from Fanbase registries. By default that is the public registry, read
over the network from its `index.yml`; a spec is only fetched when you ask for it. A local
checkout works too, which is what maintainers use. Registries you add are named in front
of a spec: `acme:png/png-strict`.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

from fanbase import __version__
from fanbase.config import RegistryConfig, check_registry_name, check_token_env, save_config
from fanbase.context import Context
from fanbase.deps import closure, dependencies, dependents, label, self_names
from fanbase.manager import (
    Installed,
    all_installed,
    declared_requirements,
    ensure_requirements,
    entry_sha,
    install,
    install_root,
    spec_path,
    uninstall,
)
from fanbase.lock import LOCK_FILENAME, load_lock, lock_path, save_lock
from fanbase.locking import build_lock, check_locked, compare, locked_registry
from fanbase.manifest import INDEX_FILENAME, dump_index, reindex
from fanbase.registry import Registry, RegistryError, split_registry
from fanbase.remote import RemoteRegistry
from fanbase.source import locate_registry, moves

_VERB = {"installed": "installed", "updated": "updated", "current": "up to date", "offline": "kept"}

# Anything a registry tells us may end up on the terminal. Control characters could be
# escape sequences that rewrite what is on screen, so they are shown as `?`.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _clean(text: object) -> str:
    return _CONTROL.sub("?", str(text))


def _report(done: Installed, hint_requirements: bool) -> None:
    print(f"{_VERB[done.status]} {_clean(done)} -> {done.path}")
    if done.status != "current":
        print(f'  include("{_clean(done.include_path)}")')
    if hint_requirements and (needs := declared_requirements(done.meta)):
        print(f"  requires: pip install {' '.join(needs)}")


def _warn_fandango(results: list[Installed]) -> None:
    for done in results:
        if problem := done.fandango_mismatch:
            print(f"warning: {_clean(done)} {_clean(problem)}", file=sys.stderr)


def _install_requirements(results: list[Installed], args) -> None:
    """Install what the specs need, once for all of them; or only say what is needed."""
    if args.no_requirements:
        needs = sorted({package for done in results for package in declared_requirements(done.meta)})
        if needs and args.all:
            print(f"requires: pip install {' '.join(needs)}")
        return
    installed: list[str] = []
    for done in results:
        for requirement in ensure_requirements(done):
            if requirement not in installed:
                installed.append(requirement)
                print(f"installed requirement {requirement}")


def _install_specs(pairs, root: Path | None = None) -> list[Installed]:
    """Install specs and what they extend, each once, what is extended before what extends it."""
    seen: set[tuple[str, str, str]] = set()
    results = []
    for reg, entry, ctx in pairs:
        for dep_reg, dep_entry in closure(ctx, reg, entry):
            key = (dep_reg.name, dep_entry.format, dep_entry.kind)
            if key not in seen:
                seen.add(key)
                results.append(install(dep_reg, dep_entry, root))
    return results


def _list_installed(where: str | None) -> int:
    """What is installed; `where` narrows it: `png`, `acme:` (a whole registry), `acme:png`."""
    name, fmt = split_registry(where) if where else (None, "")
    have = [
        done for done in all_installed()
        if (not where or done.registry == (name or "")) and (not fmt or done.format == fmt)
    ]
    if not have:
        print("nothing installed")
        return 0
    width = max(len(str(done)) for done in have)
    for done in have:
        print(f"  {_clean(done):<{width}}  {_clean(done.meta.get('description') or '')}".rstrip())
    return 0


def cmd_list(args, ctx: Context) -> int:
    if args.installed:
        return _list_installed(args.format)
    name, fmt = split_registry(args.format) if args.format else (None, "")
    reg = ctx.registry(name)
    root = install_root()
    if fmt:
        if fmt not in reg.formats():
            raise RegistryError(f"unknown format: {_clean(fmt)}")
        entries = reg.kinds(fmt)
        width = max(len(e.kind) for e in entries)
        for e in entries:
            have = "*" if spec_path(root, e.format, e.kind, reg.name).is_file() else " "
            notes = [e.meta.get("description"), e.requires and f"(requires: {', '.join(e.requires)})"]
            print(_clean(f" {have}{e.kind:<{width}}  {' '.join(n for n in notes if n)}").rstrip())
        print("\n* installed")
        return 0

    formats = reg.formats()
    width = max((len(f) for f in formats), default=0)
    for f in formats:
        n = len(reg.kinds(f))
        print(_clean(f"  {f:<{width}}  {n} spec{'' if n == 1 else 's'}"))
    return 0


def cmd_show(args, ctx: Context) -> int:
    reg, entry = ctx.resolve(args.ref)
    meta = {"format": entry.format, "kind": entry.kind, "path": entry.path, **entry.meta}
    if reg.name:
        meta = {"registry": reg.name, **meta}
    print(yaml.safe_dump(meta, sort_keys=False, allow_unicode=True), end="")
    return 0


def _install_locked(args, ctx: Context) -> int:
    """Install exactly what the lock says; install nothing if a registry has it any other way."""
    if args.refs or args.all or args.source:
        raise RegistryError("--locked installs what the lock says; name no specs")
    path = lock_path(args.lockfile)
    if path is None:
        raise RegistryError(f"no {LOCK_FILENAME} here; `fanbase lock SPEC...` makes one")
    lock = load_lock(path)
    registries = {name: locked_registry(ctx, lock, name) for name in sorted({s.registry for s in lock.specs})}
    plan, problems = [], []
    for spec in lock.specs:
        reg = registries[spec.registry]
        try:
            entry = reg.resolve(f"{spec.format}/{spec.kind}")
        except RegistryError as exc:
            problems.append(f"{spec}: {exc}")
            continue
        try:
            check_locked(lock, reg, entry, hint=False)
        except RegistryError as exc:
            problems.append(str(exc))
            continue
        plan.append((reg, entry))
    if problems:
        raise RegistryError(
            f"the registries no longer match {path}:\n  " + "\n  ".join(problems)
            + "\nread an older release of the registry, or update the lock with `fanbase lock`"
        )
    root = Path(args.into) if args.into else install_root()
    results = [install(reg, entry, root) for reg, entry in plan]
    for done in results:
        print(f"{_VERB[done.status]} {_clean(done)}")
    counts = Counter(_VERB[done.status] for done in results)
    summary = ", ".join(f"{n} {verb}" for verb, n in counts.items())
    print(f"{len(results)} specs from {path}: {summary} in {root}")
    _warn_fandango(results)
    _install_requirements(results, args)
    return 0


def cmd_lock(args, ctx: Context) -> int:
    path = Path(args.lockfile) if args.lockfile else Path(LOCK_FILENAME)
    existing = load_lock(path) if path.is_file() else None
    refs = args.refs or ([str(spec) for spec in existing.requested] if existing else [])
    if not refs:
        raise RegistryError("name the specs to lock, e.g. `fanbase lock png`")
    fresh = build_lock(ctx, refs)

    if args.check:
        if existing is None:
            raise RegistryError(f"{path} does not exist; `fanbase lock {' '.join(refs)}` makes it")
        changes = compare(existing, fresh)
        if changes:
            print(f"{path} is out of date:")
            print("\n".join(changes))
            print("run `fanbase lock` to update it")
            return 1
        print(f"{path} is up to date")
        return 0

    save_lock(fresh, path)
    print(f"locked {len(fresh.specs)} specs in {path}")
    if existing is not None:
        for line in compare(existing, fresh):
            print(line)
    for name, where in sorted(fresh.registries.items()):
        if moves(where):
            print(
                f"warning: the registry {name or 'fanbase'} was read from {_clean(where)}, which changes over time: "
                "once a spec changes there, the locked one can no longer be fetched. Lock against a release "
                "instead: --registry https://github.com/fandango-fuzzer/fanbase/tree/<tag>",
                file=sys.stderr,
            )
    return 0


def cmd_install(args, ctx: Context) -> int:
    if args.locked:
        return _install_locked(args, ctx)
    if args.all == bool(args.refs):
        raise RegistryError("name the specs to install, or give --all (not both)")
    if args.source and not args.all:
        raise RegistryError("--from goes with --all")
    root = Path(args.into) if args.into else install_root()

    if args.all:
        reg = ctx.registry(args.source)
        results = _install_specs(
            [(reg, e, ctx) for fmt in reg.formats() for e in reg.kinds(fmt)], root
        )
        for done in results:
            print(f"{_VERB[done.status]} {_clean(done)}")
        counts = Counter(_VERB[done.status] for done in results)
        summary = ", ".join(f"{n} {verb}" for verb, n in counts.items())
        print(f"{len(results)} specs: {summary} in {root}")
    else:
        results = _install_specs([(*ctx.resolve(ref), ctx) for ref in args.refs], root)
        for done in results:
            _report(done, hint_requirements=args.no_requirements)
    _warn_fandango(results)
    _install_requirements(results, args)
    return 0


def cmd_update(args, ctx: Context) -> int:
    if args.refs:
        pairs = [ctx.resolve(ref) for ref in args.refs]
    else:
        pairs = []
        for have in all_installed():
            reg = ctx.registry(have.registry or None)
            try:
                pairs.append((reg, reg.resolve(f"{have.format}/{have.kind}")))
            except RegistryError:
                print(f"warning: {_clean(have)} is no longer in its registry; kept", file=sys.stderr)
        if not pairs:
            print("nothing installed" if not all_installed() else "nothing to update")
            return 0
    results = _install_specs([(reg, entry, ctx) for reg, entry in pairs])
    for done in results:
        _report(done, hint_requirements=args.no_requirements)
    _warn_fandango(results)
    _install_requirements(results, args)
    return 0


def cmd_uninstall(args, ctx: Context) -> int:
    for done in uninstall(args.refs, force=args.force):
        print(f"removed {_clean(done)} ({done.path})")
    return 0


def _node(reg, entry) -> str:
    version = entry.meta.get("version")
    return _clean(label(reg, entry) + (f"  {version}" if version else ""))


def cmd_deps(args, ctx: Context) -> int:
    """What a spec extends, as a tree; with --reverse, what extends it."""
    reg, entry = ctx.resolve(args.ref)

    def forward(reg, entry):
        for dep in dependencies(entry, reg.name, self_names(reg)):
            try:
                dep_reg = ctx.registry(dep.registry or None)
                yield dep_reg, dep_reg.resolve(dep.ref), None
            except RegistryError as exc:
                yield None, None, f"{dep}  (cannot be found: {exc})"

    def reverse(reg, entry):
        for other, found in dependents(ctx, reg, entry):
            yield other, found, None

    children = reverse if args.reverse else forward

    def walk(reg, entry, prefix: str, trail: tuple) -> None:
        kids = list(children(reg, entry))
        for i, (kid_reg, kid, problem) in enumerate(kids):
            branch = "└── " if i == len(kids) - 1 else "├── "
            if problem:
                print(prefix + branch + _clean(problem))
                continue
            key = (kid_reg.name, kid.format, kid.kind)
            circular = key in trail
            print(prefix + branch + _node(kid_reg, kid) + ("  (circular)" if circular else ""))
            if not circular:
                walk(kid_reg, kid, prefix + ("    " if i == len(kids) - 1 else "│   "), (*trail, key))

    print(_node(reg, entry))
    walk(reg, entry, "", ((reg.name, entry.format, entry.kind),))
    return 0


def _confirm_trust(args, name: str, where: str) -> None:
    print(f"Adding the registry {name} ({_clean(where)}).")
    print(
        "Its specs are Python code that runs inside Fandango with your permissions, and may\n"
        "ask for Python packages to be installed. Add only registries you trust."
    )
    if args.trust:
        return
    if not sys.stdin.isatty():
        raise RegistryError("not adding it without your confirmation; pass --trust to give it")
    if input("Trust it? [y/N] ").strip().lower() not in ("y", "yes"):
        raise RegistryError("not added")


def cmd_registry_add(args, ctx: Context) -> int:
    location = args.url
    token = None
    if args.token_env is not None:
        check_token_env(args.token_env)
        token = RegistryConfig("probe", location, args.token_env).token()
        if token is None:
            print(f"note: ${args.token_env} is not set here", file=sys.stderr)
    if location.startswith(("http://", "https://")):
        opened = RemoteRegistry(location, token)
    else:
        path = Path(location).expanduser()
        opened = Registry(path)
        location = str(path.resolve())
    chosen = args.name if args.name is not None else opened.info.get("name")
    if chosen is None:
        raise RegistryError("the registry does not say what it is called; give it a name with --name")
    name = check_registry_name(chosen)
    existing = ctx.config.registries.get(name)
    if existing and existing.url == location:
        print(f"{name} is already added")
        return 0
    if existing:
        raise RegistryError(f"{name} is already added, as {existing.url}; remove it first")
    if (declared := opened.info.get("name")) and declared != name:
        print(
            f"note: the registry calls itself {declared}; its specs include each other as "
            f"{declared}/..., which only works under that name",
            file=sys.stderr,
        )
    _confirm_trust(args, name, location)
    ctx.config.registries[name] = RegistryConfig(name, location, args.token_env)
    path = save_config(ctx.config)
    print(f"added {name}: {len(opened.formats())} formats ({path})")
    return 0


def cmd_registry_remove(args, ctx: Context) -> int:
    if args.name not in ctx.config.registries:
        raise RegistryError(f"no registry called {args.name!r}")
    mine = [done for done in all_installed() if done.registry == args.name]
    if args.uninstall and mine:
        uninstall([str(done) for done in mine])
        print(f"removed {len(mine)} installed specs of {args.name}")
        mine = []
    del ctx.config.registries[args.name]
    dropped = [ref for ref, target in ctx.config.pins.items() if split_registry(target)[0] == args.name]
    for ref in dropped:
        del ctx.config.pins[ref]
    save_config(ctx.config)
    print(f"removed the registry {args.name}")
    if dropped:
        print(f"unpinned {', '.join(dropped)}")
    if mine:
        print(f"{len(mine)} installed specs of {args.name} are still there; "
              f"`fanbase uninstall {mine[0]}` removes one, or remove them all with --uninstall next time")
    return 0


def cmd_registry_list(args, ctx: Context) -> int:
    rows = [("fanbase", str(locate_registry(args.registry, local=False)), "default")]
    rows += [(reg.name, reg.url, "") for reg in sorted(ctx.config.registries.values(), key=lambda r: r.name)]
    width = max(len(name) for name, _, _ in rows)
    for name, where, note in rows:
        print(_clean(f"  {name:<{width}}  {where}" + (f"  ({note})" if note else "")))
    return 0


def cmd_pin(args, ctx: Context) -> int:
    if not args.ref and not args.target:
        if not ctx.config.pins:
            print("nothing pinned")
        for ref, target in sorted(ctx.config.pins.items()):
            print(_clean(f"  {ref} -> {target}"))
        return 0
    if not args.target:
        raise RegistryError("pin what to what? e.g. `fanbase pin png acme:png/png-strict`")
    if split_registry(args.ref)[0]:
        raise RegistryError(f"{args.ref} already names a registry; pin a plain name such as png")
    name, _ = split_registry(args.target)
    if not name:
        raise RegistryError(f"pin {args.ref} to a spec of a registry you added, as name:spec")
    if not args.no_check:
        ctx.registry(name).resolve(split_registry(args.target)[1])
    ctx.config.pins[args.ref.strip()] = args.target.strip()
    save_config(ctx.config)
    print(f"{args.ref} now means {args.target}")
    return 0


def cmd_unpin(args, ctx: Context) -> int:
    if args.ref not in ctx.config.pins:
        raise RegistryError(f"{args.ref} is not pinned")
    del ctx.config.pins[args.ref]
    save_config(ctx.config)
    print(f"{args.ref} is no longer pinned")
    return 0


def cmd_reindex(args, ctx: Context) -> int:
    reg = ctx.default
    assert isinstance(reg, Registry)
    rows, changed, undescribed = reindex(reg, write=not args.check)
    index_text = dump_index(rows, reg.info)
    index_file = reg.root / INDEX_FILENAME
    index_stale = not index_file.is_file() or index_file.read_text(encoding="utf-8") != index_text

    if args.check:
        # The index repeats the `fanbase` stamp, so compare it the way the metadata is compared.
        index_stale = index_stale and _index_view(index_file) != _index_view_of(index_text)
        stale = [str(e) for e in changed]
        if stale:
            print("metadata.yml out of date: " + ", ".join(stale))
        if index_stale:
            print(f"{INDEX_FILENAME} out of date")
        if undescribed:
            print("no description yet: " + ", ".join(str(e) for e in undescribed))
        if stale or index_stale or undescribed:
            print("run `fanbase reindex`")
            return 1
        print(f"{INDEX_FILENAME} and metadata.yml are up to date")
        return 0

    index_file.write_text(index_text, encoding="utf-8")
    print(f"wrote {INDEX_FILENAME}: {len(rows)} specs in {len(reg.formats())} formats")
    if changed:
        print(f"updated metadata.yml for {len(changed)} spec(s)")
    if undescribed:
        print("no description yet: " + ", ".join(str(e) for e in undescribed))
    return 0


def _index_view_of(text: str) -> tuple[int, dict, list[dict]]:
    """An index's schema, registry details and rows, without the `fanbase` stamp, for
    comparing two indexes."""
    data = yaml.safe_load(text) or {}
    rows = [{k: v for k, v in row.items() if k != "fanbase"} for row in data.get("specs", [])]
    return data.get("schema", 1), data.get("registry") or {}, rows


def _index_view(path) -> tuple[int, dict, list[dict]] | None:
    return _index_view_of(path.read_text(encoding="utf-8")) if path.is_file() else None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fanbase", description=__doc__)
    ap.add_argument("--version", action="version", version=f"fanbase {__version__}")
    ap.add_argument("--registry", help="path to a registry checkout, or a URL to fetch it from")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list formats, or the specs of one format")
    p.add_argument("format", nargs="?", help="e.g. png; acme: or acme:png for a registry you added")
    p.add_argument("--installed", action="store_true", help="list what is installed; asks no registry")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="show a spec's metadata")
    p.add_argument("ref", help="e.g. png, png/png-apng, or acme:png-strict")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("install", help="copy specs into Fandango's include path")
    p.add_argument("refs", nargs="*", metavar="ref", help="e.g. png, png/png-apng, or acme:png-strict")
    p.add_argument("--all", action="store_true", help="install every spec in the registry")
    p.add_argument("--from", dest="source", metavar="NAME", help="with --all: a registry you added, instead of the default")
    p.add_argument("--no-requirements", action="store_true", help="do not install the Python packages the specs need")
    p.add_argument("--into", help="install directory (default: Fandango's data dir)")
    p.add_argument("--locked", action="store_true", help=f"install exactly what {LOCK_FILENAME} says, or nothing")
    p.add_argument("--lockfile", metavar="FILE", help=f"the lock to use (default: $FANBASE_LOCK, else ./{LOCK_FILENAME})")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("lock", help=f"write {LOCK_FILENAME}: exactly which specs this project uses")
    p.add_argument("refs", nargs="*", metavar="ref", help="the specs to lock (default: those already in the lock)")
    p.add_argument("--lockfile", metavar="FILE", help=f"where to write it (default: ./{LOCK_FILENAME})")
    p.add_argument("--check", action="store_true", help="only say whether the lock is still what the registries have; exit 1 if not")
    p.set_defaults(fn=cmd_lock)

    p = sub.add_parser("update", help="bring installed specs up to date")
    p.add_argument("refs", nargs="*", metavar="ref", help="specs to update (default: all installed)")
    p.add_argument("--no-requirements", action="store_true", help="do not install the Python packages the specs need")
    p.set_defaults(all=False)
    p.set_defaults(fn=cmd_update)

    p = sub.add_parser("uninstall", help="remove installed specs")
    p.add_argument("refs", nargs="+", metavar="ref", help="e.g. png, png/png-apng, or acme:png-strict")
    p.add_argument("--force", action="store_true", help="remove a spec even if an installed spec extends it")
    p.set_defaults(fn=cmd_uninstall)

    p = sub.add_parser("deps", help="show what a spec extends, or with --reverse what extends it")
    p.add_argument("ref", help="e.g. png-apng")
    p.add_argument("--reverse", action="store_true", help="the specs that extend it, instead")
    p.set_defaults(fn=cmd_deps)

    p = sub.add_parser("registry", help="the registries you use besides the default one")
    reg_sub = p.add_subparsers(dest="registry_command", required=True)
    q = reg_sub.add_parser("add", help="add a registry: a URL, or the path of a local checkout")
    q.add_argument("url")
    q.add_argument("--name", help="what to call it (default: the name the registry gives itself)")
    q.add_argument("--token-env", metavar="VAR", help="the environment variable that holds a token, for a private registry")
    q.add_argument("--trust", action="store_true", help="do not ask whether you trust it")
    q.set_defaults(fn=cmd_registry_add)
    q = reg_sub.add_parser("remove", help="forget a registry")
    q.add_argument("name")
    q.add_argument("--uninstall", action="store_true", help="also remove the specs installed from it")
    q.set_defaults(fn=cmd_registry_remove)
    q = reg_sub.add_parser("list", help="the default registry and the ones you added")
    q.set_defaults(fn=cmd_registry_list)

    p = sub.add_parser("pin", help="make a plain name mean a spec of a registry you added; with no arguments, list the pins")
    p.add_argument("ref", nargs="?", help="a plain name, e.g. png")
    p.add_argument("target", nargs="?", help="e.g. acme:png/png-strict")
    p.add_argument("--no-check", action="store_true", help="do not look the target up first")
    p.set_defaults(fn=cmd_pin)

    p = sub.add_parser("unpin", help="undo a pin")
    p.add_argument("ref")
    p.set_defaults(fn=cmd_unpin)

    p = sub.add_parser("reindex", help=f"maintainers: refresh metadata.yml files and {INDEX_FILENAME}")
    p.add_argument("--check", action="store_true", help="only report what is out of date; exit 1 if anything is")
    p.set_defaults(fn=cmd_reindex, local=True)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # Registries are opened when a command asks for them, so what only looks at what
        # is installed works without a network.
        ctx = Context(explicit=args.registry, local=getattr(args, "local", False))
        return args.fn(args, ctx)
    except RegistryError as exc:
        print(f"fanbase: {_clean(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
