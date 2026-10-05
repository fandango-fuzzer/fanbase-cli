"""Reading a Fanbase registry over HTTP, from its index.yml, without cloning it.

Offers the same interface as `fanbase.registry.Registry`, so the commands don't need to
know which kind of registry they were handed. Listing needs only the one `index.yml`
fetched at construction; reading fetches exactly the one `.fan` file asked for.
"""

from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from fanbase.manifest import INDEX_FILENAME, parse_index
from fanbase.registry import Entry, RegistryBase, RegistryError, RegistryUnavailable

DEFAULT_REF = "main"
TIMEOUT = 30


def _raw_base(url: str) -> str:
    """A github.com repo URL (optionally `/tree/<ref>`) becomes a raw-content base URL.

    Anything else is used as-is: it must already serve raw file bytes at `<url>/<path>`.
    """
    parts = urlsplit(url)
    if parts.netloc != "github.com":
        return url.rstrip("/")
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        raise RegistryError(f"not a repository URL: {url}")
    owner, repo = segments[0], segments[1].removesuffix(".git")
    ref = segments[3] if len(segments) >= 4 and segments[2] == "tree" else DEFAULT_REF
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}"


def _get(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        error = RegistryUnavailable if exc.code >= 500 else RegistryError
        raise error(f"could not fetch {url}: HTTP {exc.code} {exc.reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RegistryUnavailable(f"could not reach {url}: {getattr(exc, 'reason', exc)}") from None


class RemoteRegistry(RegistryBase):
    def __init__(self, url: str) -> None:
        self.base = _raw_base(url)
        try:
            index = _get(f"{self.base}/{INDEX_FILENAME}")
        except RegistryUnavailable:
            raise
        except RegistryError as exc:
            # No index there at all (wrong URL, private repo): not a registry we can use.
            raise RegistryUnavailable(str(exc)) from None
        self._entries = parse_index(index.decode("utf-8"))

    def formats(self) -> list[str]:
        return sorted({e.format for e in self._entries})

    def kinds(self, fmt: str) -> list[Entry]:
        return sorted((e for e in self._entries if e.format == fmt), key=lambda e: e.kind)

    def read(self, entry: Entry) -> bytes:
        raw = _get(f"{self.base}/{entry.path}")
        if entry.sha256 and hashlib.sha256(raw).hexdigest() != entry.sha256:
            raise RegistryError(
                f"{entry} does not match {INDEX_FILENAME}; the registry needs `fanbase reindex`"
            )
        return raw
