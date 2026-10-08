"""Reading a Fanbase registry over HTTP, from its index.yml, without cloning it.

Offers the same interface as `fanbase.registry.Registry`, so the commands don't need to
know which kind of registry they were handed. Listing needs only the one `index.yml`
fetched at construction; reading fetches exactly the one `.fan` file asked for.

A private registry takes a token, which is sent to the registry's own host and nowhere
else: never over plain http (except to this machine), and never along a redirect to another
host.
"""

from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from fanbase.manifest import INDEX_FILENAME, parse_index_full
from fanbase.registry import Entry, RegistryBase, RegistryError, RegistryUnavailable
from fanbase.signing import SIGNATURE_FILENAME, Key, SigningError, Verified, verify

DEFAULT_REF = "main"
TIMEOUT = 30

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


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


class _KeepTokenOnThisHost(urllib.request.HTTPRedirectHandler):
    """Follow a redirect, but do not take the token to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlsplit(newurl).netloc != urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


_OPENER = urllib.request.build_opener(_KeepTokenOnThisHost)


def _get(url: str, token: str | None = None) -> bytes:
    request = urllib.request.Request(url)
    if token:
        parts = urlsplit(url)
        if parts.scheme != "https" and parts.hostname not in _LOCAL_HOSTS:
            raise RegistryError(f"refusing to send a token over plain http to {parts.netloc}")
        request.add_header("Authorization", f"token {token}")
    try:
        with _OPENER.open(request, timeout=TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        error = RegistryUnavailable if exc.code >= 500 else RegistryError
        raise error(f"could not fetch {url}: HTTP {exc.code} {exc.reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RegistryUnavailable(f"could not reach {url}: {getattr(exc, 'reason', exc)}") from None


class RemoteRegistry(RegistryBase):
    def __init__(self, url: str, token: str | None = None, signers: tuple[Key, ...] | list[Key] = ()) -> None:
        self.url = url
        self.base = _raw_base(url)
        self._token = token
        self.signed_by: Verified | None = None
        try:
            index = _get(f"{self.base}/{INDEX_FILENAME}", token)
        except RegistryUnavailable:
            raise
        except RegistryError as exc:
            # No index there at all (wrong URL, private repo): not a registry we can use.
            raise RegistryUnavailable(str(exc)) from None
        if signers:  # keys the user trusts: the index has to be signed by one of them, or nothing in it is believed
            try:
                signature = _get(f"{self.base}/{SIGNATURE_FILENAME}", token)
            except RegistryUnavailable:
                raise
            except RegistryError:
                raise SigningError(f"{url} is not signed ({SIGNATURE_FILENAME} is not there), and you trust it only if it is") from None
            self.signed_by = verify(index, signature, signers)
        self._entries, self.info = parse_index_full(index.decode("utf-8"))

    def formats(self) -> list[str]:
        return sorted({e.format for e in self._entries})

    def kinds(self, fmt: str) -> list[Entry]:
        return sorted((e for e in self._entries if e.format == fmt), key=lambda e: e.kind)

    def read(self, entry: Entry) -> bytes:
        raw = _get(f"{self.base}/{entry.path}", self._token)
        if entry.sha256 and hashlib.sha256(raw).hexdigest() != entry.sha256:
            raise RegistryError(
                f"{entry} does not match {INDEX_FILENAME}; the registry needs `fanbase reindex`"
            )
        return raw
