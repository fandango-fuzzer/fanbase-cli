"""What is known about how well a spec does: the signals a person picks between competing grammars by.

`fanbase evaluate` produces a report of one run. `quality.json` is what is kept of it: for each spec, tied to the
hash of the spec it was measured on, how often each parser accepts its files, how much of a library its files
reach, and how fast it makes them.

    {"schema": 1, "made": "2026-10-08T03:00:00Z", "fanbase": "0.5.0", "fandango": "1.3.0",
     "specs": {"png/png": {"version": "1.0", "sha256": "...", "count": 100, "seed": 1, "per_second": 15.5,
                           "decodes": "always", "best_accepted": 1.0, "expectation_met": true,
                           "targets":  {"pillow": {"version": "12.3.0", "accepted": 1.0, "files": 100}},
                           "coverage": {"libpng-cov": {"version": "libpng 1.6.59", "lines": 0.225, "branches": 0.169,
                                                       "seeds_lines": 0.161, "only_generated": 728, "only_seeds": 210,
                                                       "curve": [[1, 0.149], [10, 0.204], [100, 0.225]]}}}}}

It is made from evaluation reports (`fanbase quality build`), published with a registry's release, and read by
`fanbase list --quality` and `show --quality`, and by `evaluate --compare-with`, which says what changed since.
It comes from a registry that may not be yours, so it is read strictly, and shown as text with nothing in it that
could do anything: numbers, and names cleaned of control characters.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from fanbase import __version__
from fanbase.output import clean
from fanbase.registry import Entry, RegistryBase, RegistryError, is_safe_name

QUALITY_FILENAME = "quality.json"
SCHEMA = 1
MAX_BYTES = 8 * 1024 * 1024
MAX_SPECS = 5000

_SHA = re.compile(r"[0-9a-f]{64}")
_SPEC = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")


def _ratio(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 4) if 0 <= value <= 1 else None


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _curve(value: object) -> list[list]:
    """[[inputs, share of lines], ...] as far as it is that."""
    out = []
    for point in value[:20] if isinstance(value, list) else []:
        if isinstance(point, list) and len(point) == 2 and _count(point[0]) is not None and _ratio(point[1]) is not None:
            out.append([point[0], _ratio(point[1])])
    return out


def _text(value: object, limit: int = 80) -> str | None:
    return clean(value)[:limit] if isinstance(value, str) and value.strip() else None


# --- making it

def build(reports: list[dict], previous: dict | None = None) -> dict:
    """The quality of the specs in these evaluation reports (schema 1 of `fanbase evaluate --json`). Later reports
    add to earlier ones for the same spec, so that a run of the parsers and a run of the coverage can be one
    document. `previous`, if given, is added to rather than replaced: what it knows of other specs stays."""
    specs: dict[str, dict] = {}  # (the values are dicts of mixed things: json)
    if previous:
        specs.update({k: dict(v) for k, v in previous.get("specs", {}).items()})
    fanbase_version = fandango_version = None
    for report in reports:
        if report.get("schema") != 1 or not isinstance(report.get("specs"), list):
            raise RegistryError("not an evaluation report: `fanbase evaluate --json-file` writes one")
        fanbase_version = report.get("fanbase") or fanbase_version
        fandango_version = report.get("fandango") or fandango_version
        for spec in report["specs"]:
            if spec.get("error") or not spec.get("sha256"):
                continue
            key = spec["spec"]
            current = specs.get(key)
            if current is None or current.get("sha256") != spec["sha256"]:
                current = specs[key] = {"version": spec.get("version"), "sha256": spec["sha256"], "targets": {}, "coverage": {}}
            targets: dict = current["targets"]
            coverages: dict = current["coverage"]
            generated = spec.get("generated") or {}
            current.update(count=generated.get("produced"), seed=report.get("seed"),
                           per_second=generated.get("per_second"), decodes=spec.get("decodes"))
            for target in spec.get("targets", []):
                if target.get("status") != "ok" or not target.get("total"):
                    continue
                targets[target["name"]] = {
                    "version": target.get("version"), "accepted": round(target["accepted"] / target["total"], 4),
                    "files": target["total"]}
                if coverage := target.get("coverage"):
                    last = (coverage.get("curve") or [{}])[-1]
                    coverages[target["name"]] = {
                        "version": target.get("version"), "lines": last.get("lines_share"), "branches": last.get("branches_share"),
                        "seeds_lines": (coverage.get("seeds") or {}).get("lines_share"),
                        "only_generated": coverage.get("only_generated_lines"), "only_seeds": coverage.get("only_seed_lines"),
                        "lines_total": coverage.get("lines_total"), "inputs": last.get("inputs"),
                        "curve": [[p["inputs"], p["lines_share"]] for p in coverage.get("curve") or []
                                  if p.get("lines_share") is not None]}
            shares = [t["accepted"] for t in targets.values()]
            current["best_accepted"] = max(shares, default=None)
            if current.get("decodes") and current["best_accepted"] is not None:
                from fanbase.evaluate import band

                current["expectation_met"] = band(current["best_accepted"]) == current["decodes"]
            else:
                current["expectation_met"] = None
    return {"schema": SCHEMA, "made": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "fanbase": fanbase_version or __version__,
            "fandango": fandango_version, "specs": dict(sorted(specs.items()))}


# --- reading it

def parse(data: bytes | str) -> dict:
    """A quality document, or a report of `fanbase evaluate`, read strictly: only what is known is kept, as numbers
    and cleaned text; anything that does not fit is an error."""
    if len(data) > MAX_BYTES:
        raise RegistryError("the quality results are far too large")
    try:
        doc = json.loads(data)
    except ValueError:
        raise RegistryError("the quality results are not JSON") from None
    if isinstance(doc, dict) and isinstance(doc.get("specs"), list):  # an evaluation report: make it one
        return parse(json.dumps(build([doc])))
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("specs"), dict):
        raise RegistryError(f"the quality results should be a JSON object, schema {SCHEMA}, with `specs`")
    if len(doc["specs"]) > MAX_SPECS:
        raise RegistryError("the quality results name far too many specs")
    out: dict = {"schema": SCHEMA, "made": _text(doc.get("made"), 30), "fanbase": _text(doc.get("fanbase"), 30),
                 "fandango": _text(doc.get("fandango"), 30), "specs": {}}
    for key, spec in doc["specs"].items():
        if not (isinstance(key, str) and _SPEC.fullmatch(key) and isinstance(spec, dict)):
            raise RegistryError("the quality results name a spec that is not a spec")
        sha = spec.get("sha256")
        if not (isinstance(sha, str) and _SHA.fullmatch(sha)):
            raise RegistryError(f"the quality results for {clean(key)} do not say which file they are for")
        speed = spec.get("per_second")
        row: dict = {"version": _text(spec.get("version"), 30), "sha256": sha, "count": _count(spec.get("count")),
                     "seed": _count(spec.get("seed")),
                     "per_second": speed if isinstance(speed, (int, float)) and not isinstance(speed, bool) and speed >= 0 else None,
                     "decodes": spec.get("decodes") if spec.get("decodes") in ("always", "mostly", "rarely", "never") else None,
                     "best_accepted": _ratio(spec.get("best_accepted")),
                     "expectation_met": spec.get("expectation_met") if isinstance(spec.get("expectation_met"), bool) else None,
                     "targets": {}, "coverage": {}}
        for name, target in (spec.get("targets") or {}).items():
            if is_safe_name(name) and isinstance(target, dict):
                row["targets"][name] = {"version": _text(target.get("version")), "accepted": _ratio(target.get("accepted")),
                                        "files": _count(target.get("files"))}
        for name, cov in (spec.get("coverage") or {}).items():
            if is_safe_name(name) and isinstance(cov, dict):
                row["coverage"][name] = {
                    "version": _text(cov.get("version")), "lines": _ratio(cov.get("lines")), "branches": _ratio(cov.get("branches")),
                    "seeds_lines": _ratio(cov.get("seeds_lines")), "only_generated": _count(cov.get("only_generated")),
                    "only_seeds": _count(cov.get("only_seeds")), "lines_total": _count(cov.get("lines_total")),
                    "inputs": _count(cov.get("inputs")), "curve": _curve(cov.get("curve"))}
        out["specs"][key] = row
    return out


def load(source: str | Path) -> dict:
    """The quality results at a path or a URL."""
    from fanbase.remote import RegistryUnavailable, _get

    text = str(source)
    if text.startswith(("http://", "https://")):
        if text.startswith("http://") and not re.match(r"http://(localhost|127\.0\.0\.1|\[::1\])[:/]", text):
            raise RegistryError("quality results are read over https, not plain http")
        try:
            return parse(_get(text))
        except RegistryUnavailable as exc:
            raise RegistryError(str(exc)) from None
    path = Path(text)
    try:
        return parse(path.read_bytes())
    except OSError as exc:
        raise RegistryError(f"{path}: {exc.strerror or exc}") from None


def default_source(reg: RegistryBase) -> str | None:
    """Where a registry keeps its quality results: what it says in its registry.yml (`quality:`), else a
    quality.json in a checkout, else the latest release of a GitHub registry."""
    from fanbase.registry import Registry

    declared = reg.info.get("quality")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    if isinstance(reg, Registry):
        found = reg.root / QUALITY_FILENAME
        return str(found) if found.is_file() else None
    url = getattr(reg, "url", None)
    if isinstance(url, str):
        match = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/tree/[^/]+.*)?/?$", url)
        if match:
            return f"https://github.com/{match.group(1)}/{match.group(2)}/releases/latest/download/{QUALITY_FILENAME}"
    return None


def of(doc: dict, entry: Entry, sha256: str) -> tuple[dict, bool] | None:
    """What is known of a spec, and whether it was measured on the spec as it is now."""
    row = doc["specs"].get(str(entry))
    return None if row is None else (row, row["sha256"] == sha256)


def speed(per_second: float) -> str:
    """Files per second in few digits: 41, 1.3, 0.05. A slow spec must not read as 0."""
    return f"{per_second:.0f}" if per_second >= 10 else f"{per_second:.1f}" if per_second >= 1 else f"{per_second:.2f}"


def summary(row: dict, current: bool = True) -> str:
    """A spec's quality in a few words."""
    parts = []
    if row.get("best_accepted") is not None:
        parts.append(f"accepts {row['best_accepted']:.0%}")
    lines = [c["lines"] for c in row.get("coverage", {}).values() if c.get("lines") is not None]
    if lines:
        parts.append(f"covers {max(lines):.1%}")
    if row.get("per_second"):
        parts.append(f"{speed(row['per_second'])}/s")
    text = ", ".join(parts) or "measured"
    return text if current else f"{text} (for an earlier version)"


# --- for the commands

def for_registry(reg: RegistryBase, wanted: str | None) -> dict | None:
    """The quality results a command was asked to show (`--quality` alone: the registry's own, `--quality SOURCE`: that
    one). None, with a word on stderr, when there are none to be had: what is known of a spec is never a reason for a
    command to fail."""
    import sys

    if not wanted:
        return None
    source = default_source(reg) if wanted == "auto" else wanted
    if source is None:
        print("no quality results: this registry does not say where it keeps them (give --quality FILE or URL)", file=sys.stderr)
        return None
    try:
        return load(source)
    except RegistryError as exc:
        print(f"no quality results: {clean(exc)}", file=sys.stderr)
        return None


def cmd_quality_build(args, ctx) -> int:
    """Make quality.json from the reports of `fanbase evaluate --json-file`."""
    import sys

    reports = []
    for name in args.reports:
        try:
            reports.append(json.loads(Path(name).read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise RegistryError(f"{name}: not a report of `fanbase evaluate --json-file` ({getattr(exc, 'strerror', None) or 'not JSON'})") from None
    previous = load(args.add_to) if args.add_to else None
    document = parse(json.dumps(build(reports, previous)))
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if args.output == "-":
        print(text, end="")
    else:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"{len(document['specs'])} spec(s) in {args.output}", file=sys.stderr)
    return 0
