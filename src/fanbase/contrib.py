"""For people who write specs: start one, fork one, check it, see what changed.

These commands work on a registry checkout (a clone of the public registry, or of one of
your own), which is where `fanbase publish` takes the change to a pull request.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from packaging.version import InvalidVersion, Version

from fanbase.config import Config
from fanbase.context import Context
from fanbase.deps import check_version, closure, parse_extends, self_names
from fanbase.manager import entry_sha, install, spec_path, split_ref
from fanbase.manifest import INDEX_FILENAME, dump_index, index_is_stale, reindex
from fanbase.output import clean
from fanbase.registry import (
    DEFAULT_REGISTRY_NAME,
    Entry,
    Registry,
    RegistryBase,
    RegistryError,
    is_safe_name,
)
from fanbase.source import DEFAULT_REGISTRY, ENV_REGISTRY, Location, open_registry


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Every program these commands start goes through here, so tests can stand in for it."""
    return subprocess.run(cmd, **kwargs)


def checkout(ctx: Context) -> Registry:
    reg = ctx.default
    if not isinstance(reg, Registry):
        raise RegistryError("this command needs a local registry checkout")
    return reg


def git_user() -> str | None:
    try:
        done = _run(["git", "config", "user.name"], capture_output=True, text=True)
    except OSError:
        return None
    return done.stdout.strip() or None


def refresh(reg: Registry) -> tuple[list[dict], list[Entry], list[Entry]]:
    """Reindex the checkout and write its index.yml, as `fanbase reindex` does."""
    rows, changed, undescribed = reindex(reg)
    (reg.root / INDEX_FILENAME).write_text(dump_index(rows, reg.info), encoding="utf-8")
    return rows, changed, undescribed


def split_new_ref(ref: str) -> tuple[str, str]:
    """`png/png-fancy` or `png-fancy` -> (png, png-fancy). A format's default spec is named
    after the format; any other is the format, a dash, and what makes it different."""
    ref = ref.strip()
    if "/" in ref:
        fmt, _, kind = ref.partition("/")
    else:
        fmt, kind = ref.split("-", 1)[0], ref
    if not (is_safe_name(fmt) and is_safe_name(kind)):
        raise RegistryError(f"{ref!r} is not a usable spec name")
    suffix = kind[len(fmt) + 1:]
    if kind != fmt and not (kind.startswith(fmt + "-") and suffix[:1].isalnum()):
        raise RegistryError(
            f"a spec of the format {fmt} is called {fmt} (its default) or {fmt}-<what makes it different>, "
            f"such as {fmt}-apng; not {kind}"
        )
    return fmt, kind


# --- new

def _include_path(owner: str, fmt: str, kind: str) -> str:
    return f"{owner + '/' if owner else ''}{fmt}/{kind}.fan"


def _template(kind: str, description: str, includes: list[str]) -> str:
    lines = [f"# {kind}: {description or 'say in a line what this spec produces'}", "#",
             "# TODO: what it produces and leaves out, and what it was tested against."]
    if includes:
        lines += ["", "# Builds on what it includes; redefine only what differs."]
        lines += [f'include("{path}")' for path in includes]
        lines += ["", "# <rule> ::= ...", ""]
    else:
        lines += ["", '<start> ::= "TODO"', ""]
    return "\n".join(lines)


def cmd_new(args, ctx: Context) -> int:
    """Start a spec in the registry checkout, ready to edit."""
    reg = checkout(ctx)
    fmt, kind = split_new_ref(args.ref)
    folder = reg.specs_dir / fmt / kind
    if folder.exists():
        raise RegistryError(f"{fmt}/{kind} exists already: {folder}")

    owner = reg.info.get("name", "")
    includes: list[str] = []
    extensions = None
    for item in args.extends or []:
        dep = parse_extends(item, owner, {owner or DEFAULT_REGISTRY_NAME})
        if dep.registry == owner:
            try:
                target = reg.resolve(dep.ref)
            except RegistryError as exc:
                raise RegistryError(f"cannot extend {item}: {exc}") from None
            check_version(str(target), target.meta, dep.specifier, item)
            includes.append(_include_path(owner, target.format, target.kind))
            extensions = extensions or target.meta.get("extensions")
        else:  # the default registry's: this checkout cannot look it up
            _, dep_fmt, dep_kind = split_ref(dep.ref)
            includes.append(_include_path("", dep_fmt, dep_kind))

    meta: dict = {"description": args.description or "", "version": "0.1", "status": "draft"}
    if user := git_user():
        meta["authors"] = [user]
    if extensions:
        meta["extensions"] = list(extensions)
    if args.extends:
        meta["extends"] = list(args.extends)

    folder.mkdir(parents=True)
    try:
        (folder / f"{kind}.fan").write_text(_template(kind, args.description or "", includes), encoding="utf-8")
        (folder / "metadata.yml").write_text(yaml.safe_dump(meta, sort_keys=False, allow_unicode=True), encoding="utf-8")
        refresh(reg)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        if not any((folder.parent).iterdir()):
            folder.parent.rmdir()
        raise
    print(f"created {folder.relative_to(reg.root)}/ ({kind}.fan, metadata.yml)")
    print(f"next: edit {kind}.fan, then `fanbase check {kind}`")
    return 0


# --- fork

def _requalified(items: list, source: RegistryBase, target_owner: str) -> list:
    """The `extends` of a spec, as they must be written once the spec lives in another registry."""
    out = []
    for item in items:
        dep = parse_extends(item, source.name, self_names(source))
        if dep.registry == target_owner:
            out.append(item)
        elif dep.registry == "":
            out.append(item if str(item).startswith(f"{DEFAULT_REGISTRY_NAME}:") else f"{DEFAULT_REGISTRY_NAME}:{item}")
        else:
            raise RegistryError(
                f"{item} is a spec of the registry {dep.registry}, which a spec of {target_owner or 'this registry'} "
                "cannot extend; fork what it extends first"
            )
    return out


def cmd_fork(args, ctx: Context) -> int:
    """Copy a spec of any registry into the checkout under a new name, to change it there."""
    source, entry = ctx.resolve(args.ref)
    if args.into:
        target = Registry(Path(args.into))
    elif isinstance(ctx.default, Registry):
        target = ctx.default  # forking within the registry you are working in
    else:
        target = Registry(Path(_located_checkout()))
    fmt, kind = split_new_ref(args.name)
    if fmt != entry.format:
        raise RegistryError(f"a fork of {entry} is a spec of the format {entry.format}, not {fmt}")
    folder = target.specs_dir / fmt / kind
    if folder.exists():
        raise RegistryError(f"{fmt}/{kind} exists already: {folder}")

    owner = target.info.get("name", "")
    where = f"{source.name or DEFAULT_REGISTRY_NAME}:{entry}"
    version = entry.meta.get("version")
    keep = ("title", "extensions", "mime", "reference", "license", "source", "fandango", "pip")
    meta: dict = {"description": f"{entry.meta.get('description') or entry.kind} (fork of {where})".strip()}
    meta.update({key: entry.meta[key] for key in keep if key in entry.meta})
    authors = list(entry.meta.get("authors") or [])
    if (user := git_user()) and user not in [a.get("name") if isinstance(a, dict) else a for a in authors]:
        authors.append(user)
    if authors:
        meta["authors"] = authors
    if entry.meta.get("extends"):
        meta["extends"] = _requalified(list(entry.meta["extends"]), source, owner)
    meta.update(derived_from=f"{where}@{version}" if version is not None else where, version="0.1", status="draft")

    folder.mkdir(parents=True)
    try:
        text = source.read(entry).decode("utf-8")
        (folder / f"{kind}.fan").write_text(text, encoding="utf-8")
        (folder / "metadata.yml").write_text(yaml.safe_dump(meta, sort_keys=False, allow_unicode=True), encoding="utf-8")
        refresh(target)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        if not any(folder.parent.iterdir()):
            folder.parent.rmdir()
        raise
    print(f"forked {where} into {folder.relative_to(target.root)}/")
    print(f"it records `derived_from: {meta['derived_from']}`, and keeps the original authors")
    print(f"next: edit {kind}.fan, then `fanbase check {kind}`")
    return 0


def _located_checkout() -> str:
    from fanbase.source import locate_registry

    return locate_registry(None, local=True).value


# --- changes

@dataclass(frozen=True)
class Change:
    kind: str  # added, changed, removed
    spec: str
    old: str | None = None
    new: str | None = None
    description: str | None = None

    @property
    def unbumped(self) -> bool:
        """A changed spec whose version did not go up."""
        if self.kind != "changed":
            return False
        try:
            return not (Version(str(self.new)) > Version(str(self.old)))
        except InvalidVersion:
            return self.new == self.old


def compare_registries(old: RegistryBase, new: RegistryBase) -> list[Change]:
    def index(reg: RegistryBase) -> dict[str, Entry]:
        return {str(e): e for fmt in reg.formats() for e in reg.kinds(fmt)}

    before, after = index(old), index(new)
    changes = []
    for spec, entry in after.items():
        version = entry.meta.get("version")
        if spec not in before:
            changes.append(Change("added", spec, None, None if version is None else str(version), entry.meta.get("description")))
        elif entry_sha(old, before[spec]) != entry_sha(new, entry):
            was = before[spec].meta.get("version")
            changes.append(Change("changed", spec, None if was is None else str(was), None if version is None else str(version),
                                  entry.meta.get("description")))
    for spec, entry in before.items():
        if spec not in after:
            changes.append(Change("removed", spec, entry.meta.get("version") and str(entry.meta["version"])))
    return changes


def open_base(base: str) -> RegistryBase:
    """The registry to compare with: a URL, or the path of a checkout."""
    return open_registry(Location("url" if base.startswith(("http://", "https://")) else "path", base))


def cmd_changes(args, ctx: Context) -> int:
    """What differs from another registry (an earlier release, or the public registry): added,
    changed and removed specs. For release notes, and to check that a changed spec got a new version."""
    new, old = ctx.default, open_base(args.base)
    changes = compare_registries(old, new)
    bad = [c for c in changes if c.unbumped]

    if args.markdown:
        titles = {"added": "Added", "changed": "Changed", "removed": "Removed"}
        print(f"## Changes since {clean(args.base)}\n")
        if not changes:
            print("No changes.")
        for kind, title in titles.items():
            group = [c for c in changes if c.kind == kind]
            if group:
                print(f"### {title}\n")
                for c in group:
                    if c.kind == "changed":
                        detail = f" {c.old or '-'} → {c.new or '-'}"
                    elif c.kind == "added":
                        detail = f" {c.new}" if c.new else ""
                    else:
                        detail = ""
                    about = f" — {c.description}" if c.description and c.kind != "removed" else ""
                    print(clean(f"- `{c.spec}`{detail}{about}"))
                print()
    else:
        for c in changes:
            if c.kind == "changed":
                note = f"{c.old or '-'} -> {c.new or '-'}" + ("   (no new version: bump it)" if c.unbumped else "")
            else:
                note = (c.new if c.kind == "added" else c.old) or ""
            print(clean(f"  {c.kind:<8} {c.spec}  {note}").rstrip())
        if not changes:
            print("no changes")
    if args.check and bad:
        print(f"{len(bad)} spec(s) changed without a new version: " + ", ".join(c.spec for c in bad), file=sys.stderr)
        return 1
    return 0


# --- check

def find_fandango() -> str | None:
    """The `fandango` command: on the path, or next to this Python."""
    found = shutil.which("fandango")
    if found:
        return found
    beside = Path(sys.executable).parent / ("fandango.exe" if os.name == "nt" else "fandango")
    return str(beside) if beside.is_file() else None


def generate(reg: Registry, entry: Entry, ctx: Context, count: int, timeout: int, fandango: str) -> str | None:
    """Make Fandango produce inputs from a spec, with what it extends installed where its
    include() looks. None if it did; else why not."""
    tmp = Path(tempfile.mkdtemp(prefix="fanbase-check-"))
    try:
        library, out = tmp / "library", tmp / "out"
        for dep_reg, dep in closure(ctx, reg, entry):
            install(dep_reg, dep, library)
        env = {**os.environ, "FANDANGO_PATH": str(library)}
        cmd = [fandango, "fuzz", "-f", str(spec_path(library, entry.format, entry.kind, reg.name)),
               "-n", str(count), "-d", str(out)]
        try:
            done = _run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return f"fandango did not finish in {timeout} seconds"
        files = [p for p in out.glob("*") if p.is_file()] if out.is_dir() else []
        if done.returncode != 0:
            tail = " | ".join((done.stderr or done.stdout).strip().splitlines()[-2:])
            return f"fandango failed ({done.returncode}): {tail}"
        if not files:
            return "fandango produced no files"
        if not any(p.stat().st_size > 0 for p in files):
            return "fandango produced only empty files"
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@dataclass
class Report:
    failures: list[str]
    warnings: list[str]
    checked: int = 0
    generated: int = 0
    skipped_generation: str | None = None


def run_checks(reg: Registry, refs: list[str], *, count: int = 3, timeout: int = 300, generate_inputs: bool = True,
               base: str | None = None, strict: bool = False, say=print) -> Report:
    """Everything `fanbase check` looks at. `say` is told what is going on, one line at a time."""
    report = Report([], [])

    # 1. metadata and the index agree with the specs, and what the specs extend works
    rows, changed, undescribed = reindex(reg, write=False)
    for entry in changed:
        report.failures.append(f"{entry}: metadata.yml is out of date")
    if index_is_stale(reg, rows):
        report.failures.append(f"{INDEX_FILENAME} is out of date")

    selected = [e for fmt in reg.formats() for e in reg.kinds(fmt)]
    if refs:
        wanted = [reg.resolve(ref) for ref in refs]
        selected = [e for e in selected if e in wanted]

    # 2. what a reader needs to know about a spec
    for entry in selected:
        missing = [key for key in ("description", "authors", "license") if not entry.meta.get(key)]
        if missing:
            report.warnings.append(f"{entry}: no {', no '.join(missing)}")
        report.checked += 1

    # 3. a changed spec has a new version
    if base:
        for change in compare_registries(open_base(base), reg):
            if change.unbumped and (not refs or change.spec in {str(e) for e in selected}):
                report.failures.append(f"{change.spec}: changed since {base} but still at version {change.new}")

    # 4. every spec produces inputs
    fandango = find_fandango() if generate_inputs else None
    if generate_inputs and fandango is None:
        report.skipped_generation = "fandango is not installed, so no inputs were produced (pip install fandango-fuzzer)"
    if fandango:
        # Specs are installed the way users get them: under the registry's own name, if it has one.
        owner, was = reg.info.get("name", ""), reg.name
        if owner:  # a registry of someone's own: the default registry is the public one
            gen_ctx = Context(explicit=os.environ.get(ENV_REGISTRY) or DEFAULT_REGISTRY, config=Config())
            gen_ctx.add_registry(owner, reg)
        else:
            gen_ctx = Context(default=reg, config=Config())
        try:
            for entry in selected:
                why = generate(reg, entry, gen_ctx, count, timeout, fandango)
                if why:
                    report.failures.append(f"{entry}: {why}")
                else:
                    report.generated += 1
                say(f"  {'ok     ' if not why else 'FAILED '} {entry}")
        finally:
            reg.name = was
    if strict:
        report.failures.extend(f"{w} (--strict)" for w in report.warnings)
        report.warnings = []
    return report


def cmd_check(args, ctx: Context) -> int:
    """Is this registry (or these specs of it) in order, and does each spec produce inputs?"""
    reg = checkout(ctx)
    report = run_checks(
        reg, args.refs, count=args.count, timeout=args.timeout, generate_inputs=not args.no_generate,
        base=args.base, strict=args.strict,
    )
    for line in report.warnings:
        print(clean(f"warning: {line}"))
    for line in report.failures:
        print(clean(f"FAILED:  {line}"))
    if report.skipped_generation:
        print(f"note: {report.skipped_generation}")
    summary = f"{report.checked} specs looked at, {report.generated} produced inputs"
    if report.failures:
        print(f"{summary}; {len(report.failures)} problem(s)")
        return 1
    print(f"{summary}; nothing wrong")
    return 0
