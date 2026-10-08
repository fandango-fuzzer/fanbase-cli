"""How much of a parser the files of a spec reach.

A file that a parser accepts says little about how much of the parser it exercised. For a target that
is built to be measured (see `coverage:` in targets.py), `fanbase evaluate --coverage` asks how much of
the library's own code the generated files run, and compares it with a few real files:

  - the real files (the target's `seeds`) are run first: what they reach is the baseline;
  - the generated files are run in order, and what they have reached by the 1st, 10th, 100th, 1000th
    is the growth curve;
  - and the two are compared: what the generated files reach that the real ones do not, and what the
    real ones reach that the generated ones do not.

fanbase knows nothing about how the coverage is counted. The target says how to clear it (`reset`) and how
to read it (`snapshot`), and the snapshot is a JSON document:

    {"schema": 1,
     "lines":    {"total": 4411, "covered": ["png.c:101", "png.c:102", ...]},      # "file:line"
     "branches": {"total": 1830, "covered": ["png.c:101:0", ...]}}                # optional

so that a library built with gcov, a Python module measured with coverage.py, or anything else, can be
measured the same way. What is covered is compared as sets, so the entries only have to be the same text
for the same place. The registry's `coverage/` has the image that measures C libraries with gcov.

The measuring runs one file at a time, in order: the counters are shared by every run.
"""

from __future__ import annotations

import glob
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from fanbase.output import clean
from fanbase.registry import RegistryError
from fanbase.targets import Target, TargetError, _expand, judge

SNAPSHOT_TIMEOUT = 300  # seconds for the target to say what was covered
MAX_ENTRIES = 3_000_000  # a snapshot with more covered lines than this is not one
MAX_SNAPSHOT = 256 * 1024 * 1024  # bytes
DEFAULT_CURVE = (1, 10, 100, 1000)


class CoverageError(TargetError):
    """The coverage of a target could not be measured."""


@dataclass
class Snapshot:
    lines: frozenset[str]
    lines_total: int
    branches: frozenset[str] | None = None
    branches_total: int | None = None


def _entries(value: object, what: str) -> tuple[frozenset[str], int]:
    if not (isinstance(value, dict) and isinstance(value.get("total"), int) and not isinstance(value["total"], bool)
            and value["total"] >= 0 and isinstance(value.get("covered"), list)):
        raise CoverageError(f"the snapshot's {what} should be {{\"total\": N, \"covered\": [...]}}")
    covered = value["covered"]
    if len(covered) > MAX_ENTRIES or not all(isinstance(c, str) and c for c in covered):
        raise CoverageError(f"the snapshot's {what} should list at most {MAX_ENTRIES} places, as text")
    return frozenset(covered), value["total"]


def parse_snapshot(text: str) -> Snapshot:
    if len(text) > MAX_SNAPSHOT:
        raise CoverageError("the snapshot is far too large")
    try:
        data = json.loads(text)
    except ValueError:
        raise CoverageError("the snapshot is not JSON") from None
    if not isinstance(data, dict) or data.get("schema", 1) != 1 or "lines" not in data:
        raise CoverageError("the snapshot should be a JSON object, schema 1, with `lines`")
    lines, lines_total = _entries(data["lines"], "lines")
    branches = branches_total = None
    if data.get("branches") is not None:
        branches, branches_total = _entries(data["branches"], "branches")
    return Snapshot(lines, lines_total, branches, branches_total)


def _run(target: Target, argv: tuple[str, ...], cwd: Path | None) -> subprocess.CompletedProcess:
    cmd = _expand(argv, target)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=SNAPSHOT_TIMEOUT,
                              stdin=subprocess.DEVNULL, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise CoverageError(f"{target.name}: {cmd[0]} did not finish in {SNAPSHOT_TIMEOUT} seconds") from None
    except OSError as exc:
        raise CoverageError(f"{target.name}: cannot run {cmd[0]}: {exc}") from None


def reset(target: Target, cwd: Path | None = None) -> None:
    """Forget what has been covered."""
    assert target.coverage is not None
    if not target.coverage.reset:
        return
    done = _run(target, target.coverage.reset, cwd)
    if done.returncode != 0:
        raise CoverageError(f"{target.name}: the reset failed: {clean((done.stderr or done.stdout).strip()[-200:])}")


def take(target: Target, cwd: Path | None = None) -> Snapshot:
    """What has been covered since the last reset."""
    assert target.coverage is not None
    done = _run(target, target.coverage.snapshot, cwd)
    if done.returncode != 0:
        raise CoverageError(f"{target.name}: the snapshot failed: {clean((done.stderr or done.stdout).strip()[-200:])}")
    try:
        return parse_snapshot(done.stdout)
    except CoverageError as exc:
        raise CoverageError(f"{target.name}: {exc}") from None


def seed_files(target: Target) -> list[Path]:
    """The real files a target compares the generated ones with: what its `seeds` globs match."""
    assert target.coverage is not None
    found: list[Path] = []
    for pattern in target.coverage.seeds:
        path = Path(pattern.replace("{dir}", str(target.directory)))
        if not path.is_absolute():
            path = target.directory / path
        found += [Path(p) for p in sorted(glob.glob(str(path))) if Path(p).is_file()]
    return found


@dataclass
class CoverageResult:
    lines_total: int
    branches_total: int | None
    seeds: int  # real files that were run
    seed_lines: int | None  # what they reach (None: there were none)
    seed_branches: int | None
    curve: list[tuple[int, int, int | None]] = field(default_factory=list)  # (inputs, lines, branches)
    only_generated: int | None = None  # lines the generated files reach and the real ones do not
    only_seeds: int | None = None  # lines the real files reach and the generated ones do not
    note: str = ""

    @property
    def final(self) -> tuple[int, int, int | None] | None:
        return self.curve[-1] if self.curve else None

    def to_dict(self) -> dict:
        def share(n: int | None, total: int | None) -> float | None:
            return None if n is None or not total else round(n / total, 4)

        return {
            "lines_total": self.lines_total,
            "branches_total": self.branches_total,
            "seeds": {"files": self.seeds, "lines": self.seed_lines, "branches": self.seed_branches,
                      "lines_share": share(self.seed_lines, self.lines_total)},
            "curve": [{"inputs": n, "lines": lines, "lines_share": share(lines, self.lines_total),
                       "branches": branches, "branches_share": share(branches, self.branches_total)}
                      for n, lines, branches in self.curve],
            "only_generated_lines": self.only_generated,
            "only_seed_lines": self.only_seeds,
            "note": self.note or None,
        }


def checkpoints(curve: tuple[int, ...], total: int) -> list[int]:
    """The numbers of files at which to read the coverage: those of the curve that there are files for, and
    the last file, if it is not one of them."""
    points = sorted({n for n in curve if 0 < n <= total})
    if total and (not points or points[-1] != total):
        points.append(total)
    return points


def measure(target: Target, files: list[Path], curve: tuple[int, ...] = DEFAULT_CURVE, *, timeout: int = 20,
            memory_mb: int = 2048, cwd: Path | None = None) -> CoverageResult:
    """How much of the target the real files reach, and how much the generated ones do as there are more."""
    assert target.coverage is not None
    seeds = seed_files(target)
    baseline: Snapshot | None = None
    if seeds:
        reset(target, cwd)
        for seed in seeds:
            judge(target, seed, timeout=timeout, memory_mb=memory_mb, cwd=cwd)  # (what it says is not the point)
        baseline = take(target, cwd)

    reset(target, cwd)
    points = checkpoints(curve, len(files))
    snapshots: list[tuple[int, Snapshot]] = []
    for n, file in enumerate(files, 1):
        judge(target, file, timeout=timeout, memory_mb=memory_mb, cwd=cwd)
        if n in points:
            snapshots.append((n, take(target, cwd)))
    if not snapshots:
        raise CoverageError(f"{target.name}: there are no files to measure")

    last = snapshots[-1][1]
    total = last.lines_total or (baseline.lines_total if baseline else 0)
    result = CoverageResult(
        lines_total=total, branches_total=last.branches_total,
        seeds=len(seeds), seed_lines=len(baseline.lines) if baseline else None,
        seed_branches=len(baseline.branches) if baseline and baseline.branches is not None else None,
        curve=[(n, len(s.lines), None if s.branches is None else len(s.branches)) for n, s in snapshots],
    )
    if baseline is not None:
        result.only_generated = len(last.lines - baseline.lines)
        result.only_seeds = len(baseline.lines - last.lines)
    else:
        result.note = "the target has no real files to compare with (coverage.seeds)"
    return result


def coverage_targets(reg_root: Path, fmt: str, already: list[Target]) -> list[Target]:
    """The targets of the registry that can be measured and judge this format, which are not among `already`."""
    from fanbase.targets import load_target, target_names

    have = {t.name for t in already}
    out = []
    for name in target_names(reg_root):
        if name in have:
            continue
        try:
            target = load_target(reg_root, name)
        except RegistryError:
            continue  # a target that cannot be loaded is told of where it is asked for by name
        if target.coverage is not None and fmt in target.formats:
            out.append(target)
    return out
