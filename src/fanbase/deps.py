"""Specs that build on other specs.

A spec says what it builds on with `extends` in its metadata.yml:

    extends:
      - png                  # a spec of the same registry
      - png>=1.0,<2          # ... in a version this range allows
      - fanbase:png          # a spec of the default registry (from another registry)

and uses it with `include("png/png.fan")` (or `include("acme/png/png.fan")` for a spec of
the registry acme), redefining what it wants to change. Installing the spec installs what
it extends, so the include finds its file.

A registry's specs can extend specs of the same registry. The default registry's can
extend nothing else, so a release of it stands on its own. Any other registry's can also
extend the default registry's, as `fanbase:...`; it cannot name a third registry, because
the name a user gives a registry is the user's own.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from fanbase.registry import (
    DEFAULT_REGISTRY_NAME,
    Entry,
    RegistryBase,
    RegistryError,
    split_prefix,
)

_OPERATORS = "@<>=!~"


class DependencyError(RegistryError):
    """A spec's `extends` cannot be followed."""


@dataclass(frozen=True)
class Dependency:
    registry: str  # the registry it is looked up in: "" is the default one
    ref: str  # as written, without a registry or a version range
    specifier: SpecifierSet
    text: str  # as written, for messages

    def __str__(self) -> str:
        return self.text


def split_constraint(text: str) -> tuple[str, SpecifierSet]:
    """`png>=1.0,<2` -> (png, >=1.0,<2); `png@1.2` -> (png, ==1.2); `png` -> (png, any)."""
    text = text.strip()
    cut = next((i for i, c in enumerate(text) if c in _OPERATORS), len(text))
    ref, rest = text[:cut].strip(), text[cut:].strip()
    if rest.startswith("@"):
        rest = "==" + rest[1:].strip()
    try:
        return ref, SpecifierSet(rest)
    except InvalidSpecifier:
        raise RegistryError(f"cannot read the version range in {text!r}") from None


def self_names(reg: RegistryBase) -> set[str]:
    """The names that mean this registry when a spec of it writes `name:spec`: the name it
    was added under, the name it gives itself, and `fanbase` for the default registry."""
    names = {reg.name or DEFAULT_REGISTRY_NAME}
    if declared := reg.info.get("name"):
        names.add(declared)
    return names


def identity(reg: RegistryBase) -> str:
    """The name a registry goes by in the text of its specs: the name it gives itself, else the
    name it was added under. "" for the public registry, which has neither."""
    return reg.info.get("name") or reg.name


def parse_extends(item: object, owner: str, names: Iterable[str] | None = None) -> Dependency:
    """One entry of `extends`, for a spec of the registry called `owner` ("" is the default).

    `names` are the prefixes that mean that same registry (default: just its name).
    """
    if not isinstance(item, str) or not item.strip():
        raise DependencyError(f"extends: {item!r} is not a spec name")
    ref_text, specifier = split_constraint(item)
    prefix, ref = split_prefix(ref_text)
    if not ref:
        raise DependencyError(f"extends: {item!r} names no spec")
    if prefix is None or prefix in (set(names) if names is not None else {owner or DEFAULT_REGISTRY_NAME}):
        registry = owner
    elif prefix == DEFAULT_REGISTRY_NAME:
        registry = ""
    elif not owner:
        raise DependencyError(
            f"extends: {item!r}: the default registry's specs can only extend its own"
        )
    else:
        raise DependencyError(
            f"extends: {item!r}: a registry's specs can extend its own and the default "
            f"registry's ({DEFAULT_REGISTRY_NAME}:...), not another registry's"
        )
    return Dependency(registry, ref, specifier, item.strip())


def dependencies(entry: Entry, owner: str, names: Iterable[str] | None = None) -> list[Dependency]:
    return [parse_extends(item, owner, names) for item in entry.meta.get("extends") or []]


def label(reg: RegistryBase, entry: Entry) -> str:
    return f"{reg.name}:{entry}" if reg.name else str(entry)


def check_version(name: str, meta: dict, specifier: SpecifierSet, wanted: str, who: str = "") -> None:
    """Raise unless the version in `meta` (the metadata of the spec `name`) is one the range
    allows. `wanted` is what asked for it, as written; `who` says who asked."""
    if not specifier:
        return
    prefix = f"{who}: " if who else ""
    version = meta.get("version")
    try:
        have = Version(version) if isinstance(version, str) else None
    except InvalidVersion:
        have = None
    if have is None:
        raise DependencyError(
            f"{prefix}{wanted} needs {name} in {specifier}, but it has "
            + ("no version" if version is None else f"the version {version!r}, which is not a version number")
        )
    if not specifier.contains(have, prereleases=True):
        raise DependencyError(
            f"{prefix}{wanted} needs {name} in {specifier}, but it is at {have}; "
            "an older release of the registry (a tag, as in --registry URL/tree/<tag>) may have it"
        )


def closure(ctx, reg: RegistryBase, entry: Entry) -> list[tuple[RegistryBase, Entry]]:
    """The spec and everything it extends, what it extends first, each once.

    `ctx` opens the registries that are named (see `fanbase.context.Context`).
    """
    order: list[tuple[RegistryBase, Entry]] = []
    done: set[tuple[str, str, str]] = set()
    path: list[tuple[str, str, str]] = []

    def visit(reg: RegistryBase, entry: Entry) -> None:
        key = (reg.name, entry.format, entry.kind)
        if key in path:
            cycle = " -> ".join(f"{r}:{f}/{k}" if r else f"{f}/{k}" for r, f, k in [*path[path.index(key):], key])
            raise DependencyError(f"specs extend each other in a circle: {cycle}")
        if key in done:
            return
        path.append(key)
        who = label(reg, entry)
        for dep in dependencies(entry, reg.name, self_names(reg)):
            dep_reg = ctx.registry(dep.registry or None)
            try:
                dep_entry = dep_reg.resolve(dep.ref)
            except RegistryError as exc:
                raise DependencyError(f"{who} extends {dep}: {exc}") from None
            check_version(str(dep_entry), dep_entry.meta, dep.specifier, dep.text, who)
            visit(dep_reg, dep_entry)
        path.pop()
        done.add(key)
        order.append((reg, entry))

    visit(reg, entry)
    return order


def dependents(ctx, reg: RegistryBase, target: Entry) -> list[tuple[RegistryBase, Entry]]:
    """The specs of the registries `ctx` can open that extend `target` directly.

    A registry is searched when it is the one `target` is in (its specs extend by name) and
    when it is another one that may extend the default registry's (`fanbase:`).
    """
    found = []
    registries = [reg]
    if not reg.name:
        for name in sorted(ctx.config.registries):
            try:
                registries.append(ctx.registry(name))
            except RegistryError:
                continue  # one that cannot be reached has no say here
    for other in registries:
        for fmt in other.formats():
            for entry in other.kinds(fmt):
                try:
                    deps = dependencies(entry, other.name, self_names(other))
                except RegistryError:
                    continue
                for dep in deps:
                    if dep.registry != reg.name:
                        continue
                    try:
                        if ctx.registry(dep.registry or None).resolve(dep.ref) == target:
                            found.append((other, entry))
                            break
                    except RegistryError:
                        continue
    return found


def check_registry_dependencies(reg: RegistryBase, problems: list[str]) -> None:
    """Add to `problems` what is wrong with the `extends` of the specs of a registry that is
    being indexed: names that do not exist, versions that do not fit, circles."""
    entries = [e for fmt in reg.formats() for e in reg.kinds(fmt)]
    # A registry that names itself is not the default one, whatever it is opened as here.
    owner = reg.info.get("name", "")
    names = {owner or DEFAULT_REGISTRY_NAME}
    graph: dict[str, list[str]] = {}
    for entry in entries:
        edges = []
        try:
            deps = dependencies(entry, owner, names)
        except RegistryError as exc:
            problems.append(f"{entry}: {exc}")
            continue
        for dep in deps:
            if dep.registry != owner:
                continue  # another registry's spec: not something this registry can look up
            try:
                target = reg.resolve(dep.ref)
                check_version(str(target), target.meta, dep.specifier, dep.text, str(entry))
            except RegistryError as exc:
                problems.append(f"{entry}: extends {dep}: {exc}")
                continue
            if target == entry:
                problems.append(f"{entry}: extends itself")
            else:
                edges.append(str(target))
        graph[str(entry)] = edges

    state: dict[str, int] = {}  # 1: being followed, 2: done
    trail: list[str] = []

    def follow(node: str) -> None:
        state[node] = 1
        trail.append(node)
        for nxt in graph.get(node, []):
            if state.get(nxt) == 1:
                circle = [*trail[trail.index(nxt):], nxt]
                problems.append("specs extend each other in a circle: " + " -> ".join(circle))
            elif nxt not in state:
                follow(nxt)
        trail.pop()
        state[node] = 2

    for start in graph:
        if start not in state:
            follow(start)
