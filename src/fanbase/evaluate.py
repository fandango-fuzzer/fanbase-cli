"""`fanbase evaluate`: how well do a spec's files do against the parsers of their format?

For each spec: have Fandango produce inputs (with a seed, so it is the same each time), ask each
of the format's targets about every one, and report

  - validity:    how many files each target accepts, and why it rejects the others;
  - throughput:  how fast Fandango produces inputs, and how fast the targets answer.

What counts as good depends on the spec: many exist to make files that do not decode. A spec can say
how often its files should be accepted (`decodes`), and the report says whether they are.

It works on a registry checkout, so it runs the same for a contributor as in CI. Nothing is kept
unless asked (--keep). A target that crashes on a file has found something that is not for a public
log: with --hide-crashes a crash is counted as a plain error and nothing about it is said.
"""

from __future__ import annotations

import importlib.metadata
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from fanbase import __version__
from fanbase.context import Context
from fanbase.contrib import checkout, compare_registries, find_fandango, open_base, produce
from fanbase.deps import dependents
from fanbase.manager import entry_sha
from fanbase.output import clean
from fanbase.registry import FORMAT_FILENAME, Entry, Registry, RegistryError
from fanbase.targets import (
    CATEGORIES,
    Target,
    TargetError,
    TargetResult,
    load_target,
    run_target,
    target_names,
    unavailable,
    work_dir,
)

REPORT_SCHEMA = 1

# The band of a share of accepted files: what `decodes` is compared with.
def band(share: float) -> str:
    if share >= 0.95:
        return "always"
    if share >= 0.5:
        return "mostly"
    return "rarely" if share > 0 else "never"


@dataclass
class TargetReport:
    name: str
    title: str
    status: str  # ok, unavailable, skipped
    note: str = ""
    version: str | None = None
    result: TargetResult | None = None


@dataclass
class SpecReport:
    entry: Entry
    version: str | None
    sha256: str
    requested: int
    produced: int = 0
    seconds: float = 0.0
    note: str = ""  # fewer inputs than asked for, or why there are none
    error: str | None = None
    targets: list[TargetReport] = field(default_factory=list)
    decodes: str | None = None
    unjudged: str = ""  # why no target judged it

    @property
    def per_second(self) -> float:
        return self.produced / self.seconds if self.seconds else 0.0

    @property
    def best(self) -> float | None:
        ran = [t.result for t in self.targets if t.status == "ok" and t.result and t.result.total]
        return max((r.accepted / r.total for r in ran), default=None)

    @property
    def met(self) -> bool | None:
        """Is the share of files that the best target accepts what the spec says to expect?"""
        if self.decodes is None or self.best is None:
            return None
        return band(self.best) == self.decodes


def _fandango_version() -> str | None:
    try:
        return importlib.metadata.version("fandango-fuzzer")
    except importlib.metadata.PackageNotFoundError:
        return None


# --- choosing the specs

def select(reg: Registry, ctx: Context, args) -> list[Entry]:
    everything = [e for fmt in reg.formats() for e in reg.kinds(fmt)]
    if args.changed:
        if not args.base:
            raise RegistryError("--changed needs --base, the registry to compare with")
        names = {c.spec for c in compare_registries(open_base(args.base), reg) if c.kind in ("added", "changed")}
        chosen = [e for e in everything if str(e) in names]
        # what builds on a spec that changed may have changed with it
        queue = list(chosen)
        while queue:
            for _, found in dependents(ctx, reg, queue.pop()):
                if found not in chosen:
                    chosen.append(found)
                    queue.append(found)
        return sorted(chosen, key=str)
    if args.all:
        return everything
    if not args.refs:
        raise RegistryError("name the specs to evaluate, or give --all, or --changed with --base")
    return [reg.resolve(ref) for ref in args.refs]


def _targets_of(reg: Registry, entry: Entry, only: list[str] | None) -> tuple[list[Target], list[TargetReport]]:
    """The targets to ask about a spec, and the ones left out with the reason."""
    named = only if only is not None else list(entry.meta.get("targets") or [])
    usable, left_out = [], []
    for name in named:
        target = load_target(reg.root, name)
        if entry.format in target.formats:
            usable.append(target)
        else:
            left_out.append(TargetReport(name, target.title, "skipped", f"it does not judge {entry.format}"))
    return usable, left_out


# --- one spec

def evaluate_spec(reg: Registry, entry: Entry, ctx: Context, args, fandango: str, scratch: Path, say) -> SpecReport:
    report = SpecReport(entry, None if entry.meta.get("version") is None else str(entry.meta["version"]),
                        entry_sha(reg, entry), args.count, decodes=entry.meta.get("decodes"))
    only = [t for t in args.targets.split(",") if t] if args.targets else None
    targets, report.targets = _targets_of(reg, entry, only)
    if not targets:
        report.unjudged = (f"no targets for {entry.format}: name some in specs/{entry.format}/{FORMAT_FILENAME}"
                           if not report.targets else "none of the targets judges this format")
        return report

    made = produce(reg, entry, ctx, count=args.count, timeout=args.generate_timeout, fandango=fandango,
                   install_them=not args.no_requirements, say=say, seed=args.seed)
    try:
        report.produced, report.seconds = len(made.files), made.seconds
        if made.error:
            report.error = made.error
            return report
        if len(made.files) < args.count:
            report.note = f"Fandango found {len(made.files)} of the {args.count} inputs asked for"
            if made.returncode:
                report.note += f" ({made.tail})"
        if args.keep:
            kept = Path(args.keep) / entry.format / entry.kind
            kept.mkdir(parents=True, exist_ok=True)
            for file in made.files:
                shutil.copy2(file, kept / file.name)
        for target in targets:
            if (why := unavailable(target)) is not None:
                report.targets.append(TargetReport(target.name, target.title, "unavailable", why))
                continue
            result = run_target(target, made.files, jobs=args.jobs, timeout=args.timeout, memory_mb=args.memory,
                                hide_crashes=args.hide_crashes, cwd=scratch)
            report.targets.append(TargetReport(target.name, target.title, "ok", version=result.version, result=result))
        return report
    finally:
        made.cleanup()


# --- saying it

def to_json(reports: list[SpecReport], args) -> dict:
    return {
        "schema": REPORT_SCHEMA,
        "fanbase": __version__,
        "fandango": _fandango_version(),
        "seed": args.seed,
        "count": args.count,
        "specs": [
            {
                "spec": str(r.entry),
                "version": r.version,
                "sha256": r.sha256,
                "generated": {"requested": r.requested, "produced": r.produced, "seconds": round(r.seconds, 3),
                              "per_second": round(r.per_second, 2), "note": r.note or None},
                "error": r.error,
                "unjudged": r.unjudged or None,
                "decodes": r.decodes,
                "best_accepted": None if r.best is None else round(r.best, 4),
                "expectation_met": r.met,
                "targets": [
                    {
                        "name": t.name, "title": t.title, "status": t.status, "note": t.note or None, "version": t.version,
                        **({
                            "total": t.result.total,
                            "accepted": t.result.accepted,
                            "categories": {c: t.result.counts[c] for c in CATEGORIES},
                            "files_per_second": round(t.result.per_second, 1),
                            "reasons": [[why, n] for why, n in t.result.top_reasons(5)],
                        } if t.result else {}),
                    }
                    for t in r.targets
                ],
            }
            for r in reports
        ],
    }


def _expectation(r: SpecReport) -> str:
    if r.met is None:
        return ""
    got = f"best target accepts {r.best:.0%}, which is {band(r.best)}"  # type: ignore[arg-type]
    return f"expected {r.decodes}: {got}" + ("; as expected" if r.met else "; NOT as expected")


_COLUMNS = ("accepted", "invalid", "unsupported", "resource-limit", "crash", "timeout", "error")


def _rows(r: SpecReport) -> list[list[str]]:
    rows = []
    for t in r.targets:
        if t.result is None:
            rows.append([t.name, f"({t.status}: {clean(t.note)})"] + [""] * (len(_COLUMNS) + 1))
            continue
        res = t.result
        rows.append([t.name, f"{res.accepted}/{res.total}"] + [str(res.counts[c]) for c in _COLUMNS[1:]]
                    + [f"{res.per_second:.0f}", clean(t.version or "")])
    return rows


def as_text(reports: list[SpecReport], args) -> str:
    out = []
    for r in reports:
        head = f"{r.entry}  version {r.version or '-'}"
        if r.error:
            out += [f"{head}: no inputs: {clean(r.error)}", ""]
            continue
        out.append(f"{head}: {r.produced} inputs from seed {args.seed} in {r.seconds:.1f}s ({r.per_second:.1f}/s)")
        if r.note:
            out.append(f"  note: {clean(r.note)}")
        if r.unjudged:
            out += [f"  {r.unjudged}", ""]
            continue
        header = ["target", "accepted", *_COLUMNS[1:], "files/s", "version"]
        rows = [header, *_rows(r)]
        widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
        for row in rows:
            out.append("  " + "  ".join(cell.ljust(w) if i in (0, len(row) - 1) else cell.rjust(w)
                                         for i, (cell, w) in enumerate(zip(row, widths))).rstrip())
        for t in r.targets:
            if t.result:
                for why, n in t.result.top_reasons(2):
                    out.append(clean(f"    {t.name}: {n}x {why}"))
        if expect := _expectation(r):
            out.append(f"  {expect}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def as_markdown(reports: list[SpecReport], args) -> str:
    out = [f"## Evaluation of {len(reports)} spec(s)", "",
           f"{args.count} inputs from each, seed {args.seed}; fanbase {__version__}, fandango {_fandango_version() or '?'}.", ""]
    for r in reports:
        out.append(f"### `{r.entry}` {r.version or ''}".rstrip())
        out.append("")
        if r.error:
            out += [f"**No inputs:** {clean(r.error)}", ""]
            continue
        out.append(f"{r.produced} inputs in {r.seconds:.1f}s ({r.per_second:.1f}/s)." + (f" {clean(r.note)}." if r.note else ""))
        out.append("")
        if r.unjudged:
            out += [f"_{r.unjudged}_", ""]
            continue
        out += ["| target | accepted | invalid | unsupported | limit | crash | timeout | error | files/s | version |",
                "|---|--:|--:|--:|--:|--:|--:|--:|--:|---|"]
        for row in _rows(r):
            out.append("| " + " | ".join(row) + " |")
        if expect := _expectation(r):
            out += ["", f"{'✅' if r.met else '⚠️'} {expect}"]
        out.append("")
    return "\n".join(out)


# --- the command

def cmd_evaluate(args, ctx: Context) -> int:
    """Evaluate specs against the targets of their format."""
    if args.hide_crashes and args.keep:
        raise RegistryError("--keep would save the inputs that crash targets; it does not go with --hide-crashes")
    reg = checkout(ctx)
    fandango = find_fandango()
    if fandango is None:
        raise RegistryError("evaluate needs fandango to produce the inputs (pip install fandango-fuzzer)")
    selected = select(reg, ctx, args)
    scratch = work_dir()
    try:
        reports = []
        for entry in selected:
            print(f"evaluating {entry} ...", file=sys.stderr)
            try:
                reports.append(evaluate_spec(reg, entry, ctx, args, fandango, scratch, say=lambda line: print(line, file=sys.stderr)))
            except TargetError as exc:
                raise RegistryError(f"{entry}: {exc}") from None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    document = to_json(reports, args)
    if args.json_file:
        Path(args.json_file).write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(document, indent=2, ensure_ascii=False))
    elif args.markdown:
        print(as_markdown(reports, args))
    else:
        print(as_text(reports, args), end="")

    failed = [r for r in reports if r.error]
    if failed:
        print(f"no inputs from {', '.join(str(r.entry) for r in failed)}", file=sys.stderr)
    problems = len(failed)
    if args.strict:
        problems += sum(1 for r in reports if r.met is False)
    if args.require_targets:
        problems += sum(1 for r in reports for t in r.targets if t.status == "unavailable")
    return 1 if problems else 0


def cmd_targets(args, ctx: Context) -> int:
    """The targets of the registry checkout, and whether they can run here."""
    reg = checkout(ctx)
    names = target_names(reg.root)
    if not names:
        print("this registry defines no targets")
        return 0
    from fanbase.targets import version_of

    width = max(len(n) for n in names)
    for name in names:
        try:
            target = load_target(reg.root, name)
        except TargetError as exc:
            print(clean(f"  {name:<{width}}  invalid: {exc}"))
            continue
        why = unavailable(target)
        state = f"cannot run here: {why}" if why else f"ready{' ' + v if (v := version_of(target)) else ''}"
        print(clean(f"  {name:<{width}}  {', '.join(target.formats)}  {state}"))
    return 0
