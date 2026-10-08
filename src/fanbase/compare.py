"""What changed in how the specs do since an earlier run (`fanbase evaluate --compare-with`).

Two runs of the same spec on the same parsers give the same numbers (the seed is the same), so a difference
has a cause, and it is either the spec or a parser. A change is only called *worse* when it is the spec's: the
parser is the one it was before. When a parser was updated the numbers can move without the spec having done
anything, and that is told as what it is.

The numbers compared are those of quality.py: the share of files each parser accepts, the share of a library's
lines the files reach, and how fast Fandango makes them (told if it moves a lot, never counted as worse: it is
the machine's as much as the spec's).
"""

from __future__ import annotations

from dataclasses import dataclass

from fanbase.output import clean

ACCEPTED_DROP = 0.05  # five points of the files a parser accepts
COVERAGE_DROP = 0.02  # two points of a library's lines
SPEED_CHANGE = 2.0  # twice as fast or as slow


@dataclass(frozen=True)
class Difference:
    spec: str
    kind: str  # worse, better, note
    text: str

    def to_dict(self) -> dict:
        return {"spec": self.spec, "kind": self.kind, "text": self.text}


def _same_parser(old: dict, new: dict) -> bool:
    return old.get("version") is None or new.get("version") is None or old["version"] == new["version"]


def _moved(spec: str, what: str, old: float | None, new: float | None, drop: float, same_parser: bool, parser: str,
           old_version: object, new_version: object, spec_changed: bool, points: str = "{:.0%}") -> Difference | None:
    if old is None or new is None or abs(new - old) < 1e-9:
        return None
    delta = new - old
    shown = f"{what} {points.format(old)} -> {points.format(new)}"
    if same_parser:
        because = "the spec changed" if spec_changed else "nothing changed that is known"
        if delta <= -drop:
            return Difference(spec, "worse", f"{shown} ({because}; {parser} is the same)")
        if delta >= drop:
            return Difference(spec, "better", f"{shown} ({because}; {parser} is the same)")
        return None
    if abs(delta) >= drop:
        return Difference(spec, "note", f"{shown}, with {parser} {clean(str(old_version))} -> {clean(str(new_version))}"
                          + (" and a changed spec" if spec_changed else ""))
    return None


def compare(old: dict, new: dict) -> list[Difference]:
    """The differences between two quality documents (see quality.py), worst first."""
    out: list[Difference] = []
    for spec, row in new["specs"].items():
        before = old["specs"].get(spec)
        if before is None:
            out.append(Difference(spec, "note", "new: not in the earlier results"))
            continue
        changed = before["sha256"] != row["sha256"]
        if changed:
            out.append(Difference(spec, "note", f"the spec changed ({before.get('version') or '?'} -> {row.get('version') or '?'})"))
        for name, target in row["targets"].items():
            was = before["targets"].get(name)
            if was is None:
                out.append(Difference(spec, "note", f"{name}: new, accepts {target['accepted']:.0%}" if target["accepted"] is not None else f"{name}: new"))
                continue
            found = _moved(spec, f"{name} accepts", was["accepted"], target["accepted"], ACCEPTED_DROP, _same_parser(was, target),
                           name, was.get("version"), target.get("version"), changed)
            if found:
                out.append(found)
        for name in before["targets"]:
            if name not in row["targets"]:
                out.append(Difference(spec, "note", f"{name}: no longer measured"))
        for name, cov in row["coverage"].items():
            was = before["coverage"].get(name)
            if was is not None:
                found = _moved(spec, f"{name} covers", was["lines"], cov["lines"], COVERAGE_DROP, _same_parser(was, cov),
                               name, was.get("version"), cov.get("version"), changed, points="{:.1%}")
                if found:
                    out.append(found)
        a, b = before.get("per_second"), row.get("per_second")
        if a and b and (b / a >= SPEED_CHANGE or a / b >= SPEED_CHANGE):
            out.append(Difference(spec, "note", f"Fandango makes the files at {a:.0f}/s -> {b:.0f}/s (the machine's as much as the spec's)"))
    out += [Difference(spec, "note", "not in these results any more") for spec in old["specs"] if spec not in new["specs"]]
    order = {"worse": 0, "note": 1, "better": 2}
    return sorted(out, key=lambda d: (order[d.kind], d.spec, d.text))


def as_text(old: dict, differences: list[Difference]) -> str:
    since = ", ".join(x for x in (old.get("made"), f"fanbase {old['fanbase']}" if old.get("fanbase") else None,
                                  f"fandango {old['fandango']}" if old.get("fandango") else None) if x)
    head = f"compared with the earlier results ({since or 'undated'}):"
    if not differences:
        return f"{head} nothing differs\n"
    lines = [head]
    width = max(len(d.spec) for d in differences)
    for d in differences:
        lines.append(clean(f"  {d.kind:<6} {d.spec:<{width}}  {d.text}"))
    return "\n".join(lines) + "\n"


def as_markdown(old: dict, differences: list[Difference]) -> str:
    since = ", ".join(x for x in (old.get("made"), f"fanbase {old['fanbase']}" if old.get("fanbase") else None) if x)
    out = [f"## Compared with the earlier results ({since or 'undated'})", ""]
    if not differences:
        return "\n".join(out + ["Nothing differs.", ""])
    icons = {"worse": "⚠️ worse", "better": "✅ better", "note": "ℹ️"}
    out += ["| | spec | |", "|---|---|---|"]
    out += [f"| {icons[d.kind]} | `{d.spec}` | {clean(d.text)} |" for d in differences]
    return "\n".join(out + [""])
