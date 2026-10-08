"""`fanbase doi SPEC`: a DOI for one spec, from Zenodo, written into its metadata.yml.

A DOI makes a spec citable as it was: the record keeps its file and its metadata, and the DOI always means those.
Zenodo's records are permanent, and cannot be taken back or changed once published, so this is opt-in and careful:

  - `fanbase doi SPEC` only says what would be sent, and sends nothing;
  - `--sandbox` does it on sandbox.zenodo.org, which is for trying, and whose DOIs mean nothing: it never writes
    anything into the registry;
  - `--production` publishes on zenodo.org, after asking, and writes the DOI into `doi:` of the spec's metadata.yml
    for you to commit. The token comes from the environment (ZENODO_TOKEN, ZENODO_SANDBOX_TOKEN) and goes to
    Zenodo's own host, over https, and is never shown.

A spec in draft, a registry that is out of date, a spec that has a DOI already (unless `--new-version`), and a
spec without authors are refused. What goes into the record is the spec's `.fan` and `metadata.yml`, which are a
grammar and its description: the registry's ethics (no bug-triggering inputs) are met by what a spec is.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import yaml

from fanbase.context import Context
from fanbase.contrib import checkout, refresh
from fanbase.discover import is_group
from fanbase.manifest import reindex
from fanbase.output import clean
from fanbase.registry import Entry, Registry, RegistryError
from fanbase.remote import _OPENER, TIMEOUT

PRODUCTION = ("https://zenodo.org/api", "ZENODO_TOKEN")
SANDBOX = ("https://sandbox.zenodo.org/api", "ZENODO_SANDBOX_TOKEN")
_LOCAL = ("localhost", "127.0.0.1", "[::1]")
_ZENODO_DOI = re.compile(r"10\.\d{4,9}/zenodo\.(\d+)")


class ZenodoError(RegistryError):
    """Zenodo refused something, or could not be reached."""


@dataclass
class Plan:
    entry: Entry
    metadata: dict  # what Zenodo is told (its `metadata`)
    files: list[tuple[str, bytes]]
    previous: str | None = None  # the DOI this is a new version of
    notes: list[str] = field(default_factory=list)


def _family_first(name: str) -> str:
    """Zenodo wants "Family, Given". A name that has a comma is taken to be that already."""
    name = " ".join(name.split())
    if "," in name or " " not in name:
        return name
    given, _, family = name.rpartition(" ")
    return f"{family}, {given}"


def _creators(authors: object) -> list[dict]:
    out = []
    for author in authors if isinstance(authors, list) else []:
        name = author.get("name") if isinstance(author, dict) else author
        if not (isinstance(name, str) and name.strip()):
            continue
        creator = {"name": " ".join(name.split()) if is_group(name) else _family_first(name)}
        if isinstance(author, dict) and isinstance(author.get("orcid"), str):
            creator["orcid"] = author["orcid"]
        if isinstance(author, dict) and isinstance(author.get("affiliation"), str):
            creator["affiliation"] = author["affiliation"]
        out.append(creator)
    return out


def build_plan(reg: Registry, entry: Entry, *, new_version: bool, allow_draft: bool) -> Plan:
    """What would be published for this spec; raises if it should not be."""
    meta = entry.meta
    rows, changed, _ = reindex(reg, write=False)
    if entry in changed:
        raise RegistryError(f"{entry}: its metadata.yml is out of date; run `fanbase reindex` (the record keeps the file as it is)")
    if meta.get("status") == "draft" and not allow_draft:
        raise RegistryError(f"{entry} is a draft, and a DOI is for good: give it status: stable first (or --allow-draft)")
    version = meta.get("version")
    if version is None:
        raise RegistryError(f"{entry} has no version: a record is of one version of a spec")
    creators = _creators(meta.get("authors"))
    if not creators:
        raise RegistryError(f"{entry} has no authors: a Zenodo record needs creators (`authors:` in its metadata.yml)")
    existing = meta.get("doi")
    if existing and not new_version:
        raise RegistryError(f"{entry} has the DOI {existing} already; a new version of it is --new-version")
    if new_version and not (isinstance(existing, str) and _ZENODO_DOI.fullmatch(existing)):
        raise RegistryError(f"--new-version needs a Zenodo DOI in `doi:` to be a new version of; {entry} has "
                            + (f"{existing}, which is not one" if existing else "none"))

    title = meta.get("title") or meta.get("description") or entry.kind
    description = f"<p>{_html(str(meta.get('description') or title))}</p><p>The Fandango input specification <code>{_html(str(entry))}</code>, "\
                  f"version {_html(str(version))}, of the <a href=\"https://github.com/fandango-fuzzer/fanbase\">Fanbase registry</a>. "\
                  "It is a grammar; it contains no input that triggers a bug.</p>"
    record: dict = {
        "upload_type": "software", "title": f"{title} ({entry}, version {version})"[:250], "description": description,
        "creators": creators, "access_right": "open", "version": str(version),
        "keywords": ["fandango", "grammar", "input generation", "fuzzing", str(entry.format)],
        "related_identifiers": [{"relation": "isPartOf", "identifier": "https://github.com/fandango-fuzzer/fanbase", "scheme": "url"}],
    }
    notes = []
    if isinstance(meta.get("license"), str) and meta["license"].strip():
        record["license"] = meta["license"].strip().lower()
    else:
        notes.append("no license in metadata.yml: the record is open access with no license named")
    if isinstance(meta.get("source"), str) and meta["source"].startswith(("http://", "https://")):
        record["related_identifiers"].append({"relation": "isDerivedFrom", "identifier": meta["source"], "scheme": "url"})
    files = [(f"{entry.kind}.fan", reg.read(entry)),
             ("metadata.yml", (reg.root / entry.path).with_name("metadata.yml").read_bytes())]
    return Plan(entry, record, files, previous=existing if new_version else None, notes=notes)


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Zenodo:
    """The few calls of Zenodo's deposit API that publishing a record takes."""

    def __init__(self, api: str, token: str) -> None:
        parts = urlsplit(api)
        if parts.scheme != "https" and parts.hostname not in _LOCAL:
            raise RegistryError("the token is sent to Zenodo over https, or to this machine: not to " + clean(api))
        self.api, self._token, self.host = api.rstrip("/"), token, parts.netloc

    def call(self, method: str, url: str, body: object = None, raw: bytes | None = None, expect: tuple[int, ...] = (200, 201, 202)):
        if urlsplit(url).netloc != self.host:
            raise ZenodoError("Zenodo pointed at another host than its own; not following it with the token")
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
        try:
            with _OPENER.open(request, timeout=TIMEOUT * 4) as response:
                payload = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            said = exc.read(2000).decode("utf-8", errors="replace")
            try:
                said = json.loads(said).get("message") or said
            except (ValueError, AttributeError):
                pass
            raise ZenodoError(f"Zenodo said {exc.code} to {method} {urlsplit(url).path}: {clean(str(said))[:300]}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ZenodoError(f"could not reach Zenodo: {clean(str(getattr(exc, 'reason', exc)))}") from None
        if status not in expect:
            raise ZenodoError(f"Zenodo answered {status} to {method} {urlsplit(url).path}")
        if not payload:
            return {}
        try:
            return json.loads(payload)
        except ValueError:
            raise ZenodoError("Zenodo's answer was not JSON") from None

    def publish(self, plan: Plan, say=print) -> dict:
        """Make the record. A draft that cannot be finished is deleted, so that nothing half-made is left."""
        if plan.previous:
            record_id = _ZENODO_DOI.fullmatch(plan.previous).group(1)  # type: ignore[union-attr]
            draft = self.call("POST", f"{self.api}/deposit/depositions/{record_id}/actions/newversion")
            latest = draft.get("links", {}).get("latest_draft")
            if not isinstance(latest, str):
                raise ZenodoError("Zenodo did not say where the new version is")
            deposition = self.call("GET", latest)
        else:
            deposition = self.call("POST", f"{self.api}/deposit/depositions", body={})
        deposition_url = f"{self.api}/deposit/depositions/{deposition['id']}"
        try:
            if plan.previous:  # the new version starts with the old files: they are replaced
                for old in self.call("GET", f"{deposition_url}/files"):
                    self.call("DELETE", f"{deposition_url}/files/{old['id']}", expect=(200, 204))
            bucket = deposition["links"]["bucket"]
            for name, data in plan.files:
                self.call("PUT", f"{bucket}/{name}", raw=data)
                say(f"  uploaded {name} ({len(data)} bytes)")
            self.call("PUT", deposition_url, body={"metadata": plan.metadata})
            return self.call("POST", f"{deposition_url}/actions/publish")
        except BaseException:
            try:
                self.call("DELETE", deposition_url, expect=(200, 201, 204))
            except RegistryError:
                say(f"  a draft is left on Zenodo: {deposition.get('links', {}).get('html', deposition_url)} (delete it there)")
            raise


def _confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        return False
    return input(question).strip().lower() in ("y", "yes")


def write_doi(reg: Registry, entry: Entry, doi: str) -> None:
    meta_file = (reg.root / entry.path).with_name("metadata.yml")
    data = yaml.safe_load(meta_file.read_text(encoding="utf-8")) or {}
    data["doi"] = doi
    meta_file.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n")
    refresh(reg)


def cmd_doi(args, ctx: Context) -> int:
    reg = checkout(ctx)
    entry = reg.resolve(args.ref)
    if args.sandbox and args.production:
        raise RegistryError("--sandbox or --production, not both")
    plan = build_plan(reg, entry, new_version=args.new_version, allow_draft=args.allow_draft)

    where = "sandbox.zenodo.org (for trying: its DOIs mean nothing)" if args.sandbox else "zenodo.org"
    print(f"{entry} version {plan.metadata['version']} would be {'a new version of ' + plan.previous if plan.previous else 'a record'} on {where}:")
    print(clean(f"  title:    {plan.metadata['title']}"))
    print(clean(f"  creators: {'; '.join(c['name'] for c in plan.metadata['creators'])}"))
    print(clean(f"  license:  {plan.metadata.get('license') or '-'}"))
    print(f"  files:    {', '.join(f'{name} ({len(data)} bytes)' for name, data in plan.files)}")
    for note in plan.notes:
        print(clean(f"  note: {note}"))
    if not (args.sandbox or args.production):
        print("nothing was sent: --sandbox tries it on sandbox.zenodo.org, --production publishes on zenodo.org")
        return 0

    api, token_name = SANDBOX if args.sandbox else PRODUCTION
    api = args.api or api
    import os

    token = os.environ.get(token_name, "").strip()
    if not token:
        raise RegistryError(f"set {token_name} to a Zenodo access token with the deposit scopes (zenodo.org/account/settings/applications)")
    if args.production and not _confirm(
            "A Zenodo record is permanent: it cannot be taken back or changed. Publish it? [y/N] ", args.yes):
        print("not published")
        return 1
    result = Zenodo(api, token).publish(plan)
    doi = result.get("doi") or (result.get("metadata") or {}).get("doi")
    if not (isinstance(doi, str) and re.fullmatch(r"10\.\d{4,9}/\S+", doi)):
        raise ZenodoError("Zenodo published it but did not say its DOI; look for it in your uploads on Zenodo")
    link = (result.get("links") or {}).get("record_html") or (result.get("links") or {}).get("html") or ""
    print(clean(f"published: DOI {doi} {link}".rstrip()))
    if args.sandbox:
        print("this was the sandbox: the DOI is not real and nothing was written into the registry")
        return 0
    write_doi(reg, entry, doi)
    print(f"wrote doi: {doi} into {(reg.root / entry.path).with_name('metadata.yml').relative_to(reg.root).as_posix()}: commit it")
    return 0
