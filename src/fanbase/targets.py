"""Targets: the parsers and tools that files of a format are judged by.

A target is a folder `targets/<name>/` of a registry, with a `target.yml`:

    title: Pillow
    formats: [png, gif, bmp, jpeg, tiff, webp]            # what it can judge
    run: ["{python}", "{dir}/harness.py", "{file}"]       # how to ask it about one file
    needs:                                                # what has to be there for it to run
      commands: [identify]
      python: [PIL]                                       # modules it imports
      pip: [Pillow]                                       # what to install to get them
    version: ["{python}", "-c", "import PIL; print(PIL.__version__)"]
    classify:                                             # what a rejection means, by what it says
      - match: "(?i)decompression ?bomb|exceeds limit"
        as: resource-limit

`{file}` is the file asked about (exactly once), `{dir}` the target's own folder, `{python}` the
interpreter that runs fanbase. The command is run for each file, with limits, and its exit status is
the verdict: 0 accepts the file; anything else rejects it, and what it printed says why. Killed by
a signal is a crash, or, when it ran out of memory or time, a resource limit.

A target that is built to be measured also says how, in a `coverage:` section (see coverage.py):

    coverage:
      reset: ["/opt/cov/reset", "libpng"]          # clears what has been covered
      snapshot: ["/opt/cov/snapshot", "libpng"]    # prints what has been covered since, as JSON
      seeds: ["/opt/cov/seeds/png/*"]              # real files to compare the generated ones with

A target is a command that a registry tells fanbase to run, so it is as much code as a spec is. They are
run from a registry checkout, by whoever has read it, and in CI on a machine that is thrown away.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from fanbase.manager import declared_requirements
from fanbase.output import clean
from fanbase.registry import TARGETS_DIRNAME, RegistryError, is_safe_name

TARGET_FILENAME = "target.yml"

ACCEPTED, INVALID, UNSUPPORTED, LIMIT, CRASH, TIMEOUT, ERROR = (
    "accepted", "invalid", "unsupported", "resource-limit", "crash", "timeout", "error",
)
CATEGORIES = (ACCEPTED, INVALID, UNSUPPORTED, LIMIT, CRASH, TIMEOUT, ERROR)
_CLASSIFIABLE = (INVALID, UNSUPPORTED, LIMIT)

_COMMAND = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
_PLACEHOLDER = re.compile(r"\{[^{}]*\}")
_PLACEHOLDERS = ("{file}", "{dir}", "{python}")

# Killed by one of these: it ran out of memory or of time. By any other signal: it crashed.
_LIMIT_SIGNALS = {getattr(signal, name) for name in ("SIGKILL", "SIGXCPU") if hasattr(signal, name)}


class TargetError(RegistryError):
    """A target cannot be loaded or run."""


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern
    category: str


@dataclass(frozen=True)
class CoverageSpec:
    snapshot: tuple[str, ...]  # prints what has been covered since the last reset, as JSON (see coverage.py)
    reset: tuple[str, ...] = ()
    seeds: tuple[str, ...] = ()  # globs of real files; relative ones to the target's folder


@dataclass(frozen=True)
class Target:
    name: str
    directory: Path
    title: str
    formats: tuple[str, ...]
    run: tuple[str, ...]
    commands: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    pip: tuple[str, ...] = ()
    rules: tuple[Rule, ...] = ()
    version: tuple[str, ...] | None = None
    coverage: CoverageSpec | None = None


INCIDENT_STDERR = 8192  # how much of what a target said is kept for the private record

CRASH_KIND, HANG_KIND, KILLED_KIND = "crash", "hang", "killed"


@dataclass(frozen=True)
class Verdict:
    category: str
    reason: str = ""
    # An incident is a target that broke on a file: it crashed, hung, or was killed for what it used. It is
    # not for any public place (it may be a vulnerability that is not fixed yet), and the private record
    # of it is made from what follows.
    incident: str = ""  # "", or crash, hang or killed
    returncode: int | None = None
    stderr: bytes = b""
    argv: tuple[str, ...] = ()  # what was run, with the file as INPUT


def _strings(value: object, where: str, what: str, empty: bool = False) -> list[str]:
    if not (isinstance(value, list) and (value or empty) and all(isinstance(v, str) and v for v in value)):
        raise TargetError(f"{where}: {what} has to be a list of texts")
    return list(value)


def _command_line(value: object, where: str, what: str, needs_file: bool) -> tuple[str, ...]:
    argv = _strings(value, where, what)
    text = "\0".join(argv)
    for found in _PLACEHOLDER.findall(text):
        if found not in _PLACEHOLDERS:
            raise TargetError(f"{where}: {what} has {found}, which is not one of {', '.join(_PLACEHOLDERS)}")
    if needs_file and text.count("{file}") != 1:
        raise TargetError(f"{where}: {what} has to name the file once, as {{file}}")
    return tuple(argv)


def _check_files(argv: tuple[str, ...], folder: Path, where: str, what: str) -> None:
    """What a command line names in the target's own folder has to be there."""
    for part in argv:
        if part.startswith("{dir}/"):
            relative = part[len("{dir}/"):]
            if ".." in Path(relative).parts:
                raise TargetError(f"{where}: {what} names {relative}, which leaves the target's folder")
            if not (folder / relative).exists():
                raise TargetError(f"{where}: {what} names {relative}, which is not in the target's folder")


def load_target(root: Path, name: str) -> Target:
    """The target `name` of the registry at `root`; raises TargetError if it is not one."""
    if not is_safe_name(name):
        raise TargetError(f"{name!r} is not a target name")
    # absolute: a target is run from another folder, where a relative path would lead nowhere
    folder = (root / TARGETS_DIRNAME / name).resolve()
    path = folder / TARGET_FILENAME
    where = f"{TARGETS_DIRNAME}/{name}/{TARGET_FILENAME}"
    if not path.is_file():
        raise TargetError(f"no target called {name}: there is no {where}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TargetError(f"{where}: invalid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise TargetError(f"{where}: expected a mapping at the top level")

    title = data.get("title", name)
    if not (isinstance(title, str) and title.strip()):
        raise TargetError(f"{where}: title has to be text")
    formats = _strings(data.get("formats"), where, "formats")
    if not all(is_safe_name(f) for f in formats):
        raise TargetError(f"{where}: formats has to be a list of format names")
    run = _command_line(data.get("run"), where, "run", needs_file=True)
    version = _command_line(data["version"], where, "version", needs_file=False) if "version" in data else None

    _check_files(run, folder, where, "run")
    if version:
        _check_files(version, folder, where, "version")

    needs = data.get("needs") or {}
    if not isinstance(needs, dict):
        raise TargetError(f"{where}: needs has to be a mapping")
    commands = _strings(needs["commands"], where, "needs.commands", empty=True) if "commands" in needs else []
    if not all(_COMMAND.fullmatch(c) for c in commands):
        raise TargetError(f"{where}: needs.commands has to be a list of command names, without a path")
    modules = _strings(needs["python"], where, "needs.python", empty=True) if "python" in needs else []
    if not all(_MODULE.fullmatch(m) for m in modules):
        raise TargetError(f"{where}: needs.python has to be a list of module names")
    pip = _strings(needs["pip"], where, "needs.pip", empty=True) if "pip" in needs else []
    try:
        declared_requirements({"pip": pip})
    except RegistryError as exc:
        raise TargetError(f"{where}: needs.pip: {exc}") from None

    rules = []
    for item in data.get("classify") or []:
        if not (isinstance(item, dict) and isinstance(item.get("match"), str) and item.get("as") in _CLASSIFIABLE):
            raise TargetError(f"{where}: each classify rule needs a match, and `as` one of {', '.join(_CLASSIFIABLE)}")
        if len(item["match"]) > 200:
            raise TargetError(f"{where}: a classify pattern is longer than 200 characters")
        try:
            rules.append(Rule(re.compile(item["match"]), item["as"]))
        except re.error as exc:
            raise TargetError(f"{where}: classify pattern {item['match']!r}: {exc}") from None

    coverage = _coverage(data.get("coverage"), folder, where)
    return Target(name, folder, title.strip(), tuple(formats), run, tuple(commands), tuple(modules), tuple(pip),
                  tuple(rules), version, coverage)


def _coverage(value: object, folder: Path, where: str) -> CoverageSpec | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"reset", "snapshot", "seeds"}:
        raise TargetError(f"{where}: coverage has to be a mapping with snapshot, and reset and seeds if needed")
    if "snapshot" not in value:
        raise TargetError(f"{where}: coverage needs a snapshot: the command that says what has been covered")
    snapshot = _command_line(value["snapshot"], where, "coverage.snapshot", needs_file=False)
    reset = _command_line(value["reset"], where, "coverage.reset", needs_file=False) if "reset" in value else ()
    for argv, what in ((snapshot, "coverage.snapshot"), (reset, "coverage.reset")):
        _check_files(argv, folder, where, what)
        if any("{file}" in part for part in argv):
            raise TargetError(f"{where}: {what} is not about a file: it cannot have {{file}}")
    seeds = tuple(_strings(value["seeds"], where, "coverage.seeds", empty=True)) if "seeds" in value else ()
    if any(".." in Path(seed).parts or "\0" in seed for seed in seeds):
        raise TargetError(f"{where}: coverage.seeds cannot leave the target's folder with ..")
    return CoverageSpec(snapshot, reset, seeds)


def target_names(root: Path) -> list[str]:
    folder = root / TARGETS_DIRNAME
    return sorted(d.name for d in folder.iterdir() if (d / TARGET_FILENAME).is_file()) if folder.is_dir() else []


def unavailable(target: Target) -> str | None:
    """Why the target cannot be run here, or None if it can."""
    missing = [c for c in target.commands if shutil.which(c) is None]
    if missing:
        return f"needs the command {', '.join(missing)}"
    absent = [m for m in target.modules if importlib.util.find_spec(m.split(".")[0]) is None]
    if absent:
        hint = f" (pip install {' '.join(target.pip)})" if target.pip else ""
        return f"needs the Python module {', '.join(absent)}{hint}"
    return None


def portable(target: Target) -> tuple[str, ...]:
    """How a target is run, as something to read on another machine: without the paths of this one."""
    return tuple(part.replace("{file}", "INPUT").replace("{dir}", f"targets/{target.name}").replace("{python}", "python3")
                 for part in target.run)


def _expand(argv: tuple[str, ...], target: Target, file: Path | str = "") -> list[str]:
    return [
        part.replace("{file}", str(file)).replace("{dir}", str(target.directory)).replace("{python}", sys.executable)
        for part in argv
    ]


def version_of(target: Target) -> str | None:
    """What the target says its version is: the first line its `version` command prints."""
    if target.version is None:
        return None
    try:
        done = subprocess.run(_expand(target.version, target), capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL, errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (done.stdout or done.stderr).strip().splitlines()
    if done.returncode != 0 or not lines:
        return None
    # the first line, without the notice that tools tend to add to their version ("... Copyright (c) 2000-2026 ...")
    first = re.split(r"\s+(?:Copyright\b|\(c\)|https?://)", clean(lines[0].strip()), maxsplit=1)[0]
    return first[:60]


# --- judging one file

_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")  # ffmpeg says where in memory it was, which is different each time
_DIMENSIONS = re.compile(r"\d+x\d+")  # an image size: different for every file that is too big
_DIGITS = re.compile(r"\d{3,}")


def normalise_reason(text: str, path: Path) -> str:
    """The first line of what a target said, made the same for files that differ only in
    their name or in a number, so that the same reason is counted as one."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    line = lines[-1] if any("Traceback (most recent call last)" in line for line in lines) else lines[0]
    for name in (str(path), path.name, str(path.parent)):
        line = line.replace(name, "<file>")
    line = _DIMENSIONS.sub("NxN", _ADDRESS.sub("0xN", " ".join(line.split())))
    return clean(_DIGITS.sub("N", line))[:100]


def judge(target: Target, path: Path, *, timeout: int = 10, memory_mb: int = 2048, cwd: Path | None = None) -> Verdict:
    """Ask the target about one file."""
    argv = _expand(target.run, target, path)
    shown = portable(target)
    if os.name == "posix":
        argv = [sys.executable, "-m", "fanbase._limits", str(memory_mb), str(timeout + 5), *argv]
    try:
        done = subprocess.run(argv, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        return Verdict(TIMEOUT, f"no answer in {timeout} seconds", HANG_KIND, None,
                       (exc.stderr or b"")[:INCIDENT_STDERR] if isinstance(exc.stderr, bytes) else b"", shown)
    except (FileNotFoundError, PermissionError) as exc:
        # what exit status 127 and 126 are behind the limits wrapper on POSIX: the target cannot run, which says nothing about the file
        return Verdict(ERROR, clean(f"cannot run {argv[0]}: {exc.strerror or exc.__class__.__name__}")[:100])
    except OSError as exc:
        raise TargetError(f"cannot run the target {target.name}: {exc}") from None

    said = (done.stderr or done.stdout).decode("utf-8", errors="replace")
    if done.returncode == 0:
        return Verdict(ACCEPTED)
    if done.returncode < 0:
        try:
            killer = signal.Signals(-done.returncode)
        except ValueError:
            killer = None
        limited = killer in _LIMIT_SIGNALS
        return Verdict(LIMIT if limited else CRASH, f"killed by {killer.name if killer else -done.returncode}",
                       KILLED_KIND if limited else CRASH_KIND, done.returncode, done.stderr[:INCIDENT_STDERR], shown)
    if done.returncode in (126, 127) or "Traceback (most recent call last)" in said:
        return Verdict(ERROR, normalise_reason(said, path) or f"exited with {done.returncode}")
    reason = normalise_reason(said, path)
    for rule in target.rules:
        if rule.pattern.search(said):
            return Verdict(rule.category, reason)
    return Verdict(INVALID, reason)


@dataclass
class TargetResult:
    name: str
    version: str | None
    total: int = 0
    counts: Counter = field(default_factory=Counter)
    reasons: Counter = field(default_factory=Counter)
    seconds: float = 0.0
    skipped: int = 0  # files not asked about, because the time for this target ran out first

    @property
    def accepted(self) -> int:
        return self.counts[ACCEPTED]

    @property
    def per_second(self) -> float:
        return self.total / self.seconds if self.seconds else 0.0

    def top_reasons(self, n: int = 3) -> list[tuple[str, int]]:
        return self.reasons.most_common(n)


def run_target(target: Target, files: list[Path], *, jobs: int = 1, timeout: int = 10, memory_mb: int = 2048,
               hide_crashes: bool = False, cwd: Path | None = None, budget: float | None = None,
               incidents=None) -> TargetResult:
    """Ask the target about each file. With `hide_crashes`, a crash or a hang is counted as an
    error and nothing about it is kept: for reports that are public. (A hang can be a
    vulnerability that is not fixed yet, as a crash can.)

    A target can be slow on a spec: it may wait out its timeout on file after file. With a `budget`
    (seconds), once the time is up the files not yet asked about are skipped, and counted as such.

    `incidents`, if given, is called as `incidents(target, file, verdict, confirmed)` for each file that
    broke the target, from the worker threads. Before it is, the target is asked about the file once
    more (a hang with twice the time) so as to say whether it happens again: a hang can be a busy machine.
    """
    import time

    result = TargetResult(target.name, version_of(target))
    started = time.monotonic()
    deadline = None if budget is None else started + budget

    def ask(file: Path) -> Verdict | None:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        verdict = judge(target, file, timeout=timeout, memory_mb=memory_mb, cwd=cwd)
        if verdict.incident and incidents is not None:
            again = judge(target, file, timeout=timeout * 2 if verdict.incident == HANG_KIND else timeout,
                          memory_mb=memory_mb, cwd=cwd)
            incidents(target, file, verdict, again.incident == verdict.incident)
        return verdict

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        verdicts = list(pool.map(ask, files))
    result.seconds = time.monotonic() - started
    for verdict in verdicts:
        if verdict is None:
            result.skipped += 1
            continue
        category, reason = verdict.category, verdict.reason
        if hide_crashes and category in (CRASH, TIMEOUT):
            category, reason = ERROR, ""
        result.total += 1
        result.counts[category] += 1
        if category != ACCEPTED and reason:
            result.reasons[f"{category}: {reason}"] += 1
    return result


def work_dir() -> Path:
    """A folder to run targets in, so that what they write does not land in the registry."""
    return Path(tempfile.mkdtemp(prefix="fanbase-targets-"))
