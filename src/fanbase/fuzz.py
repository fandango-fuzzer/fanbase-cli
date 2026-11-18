"""`fanbase fuzz`: fuzz a project's own program with inputs from Fanbase specs, and keep what is found private.

This is what a project runs in its CI on itself (the Fandango GitHub Action calls it), and what its developers
run on their machines to see the same:

  - Fandango makes inputs from the specs, with a seed;
  - every target is asked about every input. A target says how to run a program on one file: usually the
    project's own, from a folder named with --target-dir (DIR/NAME/target.yml, as in the registry), and all of
    them that read the format unless --targets names some. The registry's targets can be named too, to
    compare the program with other implementations of the format;
  - a target that crashes, hangs or is killed has found something that may be a vulnerability. It goes into
    the private record, as with `fanbase evaluate --incidents`;
  - an input that the targets do not agree on (one accepts it, another does not), or that every target
    rejects although the spec says its files always decode, is a *finding*. Findings are grouped by what each
    target said, and the smallest input of each group is reduced (delta debugging) to a smaller one that still
    gets the same answers.

What is printed, which is what a public CI log shows, is counts. Everything else, findings included, goes into
the private record: encrypted to the public keys in --recipients (then it is safe as a public CI artifact), or,
without, in the clear in a folder that only you can read.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from fanbase import __version__
from fanbase.context import Context
from fanbase.contrib import checkout, find_fandango, produce
from fanbase.incidents import Extra, IncidentLog, check_recipients, run_meta, write_private
from fanbase.manager import entry_sha
from fanbase.output import clean
from fanbase.registry import Entry, Registry, RegistryError
from fanbase.targets import (
    ACCEPTED,
    ERROR,
    TARGET_FILENAME,
    Target,
    TargetError,
    Verdict,
    ask,
    load_target,
    portable,
    unavailable,
    work_dir,
)

REPORT_SCHEMA = 1
FUZZ_BUNDLE = "fuzz-private.age"
DISAGREEMENT, REJECTED = "disagreement", "rejected"
MAX_FINDINGS = 50  # kinds of finding written down in one run; more are counted
MAX_FINDING_BYTES = 3 * 1024 * 1024  # inputs kept for the findings, in all (the record has to stay mailable)
REDUCE_LIMIT = 400  # an input this small, after lines are reduced, is reduced character by character too
_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,10}")


# --- what each target said about one input

def bucket(verdict: Verdict) -> str:
    """How an answer is counted in public: accepted, rejected, or error. A target that broke is an error, as
    a harness that failed is: nothing printed tells the two apart."""
    if verdict.category == ACCEPTED:
        return "accepted"
    if verdict.incident or verdict.category == ERROR:
        return "error"
    return "rejected"


def classify(verdicts: list[Verdict], decodes: str | None) -> str | None:
    """Is this input a finding, and of which kind? An input that broke a target is not: it is in the record of
    incidents already, which is where a crash belongs."""
    if any(v.incident for v in verdicts):
        return None
    accepted = [v.category == ACCEPTED for v in verdicts]
    if any(accepted) and not all(accepted):
        return DISAGREEMENT
    if not any(accepted) and decodes == "always":
        return REJECTED
    return None


_POSITION = re.compile(r"(?i)\b(line|col(umn)?|position|offset)\s*\d+(\s*[:,]\s*(col(umn)?\s*)?\d+)?\s*[:,-]?\s*")


def gist(reason: str) -> str:
    """The kind of a rejection, without where in the file it was or what was there: its first words after the
    position, if it says one (what comes before it tends to be the same for every rejection), up to a number or
    a quote. Two inputs that a parser rejects for the same reason get the same gist."""
    positions = list(_POSITION.finditer(reason))
    after = reason[positions[-1].end():] if positions else ""
    words: list[str] = []
    for word in (after if after.strip() else _POSITION.sub("", reason)).split():
        if any(ch.isdigit() for ch in word) or word.startswith(("'", '"', "`")):
            break
        words.append(word)
        if len(words) == 6:
            break
    return " ".join(words) or reason[:40]


def answer(target: Target, verdict: Verdict) -> str:
    """What a target said, as it counts for telling findings apart."""
    return f"{target.name}={verdict.category}" + ("" if verdict.category == ACCEPTED else f" ({gist(verdict.reason)})")


def signature(kind: str, targets: list[Target], verdicts: list[Verdict]) -> str:
    return f"{kind}: " + "; ".join(answer(t, v) for t, v in zip(targets, verdicts))


def reduce(data: bytes, keeps, budget: float) -> bytes:
    """Delta debugging (ddmin): leave out lines, then, for a small input, characters, while `keeps(smaller)`
    holds, until nothing can be left out or the `budget` (seconds) is spent."""
    deadline = time.monotonic() + budget

    def ddmin(parts: list[bytes]) -> list[bytes]:
        n = 2
        while len(parts) >= 2 and time.monotonic() < deadline:
            chunk = max(1, len(parts) // n)
            for start in range(0, len(parts), chunk):
                rest = parts[:start] + parts[start + chunk:]
                if rest and keeps(b"".join(rest)):
                    parts, n = rest, max(n - 1, 2)
                    break
            else:
                if chunk == 1:
                    break
                n = min(n * 2, len(parts))
        return parts

    data = b"".join(ddmin(data.splitlines(keepends=True)))
    if len(data) <= REDUCE_LIMIT:
        data = b"".join(ddmin([data[i:i + 1] for i in range(len(data))]))
    return data


# --- one spec

@dataclass
class Finding:
    kind: str
    spec: str
    signature: str
    inputs: int  # how many inputs of the run got these answers
    suffix: str
    example: bytes  # the smallest of them
    reduced: bytes
    answers: list[tuple[str, str, str]]  # (target, category, reason) about the reduced input

    @property
    def id(self) -> str:
        return f"{self.spec.replace('/', '_')}__{self.kind}-{hashlib.sha256(self.signature.encode()).hexdigest()[:12]}"


@dataclass
class SpecRun:
    entry: Entry
    version: str | None
    sha256: str
    requested: int
    produced: int = 0
    seconds: float = 0.0
    note: str = ""
    error: str | None = None
    tally: dict[str, Counter] = field(default_factory=dict)  # target -> accepted/rejected/error
    groups: Counter = field(default_factory=Counter)  # kind -> how many kinds of finding


def _own(folder: Path) -> list[str]:
    """The names of the targets in a folder of a project's own (FOLDER/NAME/target.yml)."""
    return sorted(d.name for d in folder.iterdir() if (d / TARGET_FILENAME).is_file()) if folder.is_dir() else []


def _check_own(target: Target, folders: list[Path]) -> None:
    """With --only-own-targets, what is fuzzed is the project's own program: the target is in one of its
    folders (not a link out of them), and runs a harness that is there too, rather than some program of the
    machine. (A harness can still run anything; this keeps the command to what it is for.)"""
    if not any(target.directory.is_relative_to(f.resolve()) for f in folders):
        raise RegistryError(f"the target {target.name} is not in {', '.join(map(str, folders))} (a link out of it?)")
    if not any(part.startswith("{dir}/") for part in target.run):
        raise RegistryError(f"the target {target.name} runs {target.run[0]}, not a harness of its own folder: "
                            "with --only-own-targets, its run names a file in it, as {dir}/harness.py")


def _targets(reg: Registry, entry: Entry, args) -> list[Target]:
    """What to fuzz: the targets named with --targets; else every target of the project's own folders that reads
    the spec's format (the usual case: a project fuzzing its own program); else those the format names."""
    folders = [Path(d) for d in args.target_dir]
    own_only = getattr(args, "only_own_targets", False)
    if own_only and not folders:
        raise RegistryError("--only-own-targets fuzzes the targets of --target-dir: name that folder")
    if args.targets:
        names = [t for t in args.targets.split(",") if t]
    elif folders:
        names = list(dict.fromkeys(n for f in folders for n in _own(f)))
        names = [n for n in names if entry.format in load_target(reg.root, n, folders, registry=not own_only).formats]
        if not names:
            raise RegistryError(f"{entry}: none of the targets in {', '.join(map(str, folders))} reads {entry.format}")
    else:
        names = list(entry.meta.get("targets") or [])
    if not names:
        raise RegistryError(f"{entry}: name what to fuzz with --targets, or give --target-dir (the format names no targets)")
    found = []
    for name in names:
        target = load_target(reg.root, name, folders, registry=not own_only)
        if own_only:
            _check_own(target, folders)
        if entry.format not in target.formats:
            raise RegistryError(f"{entry}: the target {name} does not judge {entry.format} (its formats: {', '.join(target.formats)})")
        if (why := unavailable(target)) is not None:
            raise RegistryError(f"the target {name} cannot run here: {why}")
        found.append(target)
    return found


def fuzz_spec(reg: Registry, entry: Entry, ctx: Context, args, fandango: str, scratch: Path, log: IncidentLog,
              findings: list[Finding], say, tell) -> SpecRun:
    """`say` gets what Fandango says while making the inputs; `tell`, what was found, which is for a private
    place only (it is not called in CI)."""
    run = SpecRun(entry, None if entry.meta.get("version") is None else str(entry.meta["version"]),
                  entry_sha(reg, entry), args.count)
    targets = _targets(reg, entry, args)
    run.tally = {t.name: Counter() for t in targets}
    decodes = entry.meta.get("decodes")

    def note(target: Target, file: Path, verdict: Verdict, confirmed: bool) -> None:
        log.record(spec=str(entry), version=run.version, sha256=run.sha256, target=target, file=file,
                   verdict=verdict, confirmed=confirmed)

    spent: dict[str, float] = {}  # seconds each target took, in all: the fastest are asked first when reducing

    def answers(file: Path) -> list[Verdict]:
        said = []
        for t in targets:
            started = time.monotonic()
            said.append(ask(t, file, timeout=args.timeout, memory_mb=args.memory, cwd=scratch, incidents=note))
            spent[t.name] = spent.get(t.name, 0.0) + time.monotonic() - started
        return said

    made = produce(reg, entry, ctx, count=args.count, timeout=1800, fandango=fandango,
                   install_them=not args.no_requirements, say=say, seed=args.seed, budget=args.budget or None)
    try:
        run.produced, run.seconds = len(made.files), made.seconds
        if made.error:
            run.error = made.error
            return run
        if made.stopped:
            run.note = made.stopped
        elif len(made.files) < args.count:
            run.note = f"Fandango found {len(made.files)} of the {args.count} inputs asked for"
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            said = list(pool.map(answers, made.files))

        groups: dict[str, tuple[str, list[Path], list[str]]] = {}
        for file, verdicts in zip(made.files, said):
            for target, verdict in zip(targets, verdicts):
                run.tally[target.name][bucket(verdict)] += 1
            if (kind := classify(verdicts, decodes)) is not None:
                expected = [answer(t, v) for t, v in zip(targets, verdicts)]
                groups.setdefault(signature(kind, targets, verdicts), (kind, [], expected))[1].append(file)

        fastest_first = sorted(range(len(targets)), key=lambda i: spent.get(targets[i].name, 0.0))
        for sig, (kind, files, expected) in sorted(groups.items(), key=lambda item: (-len(item[1][1]), item[0])):
            run.groups[kind] += 1
            if len(findings) >= MAX_FINDINGS:
                continue
            example = min(files, key=lambda p: (p.stat().st_size, p.name))
            suffix = example.suffix if _SUFFIX.fullmatch(example.suffix) else ".bin"
            probe = scratch / f"probe{suffix}"

            def keeps(candidate: bytes, expected=expected, probe=probe) -> bool:
                """Does every target answer as before? (The same answers are the same kind of finding.) The
                fastest are asked first, and the first one that answers differently ends it."""
                probe.write_bytes(candidate)
                for i in fastest_first:
                    verdict = ask(targets[i], probe, timeout=args.timeout, memory_mb=args.memory, cwd=scratch, incidents=note)
                    if verdict.incident or answer(targets[i], verdict) != expected[i]:
                        return False
                return True

            data = example.read_bytes()
            reduced = reduce(data, keeps, args.reduce_budget)
            probe.write_bytes(reduced)
            final = answers(probe)
            findings.append(Finding(kind, str(entry), sig, len(files), suffix, data, reduced,
                                    [(t.name, v.category, v.reason) for t, v in zip(targets, final)]))
            tell(f"  {entry}: a kind of {kind} ({len(files)} input(s)), reduced from {len(data)} to {len(reduced)} bytes")
        return run
    finally:
        made.cleanup()


# --- the private record of the findings

_KIND_SAYS = {
    DISAGREEMENT: "the programs do not agree on these files: some accept them, some do not",
    REJECTED: "every program rejects these files, although the spec says that its files always decode",
}


def _finding_report(f: Finding, targets: dict[str, Target], meta: dict) -> str:
    rows = "\n".join(f"| {name} | {category}{': ' + reason if reason else ''} |" for name, category, reason in f.answers)
    ran = "\n".join(f"    {' '.join(portable(targets[name]))}" for name, _, _ in f.answers if name in targets)
    return f"""# {f.kind} on files generated from {f.spec}

**What it is:** {_KIND_SAYS[f.kind]}.
**Seen:** {f.inputs} input(s) of this run got these answers. The smallest, `original{f.suffix}` ({len(f.example)} bytes),
is reduced to `reduced{f.suffix}` ({len(f.reduced)} bytes), which gets the same ones.

What each program said about the reduced input:

| target | answer |
|---|---|
{rows}

## What ran

{ran}

(`INPUT` is the file.)

## How the files were made

Fanbase {meta['fanbase']}, Fandango {meta['fandango']}, spec `{f.spec}`, random seed {meta['seed']}, {meta['count']} inputs asked for.
To make them again: `{meta['command']}`.

## Before you report it

- [ ] Decide which program is wrong, or the spec, with the format's own specification.
- [ ] Look for an issue that says the same, open or closed: one report for each cause.
- [ ] This is not a crash. If the input makes a program misread a file, look whether it can be made to crash too
      (a build with AddressSanitizer helps); if it can, report that **privately** first.
"""


def findings_extra(findings: list[Finding], groups: Counter, targets: dict[str, Target], meta: dict) -> Extra:
    extra = Extra()
    kept = 0
    rows = []
    for f in findings:
        base = f"findings/{f.id}"
        extra.members.append((f"{base}/REPORT.md", _finding_report(f, targets, meta).encode()))
        keep_example = kept + len(f.example) + len(f.reduced) <= MAX_FINDING_BYTES
        if kept + len(f.reduced) <= MAX_FINDING_BYTES:
            extra.members.append((f"{base}/reduced{f.suffix}", f.reduced))
            kept += len(f.reduced)
        if keep_example:
            extra.members.append((f"{base}/original{f.suffix}", f.example))
            kept += len(f.example)
        rows.append({"id": f.id, "kind": f.kind, "spec": f.spec, "signature": f.signature, "inputs": f.inputs,
                     "reduced_bytes": len(f.reduced), "answers": [list(a) for a in f.answers]})
    total = sum(groups.values())
    extra.manifest = {"findings": rows, "finding_kinds": dict(groups), "findings_not_itemised": max(0, total - len(findings))}
    extra.index = ["", f"{total} kind(s) of finding: {groups[DISAGREEMENT]} of disagreement, {groups[REJECTED]} of rejected valid input.", ""]
    extra.index += [f"- `findings/{f.id}`: {f.kind} on {f.spec} ({f.inputs}x)" for f in findings]
    if total > len(findings):
        extra.index += ["", f"{total - len(findings)} more kind(s) were found after {MAX_FINDINGS} had been written down; "
                            "fix these, or run it on your machine with a smaller -n, to see them."]
    return extra


# --- saying it, in public: counts only

def to_json(runs: list[SpecRun], args, record: str | None) -> dict:
    groups = sum((r.groups for r in runs), Counter())
    return {
        "schema": REPORT_SCHEMA, "fanbase": __version__, "seed": args.seed, "count": args.count,
        "specs": [{"spec": str(r.entry), "version": r.version, "sha256": r.sha256, "produced": r.produced,
                   "seconds": round(r.seconds, 3), "note": r.note or None, "error": r.error,
                   "targets": [{"name": name, **{k: c[k] for k in ("accepted", "rejected", "error")}} for name, c in r.tally.items()]}
                  for r in runs],
        "findings": {"kinds": sum(groups.values()), DISAGREEMENT: groups[DISAGREEMENT], REJECTED: groups[REJECTED]},
        "record": record,
    }


def _closing(groups: Counter) -> str:
    total = sum(groups.values())
    found = (f"{total} kind(s) of finding ({groups[DISAGREEMENT]} of disagreement, {groups[REJECTED]} of rejected valid input)"
             if total else "no findings")
    return f"{found}; what broke a program, if anything did, is counted as an error and said only in the private record"


def as_text(runs: list[SpecRun], args) -> str:
    out = []
    for r in runs:
        head = f"{r.entry}  version {r.version or '-'}"
        if r.error:
            out += [f"{head}: no inputs: {clean(r.error)}", ""]
            continue
        out.append(f"{head}: {r.produced} inputs from seed {args.seed} in {r.seconds:.1f}s")
        if r.note:
            out.append(f"  note: {clean(r.note)}")
        width = max((len(n) for n in r.tally), default=0)
        for name, c in r.tally.items():
            out.append(f"  {name:<{width}}  accepted {c['accepted']}  rejected {c['rejected']}  error {c['error']}")
        out.append("")
    out.append(_closing(sum((r.groups for r in runs), Counter())))
    return "\n".join(out) + "\n"


def as_markdown(runs: list[SpecRun], args) -> str:
    out = [f"## Fuzzing with Fandango: {len(runs)} spec(s)", "",
           f"{args.count} inputs from each, seed {args.seed}; fanbase {__version__}.", ""]
    for r in runs:
        out += [f"### `{r.entry}` {r.version or ''}".rstrip(), ""]
        if r.error:
            out += [f"**No inputs:** {clean(r.error)}", ""]
            continue
        out += [f"{r.produced} inputs in {r.seconds:.1f}s." + (f" {clean(r.note)}." if r.note else ""), "",
                "| target | accepted | rejected | error |", "|---|--:|--:|--:|"]
        out += [f"| {name} | {c['accepted']} | {c['rejected']} | {c['error']} |" for name, c in r.tally.items()]
        out.append("")
    out += [_closing(sum((r.groups for r in runs), Counter())).capitalize() + ".", ""]
    return "\n".join(out)


# --- the command

def cmd_fuzz(args, ctx: Context) -> int:
    """Fuzz parsers with inputs from specs; what is found goes into a private record."""
    recipients = Path(args.recipients) if args.recipients else None
    if recipients:
        check_recipients(recipients)  # before anything runs: no way to keep it private, no run
    if args.count < 1:
        raise RegistryError("-n is the number of inputs to make from each spec: at least 1")
    reg = checkout(ctx)
    fandango = find_fandango()
    if fandango is None:
        raise RegistryError("fuzz needs fandango to produce the inputs (pip install fandango-fuzzer)")
    entries = [reg.resolve(ref) for ref in args.refs]
    for entry in entries:  # every target is there and runs, before any input is made
        try:
            _targets(reg, entry, args)
        except TargetError as exc:
            raise RegistryError(f"{entry}: {exc}") from None

    log = IncidentLog()
    findings: list[Finding] = []
    runs: list[SpecRun] = []
    used: dict[str, Target] = {}
    def say(line: str) -> None:
        print(line, file=sys.stderr)

    def tell(line: str) -> None:  # with recipients this is CI, where what was found is not said
        if recipients is None:
            print(line, file=sys.stderr)

    scratch = work_dir()
    try:
        for entry in entries:
            print(f"fuzzing with {entry} ...", file=sys.stderr)
            used.update({t.name: t for t in _targets(reg, entry, args)})
            runs.append(fuzz_spec(reg, entry, ctx, args, fandango, scratch, log, findings, say=say, tell=tell))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    groups = sum((r.groups for r in runs), Counter())
    meta = run_meta(args.seed, args.count)
    meta["command"] = (f"fanbase fuzz {' '.join(args.refs)}" + (f" --targets {args.targets}" if args.targets else "")
                       + f" -n {args.count} --seed {args.seed}")
    extra = findings_extra(findings, groups, used, meta)
    saved = write_private(log, meta, Path(args.record), recipients, extra=extra, bundle_name=FUZZ_BUNDLE, folder_prefix="fuzz")
    if recipients:  # the same words whatever happened
        print(f"private record: {saved}", file=sys.stderr)
    elif saved:
        print(f"what was found is in {saved} (only you can read it)", file=sys.stderr)

    if args.json_file:
        Path(args.json_file).write_text(json.dumps(to_json(runs, args, str(saved) if saved else None), indent=2) + "\n",
                                        encoding="utf-8")
    print(as_markdown(runs, args) if args.markdown else as_text(runs, args), end="")

    failed = [r for r in runs if r.error]
    if failed:
        print(f"no inputs from {', '.join(str(r.entry) for r in failed)}", file=sys.stderr)
        return 1
    if args.fail and (sum(groups.values()) or log.incidents):
        return 1
    return 0
