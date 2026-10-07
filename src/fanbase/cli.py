"""The `fanbase` command.

Browses and installs from a Fanbase registry. By default that is the public registry, read
over the network from its `index.yml`; a spec is only fetched when you ask for it. A local
checkout works too, which is what maintainers use.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

import yaml

from fanbase import __version__
from fanbase.manager import (
    Installed,
    all_installed,
    declared_requirements,
    ensure_requirements,
    install,
    install_root,
    installed_specs,
    spec_path,
    uninstall,
)
from fanbase.manifest import INDEX_FILENAME, dump_index, reindex
from fanbase.registry import Registry, RegistryBase, RegistryError
from fanbase.source import find_registry


_VERB = {"installed": "installed", "updated": "updated", "current": "up to date", "offline": "kept"}


def _report(done: Installed, hint_requirements: bool) -> None:
    print(f"{_VERB[done.status]} {done} -> {done.path}")
    if done.status != "current":
        print(f'  include("{done.format}/{done.kind}.fan")')
    if hint_requirements and (needs := declared_requirements(done.meta)):
        print(f"  requires: pip install {' '.join(needs)}")


def _warn_fandango(results: list[Installed]) -> None:
    for done in results:
        if problem := done.fandango_mismatch:
            print(f"warning: {done} {problem}", file=sys.stderr)


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


def _list_installed(fmt: str | None) -> int:
    have = [done for done in all_installed() if fmt in (None, done.format)]
    if not have:
        print("nothing installed")
        return 0
    width = max(len(str(done)) for done in have)
    for done in have:
        print(f"  {done!s:<{width}}  {done.meta.get('description') or ''}".rstrip())
    return 0


def cmd_list(args, reg: RegistryBase | None) -> int:
    if args.installed:
        return _list_installed(args.format)
    root = install_root()
    if args.format:
        if args.format not in reg.formats():
            raise RegistryError(f"unknown format: {args.format}")
        entries = reg.kinds(args.format)
        width = max(len(e.kind) for e in entries)
        for e in entries:
            have = "*" if spec_path(root, e.format, e.kind).is_file() else " "
            notes = [e.meta.get("description"), e.requires and f"(requires: {', '.join(e.requires)})"]
            print(f" {have}{e.kind:<{width}}  {' '.join(n for n in notes if n)}".rstrip())
        print("\n* installed")
        return 0

    formats = reg.formats()
    width = max((len(f) for f in formats), default=0)
    for fmt in formats:
        n = len(reg.kinds(fmt))
        print(f"  {fmt:<{width}}  {n} spec{'' if n == 1 else 's'}")
    return 0


def cmd_show(args, reg: RegistryBase) -> int:
    entry = reg.resolve(args.ref)
    meta = {"format": entry.format, "kind": entry.kind, "path": entry.path, **entry.meta}
    print(yaml.safe_dump(meta, sort_keys=False, allow_unicode=True), end="")
    return 0


def cmd_install(args, reg: RegistryBase) -> int:
    if args.all == bool(args.refs):
        raise RegistryError("name the specs to install, or give --all (not both)")
    root = None
    if args.into:
        from pathlib import Path

        root = Path(args.into)

    if args.all:
        entries = [e for fmt in reg.formats() for e in reg.kinds(fmt)]
        results = [install(reg, e, root) for e in entries]
        for done in results:
            print(f"{_VERB[done.status]} {done}")
        counts = Counter(_VERB[done.status] for done in results)
        summary = ", ".join(f"{n} {verb}" for verb, n in counts.items())
        where = f" in {results[0].path.parent.parent}" if results else ""
        print(f"{len(results)} specs: {summary}{where}")
    else:
        results = []
        for ref in args.refs:
            done = install(reg, reg.resolve(ref), root)
            _report(done, hint_requirements=args.no_requirements)
            results.append(done)
    _warn_fandango(results)
    _install_requirements(results, args)
    return 0


def cmd_update(args, reg: RegistryBase) -> int:
    if args.refs:
        entries = [reg.resolve(ref) for ref in args.refs]
    else:
        entries = installed_specs(reg)
        if not entries:
            print("nothing installed")
            return 0
    results = []
    for entry in entries:
        done = install(reg, entry)
        _report(done, hint_requirements=args.no_requirements)
        results.append(done)
    _warn_fandango(results)
    _install_requirements(results, args)
    return 0


def cmd_uninstall(args, reg: None) -> int:
    for done in uninstall(args.refs):
        print(f"removed {done} ({done.path})")
    return 0


def cmd_reindex(args, reg: RegistryBase) -> int:
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
    p.add_argument("format", nargs="?", help="e.g. png")
    p.add_argument("--installed", action="store_true", help="list what is installed; asks no registry")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="show a spec's metadata")
    p.add_argument("ref", help="e.g. png, or png/png-apng")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("install", help="copy specs into Fandango's include path")
    p.add_argument("refs", nargs="*", metavar="ref", help="e.g. png, or png/png-apng")
    p.add_argument("--all", action="store_true", help="install every spec in the registry")
    p.add_argument("--no-requirements", action="store_true", help="do not install the Python packages the specs need")
    p.add_argument("--into", help="install directory (default: Fandango's data dir)")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("update", help="bring installed specs up to date")
    p.add_argument("refs", nargs="*", metavar="ref", help="specs to update (default: all installed)")
    p.add_argument("--no-requirements", action="store_true", help="do not install the Python packages the specs need")
    p.set_defaults(all=False)
    p.set_defaults(fn=cmd_update)

    p = sub.add_parser("uninstall", help="remove installed specs")
    p.add_argument("refs", nargs="+", metavar="ref", help="e.g. png, or png/png-apng")
    p.set_defaults(fn=cmd_uninstall, offline=True)

    p = sub.add_parser("reindex", help=f"maintainers: refresh metadata.yml files and {INDEX_FILENAME}")
    p.add_argument("--check", action="store_true", help="only report what is out of date; exit 1 if anything is")
    p.set_defaults(fn=cmd_reindex, local=True)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # What only looks at the installed specs must work without a network.
        offline = getattr(args, "offline", False) or getattr(args, "installed", False)
        reg = None if offline else find_registry(args.registry, local=getattr(args, "local", False))
        return args.fn(args, reg)
    except RegistryError as exc:
        print(f"fanbase: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
