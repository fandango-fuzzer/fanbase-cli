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
from fanbase.contrib import changed_specs, checkout, find_fandango, produce
from fanbase.coverage import DEFAULT_CURVE, CoverageError, CoverageResult, coverage_targets, measure
from fanbase.incidents import IncidentLog, check_recipients, run_meta, write_private
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
    coverage: CoverageResult | None = None


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
        return changed_specs(reg, ctx, args.base)
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

def evaluate_spec(reg: Registry, entry: Entry, ctx: Context, args, fandango: str, scratch: Path, say,
                  incidents: IncidentLog | None = None) -> SpecReport:
    report = SpecReport(entry, None if entry.meta.get("version") is None else str(entry.meta["version"]),
                        entry_sha(reg, entry), args.count, decodes=entry.meta.get("decodes"))
    only = [t for t in args.targets.split(",") if t] if args.targets else None
    targets, report.targets = _targets_of(reg, entry, only)
    if getattr(args, "coverage_only", False):  # only the targets that can be measured
        targets, report.targets = coverage_targets(reg.root, entry.format, []), []
    elif getattr(args, "coverage", False):  # they join the ones the format names
        targets += coverage_targets(reg.root, entry.format, targets)
    if not targets:
        report.unjudged = (f"no targets for {entry.format}: name some in specs/{entry.format}/{FORMAT_FILENAME}"
                           if not report.targets else "none of the targets judges this format")
        return report

    made = produce(reg, entry, ctx, count=args.count, timeout=1800, fandango=fandango,
                   install_them=not args.no_requirements, say=say, seed=args.seed, budget=args.budget or None)
    try:
        report.produced, report.seconds = len(made.files), made.seconds
        if made.error:
            report.error = made.error
            return report
        if made.stopped:
            report.note = made.stopped
        elif len(made.files) < args.count:
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
            def note(tgt, file, verdict, confirmed):
                incidents.record(spec=str(entry), version=report.version, sha256=report.sha256, target=tgt, file=file,
                                 verdict=verdict, confirmed=confirmed)

            result = run_target(target, made.files, jobs=args.jobs, timeout=args.timeout, memory_mb=args.memory,
                                hide_crashes=args.hide_crashes, cwd=scratch, budget=args.judge_budget or None,
                                incidents=note if incidents is not None else None)
            row = TargetReport(target.name, target.title, "ok", version=result.version, result=result)
            report.targets.append(row)
            if getattr(args, "coverage", False) and target.coverage is not None:
                try:
                    row.coverage = measure(target, made.files, args.curve_points, timeout=args.timeout,
                                           memory_mb=args.memory, cwd=scratch)
                except CoverageError as exc:
                    row.note = f"coverage: {exc}"
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
                            "skipped": t.result.skipped,
                            "accepted": t.result.accepted,
                            "categories": {c: t.result.counts[c] for c in CATEGORIES},
                            "files_per_second": round(t.result.per_second, 1),
                            "reasons": [[why, n] for why, n in t.result.top_reasons(5)],
                        } if t.result else {}),
                        **({"coverage": t.coverage.to_dict()} if t.coverage else {}),
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


def _coverage_lines(r: SpecReport) -> list[tuple[TargetReport, list[str]]]:
    """What each measured target says about how much of it the files reach, as plain lines."""
    out = []
    for t in r.targets:
        c = t.coverage
        if c is None:
            continue
        def share(n: int | None, total: int | None) -> str:
            return "" if n is None else (f"{n}" + (f" ({n / total:.1%})" if total else ""))

        head = f"{t.name}{' ' + clean(t.version) if t.version else ''}: {c.lines_total} lines"
        if c.branches_total:
            head += f", {c.branches_total} branches"
        lines = [head]
        if c.seed_lines is not None:
            lines.append(f"{c.seeds} real file(s) reach {share(c.seed_lines, c.lines_total)} lines"
                         + (f" and {share(c.seed_branches, c.branches_total)} branches" if c.seed_branches is not None else ""))
        for n, covered, branches in c.curve:
            lines.append(f"{n} generated: {share(covered, c.lines_total)} lines"
                         + (f", {share(branches, c.branches_total)} branches" if branches is not None else ""))
        if c.only_generated is not None:
            lines.append(f"the generated files reach {c.only_generated} lines the real ones do not; the real ones reach {c.only_seeds} the generated ones do not")
        if c.note:
            lines.append(clean(c.note))
        out.append((t, lines))
    return out


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
                if t.result.skipped:
                    out.append(f"    {t.name}: out of time after {t.result.total} of {t.result.total + t.result.skipped} files")
                for why, n in t.result.top_reasons(2):
                    out.append(clean(f"    {t.name}: {n}x {why}"))
        for _, lines in _coverage_lines(r):
            out += ["  coverage of " + lines[0], *[f"    {line}" for line in lines[1:]]]
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
        for t in r.targets:
            if t.result and t.result.skipped:
                out += ["", f"_{t.name}: out of time after {t.result.total} of {t.result.total + t.result.skipped} files._"]
        for t, lines in _coverage_lines(r):
            c = t.coverage
            assert c is not None
            out += ["", f"**Coverage of {lines[0]}**", ""]
            out += ["| inputs | lines | branches |", "|---|--:|--:|"]
            if c.seed_lines is not None:
                out.append(f"| {c.seeds} real | {c.seed_lines} | {'' if c.seed_branches is None else c.seed_branches} |")
            out += [f"| {n} generated | {covered} ({covered / c.lines_total:.1%}) | {'' if branches is None else branches} |"
                    if c.lines_total else f"| {n} generated | {covered} | {'' if branches is None else branches} |"
                    for n, covered, branches in c.curve]
            if c.only_generated is not None:
                out += ["", f"The generated files reach {c.only_generated} lines the real ones do not; the real ones reach {c.only_seeds} the generated ones do not."]
            if c.note:
                out += ["", f"_{clean(c.note)}_"]
        if expect := _expectation(r):
            out += ["", f"{'✅' if r.met else '⚠️'} {expect}"]
        out.append("")
    return "\n".join(out)


# --- the command

def _curve(text: str) -> tuple[int, ...]:
    try:
        points = tuple(int(part) for part in text.split(",") if part.strip())
    except ValueError:
        points = ()
    if not points or any(n < 1 for n in points):
        raise RegistryError(f"--curve is a list of numbers of inputs, like 1,10,100,1000; not {clean(text)!r}")
    return points


def cmd_evaluate(args, ctx: Context) -> int:
    """Evaluate specs against the targets of their format."""
    recipients = Path(args.incident_recipients) if args.incident_recipients else None
    if recipients and not args.incidents:
        raise RegistryError("--incident-recipients goes with --incidents DIR, where the private record is written")
    if recipients:
        args.hide_crashes = True  # a record that is private is not also printed
    if args.hide_crashes and args.keep:
        raise RegistryError("--keep would save the inputs that crash targets; it does not go with --hide-crashes")
    if recipients:
        check_recipients(recipients)  # before anything runs: no way to keep it private, no run
    if getattr(args, "coverage_only", False):
        args.coverage = True
    args.curve_points = _curve(args.curve) if getattr(args, "curve", None) is not None else DEFAULT_CURVE
    log = IncidentLog() if args.incidents else None
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
                reports.append(evaluate_spec(reg, entry, ctx, args, fandango, scratch,
                                             say=lambda line: print(line, file=sys.stderr), incidents=log))
            except TargetError as exc:
                raise RegistryError(f"{entry}: {exc}") from None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if log is not None:
        saved = write_private(log, run_meta(args.seed, args.count), Path(args.incidents), recipients)
        if recipients:  # the same words whatever happened
            print(f"private record: {saved}", file=sys.stderr)
        elif saved:
            print(f"{len(log.incidents)} incident(s) saved in {saved}", file=sys.stderr)

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
