"""Which registry a spec comes from.

A ref is `png`, `png/png-apng` or `png-apng`, optionally with the name of a registry the
user added in front: `acme:png/png-strict`. Without a name it means the default registry
(the public one, unless --registry or $FANBASE_REGISTRY say otherwise), which is also
`fanbase:`. A plain ref never reaches a registry the user did not name, so installing
`png` can only ever fetch what the default registry's maintainers published.

The user may also pin a plain ref to a spec of an added registry, which then is what that
ref means to them (`fanbase pin png acme:png/png-strict`).
"""

from __future__ import annotations

from pathlib import Path

from fanbase.config import Config, RegistryConfig, load_config
from fanbase.registry import (
    Registry,
    RegistryBase,
    RegistryError,
    split_registry,
)
from fanbase.remote import RemoteRegistry
from fanbase.source import find_registry

def open_configured(reg: RegistryConfig) -> RegistryBase:
    """A registry the user added: a URL, or a path to a local checkout."""
    if reg.url.startswith(("http://", "https://")):
        opened: RegistryBase = RemoteRegistry(reg.url, reg.token())
    elif (Path(reg.url).expanduser() / "specs").is_dir():
        opened = Registry(Path(reg.url).expanduser())
    else:
        raise RegistryError(f"registry {reg.name}: {reg.url} is neither a URL nor a registry checkout")
    opened.name = reg.name
    return opened


class Context:
    """The default registry, the ones the user added, and the pins: opened when needed.

    `default` can be given (a registry that is already open, as tests and `ensure()` do);
    otherwise it is located the usual way, on first use.
    """

    def __init__(
        self,
        default: RegistryBase | None = None,
        explicit: str | None = None,
        config: Config | None = None,
        local: bool = False,
    ) -> None:
        self._default = default
        self._explicit = explicit
        self._local = local
        self.config = config if config is not None else load_config()
        self._named: dict[str, RegistryBase] = {}

    @property
    def default(self) -> RegistryBase:
        if self._default is None:
            self._default = find_registry(self._explicit, local=self._local)
        return self._default

    def registry(self, name: str | None) -> RegistryBase:
        if name is None:
            return self.default
        if name not in self._named:
            if name not in self.config.registries:
                raise RegistryError(
                    f"no registry called {name!r}; add it with `fanbase registry add {name} URL`"
                )
            self._named[name] = open_configured(self.config.registries[name])
        return self._named[name]

    def pinned(self, ref: str) -> str:
        """What the user pinned `ref` to, or `ref` itself."""
        return self.config.pins.get(ref.strip(), ref.strip())

    def resolve(self, ref: str):
        """The registry and the entry a ref means."""
        ref = self.pinned(ref)
        name, rest = split_registry(ref)
        reg = self.registry(name)
        try:
            return reg, reg.resolve(rest)
        except RegistryError as exc:
            if name is None and (elsewhere := self.elsewhere(rest)):
                raise RegistryError(f"{exc}; the registries you added have: {', '.join(elsewhere)}") from None
            raise

    def elsewhere(self, ref: str) -> list[str]:
        """The registries the user added that have a spec by this name: a hint, never a fallback."""
        found = []
        for name in sorted(self.config.registries):
            try:
                found.append(f"{name}:{self.registry(name).resolve(ref)}")
            except RegistryError:
                continue
        return found
