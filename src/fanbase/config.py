"""The user's own settings: the registries they added, and what they pinned.

    registries:
      acme:
        url: https://github.com/acme/fuzz-specs     # or a path to a local checkout
        token_env: ACME_TOKEN                        # optional: the variable holding a token
    pins:
      png: acme:png/png-strict                       # what a plain `png` means to this user

A registry is in the file because the user chose to trust it: its specs are Python code
that runs inside Fandango. The public registry is never listed; it is always there.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from fanbase.registry import DEFAULT_REGISTRY_NAME, REGISTRY_NAME, RegistryError

ENV_CONFIG = "FANBASE_CONFIG"

_ENV_VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class RegistryConfig:
    name: str
    url: str
    token_env: str | None = None

    def token(self) -> str | None:
        """The token, if the registry names a variable and the variable is set."""
        return (os.environ.get(self.token_env) or None) if self.token_env else None


@dataclass
class Config:
    registries: dict[str, RegistryConfig] = field(default_factory=dict)
    pins: dict[str, str] = field(default_factory=dict)


def config_path() -> Path:
    if explicit := os.environ.get(ENV_CONFIG):
        return Path(explicit)
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "fanbase" / "config.yml"


def check_registry_name(name: str) -> str:
    if name == DEFAULT_REGISTRY_NAME:
        raise RegistryError(f"{name!r} is the name of the default registry; pick another")
    if not REGISTRY_NAME.fullmatch(name):
        raise RegistryError(
            f"{name!r} is not a usable registry name: lower case letters, digits and dashes, "
            "starting with a letter, at most 32 characters"
        )
    return name


def check_token_env(name: object) -> str:
    if not (isinstance(name, str) and _ENV_VAR.fullmatch(name)):
        raise RegistryError("token_env has to be the name of an environment variable")
    return name


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.is_file():
        return Config()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RegistryError(f"{path}: invalid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise RegistryError(f"{path}: expected a mapping at the top level")

    registries, pins = data.get("registries") or {}, data.get("pins") or {}
    for key, value in (("registries", registries), ("pins", pins)):
        if not isinstance(value, dict):
            raise RegistryError(f"{path}: {key} has to be a mapping")

    config = Config()
    for name, item in registries.items():
        where = f"{path}: registry {name!r}"
        try:
            check_registry_name(str(name))
        except RegistryError as exc:
            raise RegistryError(f"{path}: {exc}") from None
        if not isinstance(item, dict) or not isinstance(item.get("url"), str) or not item["url"]:
            raise RegistryError(f"{where} needs a url")
        token_env = item.get("token_env")
        if token_env is not None:
            try:
                check_token_env(token_env)
            except RegistryError as exc:
                raise RegistryError(f"{where}: {exc}") from None
        config.registries[str(name)] = RegistryConfig(str(name), item["url"], token_env)
    for ref, target in pins.items():
        if not isinstance(ref, str) or not isinstance(target, str) or ":" not in target:
            raise RegistryError(f"{path}: pin {ref!r} has to point at a spec of an added registry (name:spec)")
        config.pins[ref] = target
    return config


def save_config(config: Config, path: Path | None = None) -> Path:
    path = path or config_path()
    data: dict = {}
    if config.registries:
        data["registries"] = {
            reg.name: {k: v for k, v in (("url", reg.url), ("token_env", reg.token_env)) if v}
            for reg in sorted(config.registries.values(), key=lambda r: r.name)
        }
    if config.pins:
        data["pins"] = dict(sorted(config.pins.items()))
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
