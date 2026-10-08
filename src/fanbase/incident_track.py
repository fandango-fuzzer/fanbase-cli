"""`fanbase incidents list|show|track|verify`: what to do with a private record once it is open.

An opened record (a folder: `fanbase incidents open`, or what `evaluate --incidents DIR` writes without a key) says what
broke. The person who found it has to tell the vendor, in private, and wait. These keep that straight:

  - `list` the incidents of a record, with where each stands;
  - `show` the note to the vendor of one;
  - `track` what has been done about it: who it was reported to, when, a reference, and when it was fixed;
  - `verify` that it still breaks the parser, by running the kept input again with the parser as it is now.

The clock is the usual one (OSS-Fuzz, Project Zero): a bug may be made public 90 days after it was reported, or 30 days
after it was fixed if that comes first. It is a reminder of that, not a rule fanbase enforces; fanbase never makes anything public.
What is tracked is in `tracking.yml` in the record's folder, which only the person who can read the record can read.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
from pathlib import Path

import yaml

from fanbase.context import Context
from fanbase.output import clean
from fanbase.registry import RegistryError

TRACKING = "tracking.yml"
DAYS_TO_PUBLIC = 90
DAYS_AFTER_FIX = 30
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")


def today() -> datetime.date:
    return datetime.date.today()


def load_record(folder: Path) -> dict:
    """The manifest of an opened record."""
    path = folder / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RegistryError(f"{folder} is not an opened record: there is no usable manifest.json (`fanbase incidents open` makes one)") from None
    if not (isinstance(manifest, dict) and isinstance(manifest.get("incidents"), list)):
        raise RegistryError(f"{path} is not the manifest of a record")
    for incident in manifest["incidents"]:
        if not (isinstance(incident, dict) and isinstance(incident.get("id"), str) and _ID.fullmatch(incident["id"])):
            raise RegistryError(f"{path} names an incident that has no usable id")
    return manifest


def load_tracking(folder: Path) -> dict:
    path = folder / TRACKING
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        raise RegistryError(f"{path} could not be read") from None
    found = data.get("incidents") if isinstance(data, dict) else None
    if not isinstance(found, dict):
        raise RegistryError(f"{path} is not a tracking file")
    return found


def save_tracking(folder: Path, tracking: dict) -> None:
    path = folder / TRACKING
    text = "# what has been done about each incident of this record; `fanbase incidents track` writes it\n" + \
        yaml.safe_dump({"incidents": tracking}, sort_keys=True, allow_unicode=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tracking.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def pick(manifest: dict, wanted: str) -> dict:
    """The incident whose id is `wanted`, or the only one that starts with it."""
    exact = [i for i in manifest["incidents"] if i["id"] == wanted]
    found = exact or [i for i in manifest["incidents"] if i["id"].startswith(wanted)]
    if not found:
        raise RegistryError(f"no incident called {clean(wanted)!r} in this record (`fanbase incidents list` has them)")
    if len(found) > 1:
        raise RegistryError(f"{clean(wanted)!r} could be any of: {', '.join(i['id'] for i in found)}")
    return found[0]


def _date(value: object, what: str) -> datetime.date | None:
    if value is None:
        return None
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        raise RegistryError(f"{what}: {clean(str(value))!r} is not a date like 2026-10-08") from None


def parse_date(text: str, what: str) -> datetime.date:
    if text == "today":
        return today()
    parsed = _date(text, what)
    assert parsed is not None
    if parsed > today():
        raise RegistryError(f"{what}: {parsed} is in the future")
    return parsed


def clock(track: dict | None, now: datetime.date | None = None) -> tuple[str, datetime.date | None]:
    """Where an incident stands with the 90-day clock, in words, and the date from which it may be public."""
    now = now or today()
    if not track or not track.get("reported"):
        return "not reported yet", None
    reported = _date(track["reported"], "reported")
    assert reported is not None
    public = reported + datetime.timedelta(days=DAYS_TO_PUBLIC)
    fixed = _date(track.get("fixed_on"), "fixed_on")
    if fixed is not None:
        public = min(public, fixed + datetime.timedelta(days=DAYS_AFTER_FIX))
    left = (public - now).days
    state = f"reported {reported}" + (f", fixed {fixed}" if fixed else "")
    state += f"; may be public from {public}" + (f" ({left} days)" if left > 0 else (" (that is today)" if left == 0 else f" ({-left} days ago)"))
    return state, public


# --- the commands

def cmd_incidents_list(args, ctx: Context) -> int:
    folder = Path(args.folder)
    manifest, tracking = load_record(folder), load_tracking(folder)
    found = manifest["incidents"]
    if not found:
        print("nothing broke in this record")
        return 0
    width = max(len(i["id"]) for i in found)
    for incident in found:
        status, _ = clock(tracking.get(incident["id"]))
        seen = f"{incident.get('occurrences', '?')}x" + (f", {incident['confirmed']} again" if incident.get("confirmed") is not None else "")
        print(clean(f"  {incident['id']:<{width}}  {incident.get('kind', '?')} of {incident.get('target', '?')} {incident.get('target_version') or ''} "
                    f"on {incident.get('spec', '?')} ({seen}): {status}"))
    if manifest.get("not_itemised"):
        print(f"({manifest['not_itemised']} further occurrence(s) of other causes were counted and not written down)")
    return 0


def cmd_incidents_show(args, ctx: Context) -> int:
    folder = Path(args.folder)
    incident = pick(load_record(folder), args.id)
    report = folder / incident["id"] / "REPORT.md"
    try:
        text = report.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise RegistryError(f"{report} is not there") from None
    print(clean(text), end="")
    track = load_tracking(folder).get(incident["id"])
    if track:
        print(f"\n## Tracked\n\n{yaml.safe_dump(track, sort_keys=True, allow_unicode=True)}{clock(track)[0]}")
    return 0


def cmd_incidents_track(args, ctx: Context) -> int:
    folder = Path(args.folder)
    incident = pick(load_record(folder), args.id)
    tracking = load_tracking(folder)
    track = dict(tracking.get(incident["id"]) or {})
    changes = {
        "vendor": args.vendor, "reference": args.reference, "note": args.note, "fixed_in": args.fixed_in,
        "reported": parse_date(args.reported, "--reported") if args.reported else None,
        "fixed_on": parse_date(args.fixed_on, "--fixed-on") if args.fixed_on else None,
    }
    if all(value is None for value in changes.values()):
        raise RegistryError("say what happened: --reported DATE, --vendor NAME, --reference TEXT, --fixed-in VERSION --fixed-on DATE, --note TEXT")
    for key, value in changes.items():
        if value is not None:
            track[key] = value.isoformat() if isinstance(value, datetime.date) else clean(str(value))[:300]
    if track.get("fixed_on") and not track.get("reported"):
        raise RegistryError("it was fixed, but has it been reported? give --reported DATE as well")
    if track.get("reported") and track.get("fixed_on") and _date(track["fixed_on"], "fixed_on") < _date(track["reported"], "reported"):  # type: ignore[operator]
        raise RegistryError("it cannot have been fixed before it was reported")
    tracking[incident["id"]] = track
    save_tracking(folder, tracking)
    print(f"{incident['id']}: {clock(track)[0]}")
    return 0


def cmd_incidents_verify(args, ctx: Context) -> int:
    """Run what the record kept against the parser as it is now."""
    from fanbase.contrib import checkout
    from fanbase.targets import HANG_KIND, TargetError, judge, load_target, unavailable, version_of

    folder = Path(args.folder)
    incident = pick(load_record(folder), args.id)
    reg = checkout(ctx)
    try:
        target = load_target(reg.root, incident["target"])
    except TargetError as exc:
        raise RegistryError(f"{incident['id']}: {exc}") from None
    if why := unavailable(target):
        raise RegistryError(f"{target.name} cannot run here: {why}")
    inputs = [i for i in incident.get("inputs", []) if i.get("file")]
    if not inputs:
        raise RegistryError(f"{incident['id']} kept no input that can be run again (it was too large, or is not in this record)")
    now, then = version_of(target), incident.get("target_version")
    print(clean(f"{incident['id']}: {incident.get('kind')} of {target.name}, {then or 'a version that was not recorded'}; now {now or 'a version that is not known'}"))
    still = fine = 0
    for item in inputs:
        path = (folder / item["file"]).resolve()
        if folder.resolve() not in path.parents or not path.is_file():
            print(f"  {item['file']}: not there")
            continue
        verdict = judge(target, path, timeout=args.timeout, memory_mb=args.memory)
        if verdict.incident:
            still += 1
            how = f"killed by {verdict_signal(verdict)}" if verdict.incident != HANG_KIND else f"no answer in {args.timeout} seconds"
            print(clean(f"  {item['file']}: still {verdict.incident}: {how}"))
        else:
            fine += 1
            print(f"  {item['file']}: no longer breaks it ({verdict.category})")
    if still:
        print("it is not fixed in this version")
        return 1
    if fine:
        print(f"it no longer breaks {target.name}" + (f" in {now}" if now else "")
              + f": if the vendor fixed it, `fanbase incidents track {args.folder} {incident['id']} --fixed-in VERSION --fixed-on DATE` starts the 30 days")
    return 0


def verdict_signal(verdict) -> str:
    import signal

    if verdict.returncode is not None and verdict.returncode < 0:
        try:
            return signal.Signals(-verdict.returncode).name
        except ValueError:
            return str(-verdict.returncode)
    return "a signal"
