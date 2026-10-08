"""Looking at specs: what is out of date, what differs, what exists, how to cite one."""

from __future__ import annotations

import datetime
import difflib
import re
import sys

from fanbase.context import Context
from fanbase.locking import where
from fanbase.manager import all_installed, entry_sha, installed_copy
from fanbase.output import clean, dump_json
from fanbase.registry import RegistryError


# --- outdated

def cmd_outdated(args, ctx: Context) -> int:
    """The installed specs that the registries have a different version of."""
    rows: list[dict[str, str | None]] = []
    for have in all_installed():
        reg = ctx.registry(have.registry or None)
        try:
            entry = reg.resolve(f"{have.format}/{have.kind}")
        except RegistryError:
            rows.append({"spec": str(have), "installed": have.meta.get("version"), "registry": None, "status": "gone"})
            continue
        if entry_sha(reg, entry) == have.meta.get("sha256"):
            continue
        before, now = have.meta.get("version"), entry.meta.get("version")
        rows.append({
            "spec": str(have), "installed": None if before is None else str(before),
            "registry": None if now is None else str(now),
            "status": "changed" if before == now else "newer",
        })

    if args.json:
        print(dump_json(rows))
    elif not rows:
        print("everything installed is up to date")
    else:
        width = max(len(str(row["spec"])) for row in rows)
        for row in rows:
            if row["status"] == "gone":
                note = f"{row['installed'] or '-'}  (no longer in its registry)"
            elif row["status"] == "changed":
                note = f"{row['installed'] or '-'}  (changed, with no new version)"
            else:
                note = f"{row['installed'] or '-'} -> {row['registry'] or '-'}"
            print(clean(f"  {row['spec']:<{width}}  {note}"))
        print("\n`fanbase update` brings them up to date")
    return 1 if rows and args.check else 0


# --- diff

def _read(reg, entry) -> str:
    return reg.read(entry).decode("utf-8", errors="replace")


def cmd_diff(args, ctx: Context) -> int:
    """One spec: the installed copy against the registry's. Two: the registry's two copies."""
    if len(args.refs) > 2:
        raise RegistryError("diff takes one spec, or two to compare")
    if len(args.refs) == 1:
        reg, entry = ctx.resolve(args.refs[0])
        spec = f"{reg.name}:{entry}" if reg.name else str(entry)
        have = installed_copy(spec)
        if have is None:
            raise RegistryError(f"{spec} is not installed, so there is nothing to compare; `fanbase diff A B` compares two specs of a registry")
        old, new = have.path.read_bytes().decode("utf-8", errors="replace"), _read(reg, entry)
        names: tuple[str, str] = (f"installed {spec}", f"registry {spec}")
    else:
        pairs = [ctx.resolve(ref) for ref in args.refs]
        old, new = (_read(reg, entry) for reg, entry in pairs)
        names = (
            f"{pairs[0][0].name + ':' if pairs[0][0].name else ''}{pairs[0][1]}",
            f"{pairs[1][0].name + ':' if pairs[1][0].name else ''}{pairs[1][1]}",
        )
    text = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=names[0], tofile=names[1],
    ))
    if text:
        print(clean(text), end="" if text.endswith("\n") else "\n")
    return 1 if text else 0


# --- search

def _haystack(entry) -> str:
    meta = entry.meta
    parts = [entry.format, entry.kind, meta.get("title"), meta.get("description"), meta.get("mime"), *(meta.get("extensions") or [])]
    return " ".join(str(part).lower() for part in parts if part)


def cmd_search(args, ctx: Context) -> int:
    """The specs that match every word, in the default registry or, with --all, every one you added."""
    registries = [ctx.registry(None)]
    if args.all:
        for name in sorted(ctx.config.registries):
            try:
                registries.append(ctx.registry(name))
            except RegistryError as exc:
                print(f"warning: {clean(exc)}", file=sys.stderr)
    words = [word.lower() for word in args.words]
    extension = args.extension.lower().lstrip(".") if args.extension else None
    hits = []
    for reg in registries:
        for fmt in reg.formats():
            for entry in reg.kinds(fmt):
                if not all(word in _haystack(entry) for word in words):
                    continue
                extensions = [str(x).lower() for x in entry.meta.get("extensions") or []]
                if extension and extension not in extensions:
                    continue
                hits.append((reg, entry))

    if args.json:
        print(dump_json([
            {
                "spec": f"{reg.name + ':' if reg.name else ''}{entry}",
                "registry": reg.name or "fanbase",
                "format": entry.format, "kind": entry.kind,
                "version": entry.meta.get("version"),
                "description": entry.meta.get("description"),
                "extensions": entry.meta.get("extensions") or [],
            }
            for reg, entry in hits
        ]))
        return 0
    if not hits:
        print("nothing found")
        return 0
    names = [f"{reg.name + ':' if reg.name else ''}{entry}" for reg, entry in hits]
    width = max(len(name) for name in names)
    for name, (_, entry) in zip(names, hits):
        print(clean(f"  {name:<{width}}  {entry.meta.get('description') or ''}").rstrip())
    return 0


# --- cite

_BIBTEX_UNSAFE = re.compile(r"[{}\\]")
_BIBTEX_SPECIAL = re.compile(r"([&%$#_])")


def _bib(text: object) -> str:
    return _BIBTEX_SPECIAL.sub(r"\\\1", _BIBTEX_UNSAFE.sub("", str(text)))


# "The Fandango Fuzzer Team" is not a person with a family name: a name like that is written as it is.
_GROUP = re.compile(r"^the\b|\b(team|group|project|lab|labs|consortium|community|contributors|university|institute|"
                    r"centre|center|foundation|gmbh|inc|ltd|llc)\b", re.IGNORECASE)


def is_group(name: str) -> bool:
    """Does this name a team, a project or an institution rather than a person?"""
    return _GROUP.search(name) is not None


def bib_author(name: str) -> str:
    """An author for BibTeX: a group in braces, so that it is not read as a first and a last name."""
    return "{" + _bib(name) + "}" if is_group(name) else _bib(name)


def _author_names(meta: dict) -> list[str]:
    names = []
    for author in meta.get("authors") or []:
        name = author.get("name") if isinstance(author, dict) else author
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def citation(ctx: Context, reg, entry, today: datetime.date | None = None) -> dict:
    meta = entry.meta
    sha = entry_sha(reg, entry)
    doi = meta.get("doi")
    return {
        "authors": _author_names(meta) or ["Fanbase contributors"],
        "title": str(meta.get("title") or entry.kind),
        "spec": f"{reg.name + ':' if reg.name else ''}{entry}",
        "version": None if meta.get("version") is None else str(meta["version"]),
        "doi": doi if isinstance(doi, str) else None,
        "url": f"https://doi.org/{doi}" if isinstance(doi, str) else where(ctx, reg),
        "path": entry.path,
        "sha256": sha,
        "accessed": (today or datetime.date.today()).isoformat(),
    }


def cmd_cite(args, ctx: Context) -> int:
    """How to cite a spec: with its DOI when it has one, else where it is and the hash of the file."""
    reg, entry = ctx.resolve(args.ref)
    c = citation(ctx, reg, entry)
    if args.json:
        print(dump_json(c))
    elif args.bibtex:
        key = re.sub(r"[^A-Za-z0-9]+", "_", f"fanbase_{c['spec']}_{c['version'] or ''}").strip("_")
        fields = [
            ("author", " and ".join(bib_author(a) for a in c["authors"])),
            ("title", f"{_bib(c['title'])} ({_bib(c['spec'])}), a Fandango input specification"),
            ("version", _bib(c["version"]) if c["version"] else None),
            ("doi", c["doi"]),
            ("url", c["url"]),
            ("note", f"Fanbase registry, {_bib(c['path'])}, sha256 {c['sha256'][:12]}"),
            ("urldate", c["accessed"]),
        ]
        print(f"@software{{{key},")
        for name, value in fields:
            if value:
                print(f"  {name:<8}= {{{value}}},")
        print("}")
    else:
        version = f", version {c['version']}" if c["version"] else ""
        print(clean(
            f"{', '.join(c['authors'])}. {c['title']} ({c['spec']}){version}. Fanbase registry of Fandango input "
            f"specifications. {c['url']} ({c['path']}, sha256 {c['sha256'][:12]}), accessed {c['accessed']}."
        ))
    return 0
