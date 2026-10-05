"""Reading a Fanbase registry from disk.

Layout:

    specs/<format>/<kind>/<kind>.fan
    specs/<format>/<kind>/metadata.yml
    index.yml

The default kind of a format is the one named after the format itself, so
`specs/png/png/png.fan` is what plain `png` resolves to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

METADATA_FILENAME = "metadata.yml"

# png/png-animated · png  (the kind is optional; it defaults to the one named after the format)
# A bare kind name (png-animated) is resolved by `RegistryBase.resolve`.
_REF = re.compile(r"^(?P<format>[^/]+)(?:/(?P<kind>[^/]+))?$")


class RegistryError(Exception):
    pass


class RegistryUnavailable(RegistryError):
    """The registry could not be reached (network down, host unknown, ...)."""


@dataclass(frozen=True)
class Entry:
    """One spec in the registry, addressed as `<format>/<kind>`."""

    format: str
    kind: str
    path: str  # posix path to the .fan file, relative to the registry root
    meta: dict = field(default_factory=dict, compare=False)
    sha256: str = ""  # only known when the entry came from index.yml

    def __str__(self) -> str:
        return f"{self.format}/{self.kind}"

    @property
    def requires(self) -> list[str]:
        return list(self.meta.get("requires") or [])


class RegistryBase:
    """Reference resolution, shared by local and remote registries."""

    def formats(self) -> list[str]:
        raise NotImplementedError

    def kinds(self, fmt: str) -> list[Entry]:
        raise NotImplementedError

    def read(self, entry: Entry) -> bytes:
        raise NotImplementedError

    def resolve(self, ref: str) -> Entry:
        """`png`, `png/png-apng`, or just `png-apng` (a kind name, if it is unique)."""
        m = _REF.match(ref.strip())
        if not m:
            raise RegistryError(f"cannot parse reference: {ref!r}")
        fmt, kind = m.group("format"), m.group("kind")

        formats = self.formats()
        if kind is None and fmt not in formats:
            matches = [e for f in formats for e in self.kinds(f) if e.kind == fmt]
            if len(matches) == 1:
                return matches[0]
            raise RegistryError(f"unknown format or spec: {fmt}")
        if fmt not in formats:
            raise RegistryError(f"unknown format: {fmt}")
        entries = {e.kind: e for e in self.kinds(fmt)}

        if kind:
            if kind not in entries:
                raise RegistryError(f"unknown spec: {fmt}/{kind}")
            return entries[kind]
        if fmt in entries:
            return entries[fmt]
        if len(entries) == 1:
            return next(iter(entries.values()))
        raise RegistryError(f"{fmt} has no default spec; name one of: " + ", ".join(sorted(entries)))


class Registry(RegistryBase):
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.specs_dir = self.root / "specs"
        if not self.specs_dir.is_dir():
            raise RegistryError(f"no specs/ directory under {self.root}")

    def formats(self) -> list[str]:
        return sorted(p.name for p in self.specs_dir.iterdir() if p.is_dir() and self._kind_dirs(p))

    def kinds(self, fmt: str) -> list[Entry]:
        base = self.specs_dir / fmt
        if not base.is_dir():
            return []
        return [self.entry(fmt, d.name) for d in self._kind_dirs(base)]

    def entry(self, fmt: str, kind: str) -> Entry:
        folder = self.specs_dir / fmt / kind
        path = (folder / f"{kind}.fan").relative_to(self.root).as_posix()
        return Entry(fmt, kind, path, read_metadata(folder / METADATA_FILENAME))

    def read(self, entry: Entry) -> bytes:
        return (self.root / entry.path).read_bytes()

    @staticmethod
    def _kind_dirs(fmt_dir: Path) -> list[Path]:
        """Kind folders: the ones holding a `<kind>.fan` next to their metadata."""
        return sorted(d for d in fmt_dir.iterdir() if (d / f"{d.name}.fan").is_file())


def read_metadata(path: Path) -> dict:
    """A kind's metadata.yml; empty if it has none yet (`fanbase reindex` creates it)."""
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RegistryError(f"{path}: invalid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise RegistryError(f"{path}: expected a mapping at the top level")
    return data
