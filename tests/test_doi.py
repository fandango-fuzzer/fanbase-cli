"""`fanbase doi`: a DOI for a spec from Zenodo; tried against a Zenodo that is a small server on this machine."""

import http.server
import json
import re
import sys
import threading

import pytest
import yaml
from conftest import build_registry, republish

from fanbase import contrib, doi as doi_module
from fanbase.cli import main

TOKEN = "SECRET-ZENODO-TOKEN-1234"


class FakeZenodo:
    """The calls of Zenodo's deposit API that publishing takes, kept in the order they came, with what they carried."""

    def __init__(self, prefix="10.5281", fail_on=None, other_host=None, redirect_to=None, redirect_get_to=None):
        self.prefix, self.fail_on, self.other_host, self.redirect_to = prefix, fail_on, other_host, redirect_to
        self.redirect_get_to = redirect_get_to
        self.calls: list[tuple[str, str, dict]] = []  # method, path, {auth, body}
        self.files: dict[str, bytes] = {}
        self.metadata: dict = {}
        self.next_id = 111
        self.published: list[int] = []

    def start(self):
        zen = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _read(self):
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _send(self, status, payload=None):
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self):
                body = self._read()
                path = self.path
                zen.calls.append((self.command, path, {"auth": self.headers.get("Authorization"), "body": body}))
                base = f"http://127.0.0.1:{zen.port}"
                if zen.redirect_to and path == "/api/deposit/depositions" and self.command == "POST":
                    self.send_response(307)
                    self.send_header("Location", zen.redirect_to + "/api/deposit/depositions")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if zen.redirect_get_to and self.command == "GET":
                    self.send_response(302)
                    self.send_header("Location", zen.redirect_get_to + path)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if zen.fail_on == "upload" and self.command == "PUT" and path.startswith("/api/files/"):
                    return self._send(400, {"message": "The file is too big for this test"})
                if zen.fail_on == "metadata" and self.command == "PUT" and path.startswith("/api/deposit/depositions/"):
                    return self._send(400, {"message": "Validation error", "errors": [{"field": "creators"}]})
                if zen.fail_on == "publish" and path.endswith("/actions/publish"):
                    return self._send(500, {"message": "boom"})
                bucket_host = zen.other_host or base
                if self.command == "POST" and path == "/api/deposit/depositions":
                    n = zen.next_id
                    return self._send(201, {"id": n, "links": {"bucket": f"{bucket_host}/api/files/bucket-{n}", "html": f"{base}/deposit/{n}"}})
                if self.command == "PUT" and path.startswith("/api/files/"):
                    zen.files[path.rsplit("/", 1)[1]] = body
                    return self._send(201, {})
                if self.command == "PUT" and re.fullmatch(r"/api/deposit/depositions/\d+", path):
                    zen.metadata = json.loads(body)["metadata"]
                    return self._send(200, {})
                if self.command == "POST" and path.endswith("/actions/publish"):
                    n = int(path.split("/")[-3])
                    zen.published.append(n)
                    return self._send(202, {"id": n, "doi": f"{zen.prefix}/zenodo.{n}", "links": {"record_html": f"{base}/records/{n}"}})
                if self.command == "POST" and path.endswith("/actions/newversion"):
                    return self._send(201, {"links": {"latest_draft": f"{base}/api/deposit/depositions/222"}})
                if self.command == "GET" and path == "/api/deposit/depositions/222":
                    return self._send(200, {"id": 222, "links": {"bucket": f"{base}/api/files/bucket-222", "html": f"{base}/deposit/222"}})
                if self.command == "GET" and path == "/api/deposit/depositions/222/files":
                    return self._send(200, [{"id": "old-fan"}, {"id": "old-yml"}])
                if self.command == "DELETE":
                    return self._send(204)
                self._send(404, {"message": f"no {self.command} {path}"})

            do_GET = do_POST = do_PUT = do_DELETE = _handle

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/api"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def seen(self):
        return [(method, path) for method, path, _ in self.calls]


@pytest.fixture
def zenodo():
    server = FakeZenodo().start()
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def quiet_world(monkeypatch):
    monkeypatch.setenv("ZENODO_TOKEN", TOKEN)
    monkeypatch.setenv("ZENODO_SANDBOX_TOKEN", TOKEN)
    monkeypatch.setattr(contrib, "git_user", lambda: "Ada Lovelace")


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def reg(tmp_path):
    return build_registry(tmp_path / "reg", {
        ("png", "png-apng"): dict(version="1.2", description="Animated PNGs", status="stable", license="Apache-2.0",
                                  authors=["Grace Hopper", {"name": "Ada Lovelace", "orcid": "0000-0002-1825-0097"}, "Tim, Berners"]),
        ("png", "png-draft"): dict(version="0.1", description="Not yet", status="draft", authors=["Grace Hopper"]),
        ("png", "png-nobody"): dict(version="1.0", description="Nobody wrote it", status="stable"),
        ("png", "png-unversioned"): dict(description="No version", status="stable", authors=["Grace Hopper"]),
    })


def meta(reg, kind="png-apng"):
    return yaml.safe_load((reg / "specs/png" / kind / "metadata.yml").read_text())


def doi(capsys, reg, zen, *extra, kind="png-apng"):
    return run(capsys, "--registry", str(reg), "doi", kind, "--api", zen.url, *extra)


# --- the plan

def test_by_default_it_only_says_what_it_would_do(capsys, reg, zenodo):
    code, out, err = doi(capsys, reg, zenodo)
    assert code == 0 and zenodo.calls == []
    assert "png/png-apng version 1.2 would be a record on zenodo.org:" in out
    assert "creators: Hopper, Grace; Lovelace, Ada; Tim, Berners" in out  # (Family, Given)
    assert "license:  apache-2.0" in out and "png-apng.fan (" in out and "metadata.yml (" in out
    assert "nothing was sent" in out


# --- the sandbox

def test_the_sandbox_tries_it_and_writes_nothing(capsys, reg, zenodo):
    before = (reg / "specs/png/png-apng/metadata.yml").read_bytes()
    code, out, err = doi(capsys, reg, zenodo, "--sandbox")
    assert code == 0 and "published: DOI 10.5281/zenodo.111" in out and "this was the sandbox" in out
    assert (reg / "specs/png/png-apng/metadata.yml").read_bytes() == before and "doi" not in meta(reg)
    assert zenodo.seen() == [
        ("POST", "/api/deposit/depositions"), ("PUT", "/api/files/bucket-111/png-apng.fan"),
        ("PUT", "/api/files/bucket-111/metadata.yml"), ("PUT", "/api/deposit/depositions/111"),
        ("POST", "/api/deposit/depositions/111/actions/publish")]


def test_what_is_sent_is_the_spec_as_it_is(capsys, reg, zenodo):
    doi(capsys, reg, zenodo, "--sandbox")
    assert zenodo.files["png-apng.fan"] == (reg / "specs/png/png-apng/png-apng.fan").read_bytes()
    assert zenodo.files["metadata.yml"] == (reg / "specs/png/png-apng/metadata.yml").read_bytes()
    m = zenodo.metadata
    assert m["version"] == "1.2" and m["upload_type"] == "software" and m["access_right"] == "open" and m["license"] == "apache-2.0"
    assert m["creators"][1] == {"name": "Lovelace, Ada", "orcid": "0000-0002-1825-0097"}
    assert "png/png-apng" in m["title"] and "contains no input that triggers a bug" in m["description"]
    assert {"relation": "isPartOf", "identifier": "https://github.com/fandango-fuzzer/fanbase", "scheme": "url"} in m["related_identifiers"]


def test_the_token_goes_in_the_authorization_and_nowhere_else(capsys, reg, zenodo):
    code, out, err = doi(capsys, reg, zenodo, "--sandbox")
    assert all(c[2]["auth"] == f"Bearer {TOKEN}" for c in zenodo.calls) and TOKEN not in out + err
    assert not any(TOKEN.encode() in c[2]["body"] for c in zenodo.calls) and all(TOKEN not in path for _, path, _ in zenodo.calls)


# --- production

def test_production_publishes_and_writes_the_doi(capsys, reg, zenodo):
    code, out, err = doi(capsys, reg, zenodo, "--production", "--yes")
    assert code == 0 and "published: DOI 10.5281/zenodo.111" in out
    assert meta(reg)["doi"] == "10.5281/zenodo.111" and "wrote doi: 10.5281/zenodo.111 into specs/png/png-apng/metadata.yml" in out
    assert run(capsys, "--registry", str(reg), "reindex", "--check")[0] == 0  # the registry is in order with it
    _, cite, _ = run(capsys, "--registry", str(reg), "cite", "png-apng", "--bibtex")
    assert "doi     = {10.5281/zenodo.111}" in cite


def test_the_changed_metadata_does_not_make_the_spec_a_changed_one(capsys, reg, zenodo, tmp_path):
    import shutil

    shutil.copytree(reg, tmp_path / "before")
    doi(capsys, reg, zenodo, "--production", "--yes")
    code, out, _ = run(capsys, "--registry", str(reg), "changes", "--base", str(tmp_path / "before"))
    assert "no changes" in out  # (a DOI is not a new version of the grammar)


def test_production_asks_and_not_being_able_to_ask_is_a_no(capsys, reg, zenodo, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    code, out, _ = doi(capsys, reg, zenodo, "--production")
    assert code == 1 and "not published" in out and zenodo.calls == [] and "doi" not in meta(reg)


def test_production_at_a_terminal_asks_and_yes_publishes(capsys, reg, zenodo, monkeypatch):
    asked = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda q: asked.append(q) or "y")
    assert doi(capsys, reg, zenodo, "--production")[0] == 0
    assert len(asked) == 1 and "permanent" in asked[0] and meta(reg)["doi"] == "10.5281/zenodo.111"


# --- a new version

def test_a_new_version_replaces_the_files_and_gets_its_own_doi(capsys, reg, zenodo):
    republish(reg, "png-apng", version="1.3", doi="10.5281/zenodo.111")
    code, out, _ = doi(capsys, reg, zenodo, "--production", "--yes", "--new-version")
    assert code == 0 and meta(reg)["doi"] == "10.5281/zenodo.222"
    assert zenodo.seen() == [
        ("POST", "/api/deposit/depositions/111/actions/newversion"), ("GET", "/api/deposit/depositions/222"),
        ("GET", "/api/deposit/depositions/222/files"), ("DELETE", "/api/deposit/depositions/222/files/old-fan"),
        ("DELETE", "/api/deposit/depositions/222/files/old-yml"), ("PUT", "/api/files/bucket-222/png-apng.fan"),
        ("PUT", "/api/files/bucket-222/metadata.yml"), ("PUT", "/api/deposit/depositions/222"),
        ("POST", "/api/deposit/depositions/222/actions/publish")]
    assert zenodo.metadata["version"] == "1.3"


# --- what is refused

@pytest.mark.parametrize("kind, setup, complaint", [
    ("png-draft", None, "is a draft, and a DOI is for good"),
    ("png-nobody", None, "has no authors"),
    ("png-unversioned", None, "has no version"),
    ("png-apng", lambda r: republish(r, "png-apng", doi="10.5281/zenodo.5"), "has the DOI 10.5281/zenodo.5 already"),
])
def test_what_should_not_get_a_doi(capsys, reg, zenodo, kind, setup, complaint):
    if setup:
        setup(reg)
    code, _, err = doi(capsys, reg, zenodo, "--sandbox", kind=kind)
    assert code == 2 and complaint in err and zenodo.calls == []


def test_a_draft_can_be_done_if_asked(capsys, reg, zenodo):
    code, out, _ = doi(capsys, reg, zenodo, "--sandbox", "--allow-draft", kind="png-draft")
    assert code == 0 and "published: DOI" in out


def test_a_stale_registry_is_refused(capsys, reg, zenodo):
    (reg / "specs/png/png-apng/png-apng.fan").write_text("import brotli\n<start> ::= 'x'\n")  # not reindexed
    code, _, err = doi(capsys, reg, zenodo, "--sandbox")
    assert code == 2 and "its metadata.yml is out of date" in err and zenodo.calls == []


def test_a_new_version_needs_a_zenodo_doi_to_be_a_version_of(capsys, reg, zenodo):
    code, _, err = doi(capsys, reg, zenodo, "--sandbox", "--new-version")
    assert code == 2 and "needs a Zenodo DOI" in err and "has none" in err
    republish(reg, "png-apng", doi="10.1234/elsewhere.5")
    code, _, err = doi(capsys, reg, zenodo, "--sandbox", "--new-version")
    assert code == 2 and "which is not one" in err


def test_both_modes_are_refused(capsys, reg, zenodo):
    assert doi(capsys, reg, zenodo, "--sandbox", "--production")[0] == 2


def test_no_token_says_which_variable(capsys, reg, zenodo, monkeypatch):
    monkeypatch.delenv("ZENODO_SANDBOX_TOKEN")
    code, _, err = doi(capsys, reg, zenodo, "--sandbox")
    assert code == 2 and "set ZENODO_SANDBOX_TOKEN" in err and zenodo.calls == []


def test_the_token_is_for_https_or_this_machine(capsys, reg):
    code, _, err = run(capsys, "--registry", str(reg), "doi", "png-apng", "--sandbox", "--api", "http://zenodo.example.org/api")
    assert code == 2 and "over https, or to this machine" in err


# --- when it goes wrong

@pytest.mark.parametrize("fail_on, complaint", [("upload", "Zenodo said 400"), ("metadata", "Validation error"), ("publish", "Zenodo said 500")])
def test_a_draft_that_cannot_be_finished_is_deleted(capsys, reg, fail_on, complaint):
    zen = FakeZenodo(fail_on=fail_on).start()
    try:
        code, _, err = doi(capsys, reg, zen, "--production", "--yes")
        assert code == 2 and complaint in err and TOKEN not in err
        assert ("DELETE", "/api/deposit/depositions/111") in zen.seen() and "doi" not in meta(reg)
    finally:
        zen.stop()


def test_a_draft_that_cannot_be_deleted_is_said(capsys, reg, monkeypatch):
    zen = FakeZenodo(fail_on="metadata").start()
    real = doi_module.Zenodo.call

    def call(self, method, *args, **kwargs):
        if method == "DELETE":
            raise doi_module.ZenodoError("no")
        return real(self, method, *args, **kwargs)

    monkeypatch.setattr(doi_module.Zenodo, "call", call)
    try:
        code, out, err = doi(capsys, reg, zen, "--production", "--yes")
        assert code == 2 and "a draft is left on Zenodo" in out and "/deposit/111" in out
    finally:
        zen.stop()


def test_zenodo_pointing_at_another_host_does_not_get_the_token(capsys, reg):
    elsewhere = FakeZenodo().start()
    zen = FakeZenodo(other_host=f"http://localhost:{elsewhere.port}").start()  # (a different host name for the same machine)
    try:
        code, _, err = doi(capsys, reg, zen, "--production", "--yes")
        assert code == 2 and "another host than its own" in err and elsewhere.calls == []
    finally:
        zen.stop()
        elsewhere.stop()


def test_a_redirect_to_another_host_does_not_take_the_token(capsys, reg):
    elsewhere = FakeZenodo().start()
    zen = FakeZenodo(redirect_to=f"http://localhost:{elsewhere.port}").start()
    try:
        code, _, err = doi(capsys, reg, zen, "--production", "--yes")  # (a POST that is redirected is not followed at all)
        assert code == 2 and all(c[2]["auth"] is None for c in elsewhere.calls) and TOKEN not in err
    finally:
        zen.stop()
        elsewhere.stop()


def test_a_redirect_that_is_followed_goes_without_the_token():
    elsewhere = FakeZenodo().start()
    zen = FakeZenodo(redirect_get_to=f"http://localhost:{elsewhere.port}").start()
    try:
        doi_module.Zenodo(zen.url, TOKEN).call("GET", f"{zen.url}/deposit/depositions/222", expect=(200, 404))
    except doi_module.ZenodoError:
        pass
    try:
        assert zen.calls[0][2]["auth"] == f"Bearer {TOKEN}"  # the host asked has it
        assert elsewhere.calls and all(c[2]["auth"] is None for c in elsewhere.calls)  # the host it was sent on to does not
    finally:
        zen.stop()
        elsewhere.stop()


def test_a_zenodo_that_does_not_say_the_doi(capsys, reg, zenodo, monkeypatch):
    monkeypatch.setattr(zenodo, "prefix", "not-a-prefix")
    code, _, err = doi(capsys, reg, zenodo, "--production", "--yes")
    assert code == 2 and "did not say its DOI" in err and "doi" not in meta(reg)


def test_names():
    assert doi_module._family_first("Grace Hopper") == "Hopper, Grace"
    assert doi_module._family_first("Ada Byron King Lovelace") == "Lovelace, Ada Byron King"
    assert doi_module._family_first("Hopper, Grace") == "Hopper, Grace" and doi_module._family_first("Plato") == "Plato"
