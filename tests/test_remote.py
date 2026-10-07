import http.server
import threading

import pytest
import yaml

from fanbase.manifest import dump_index
from fanbase.registry import RegistryError, RegistryUnavailable
from fanbase.remote import RemoteRegistry, _get

INDEX = dump_index([{
    "format": "png", "kind": "png", "path": "specs/png/png/png.fan",
    "sha256": "0" * 64, "description": "x",
}])


class Recorder(http.server.BaseHTTPRequestHandler):
    """Serves INDEX at /index.yml, records every request's Authorization header, and
    redirects /to-other to `redirect_to`."""

    seen: list
    redirect_to: str = ""

    def do_GET(self):
        self.seen.append((self.path, self.headers.get("Authorization")))
        if self.path == "/to-other":
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.end_headers()
            return
        body = INDEX.encode() if self.path.endswith("index.yml") else b"x"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a, **k):
        pass


def serve():
    handler = type("Handler", (Recorder,), {"seen": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, handler, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def servers():
    made = [serve(), serve()]
    yield made
    for server, _, _ in made:
        server.shutdown()
        server.server_close()


def test_no_token_means_no_authorization_header(servers):
    _, handler, url = servers[0]
    RemoteRegistry(url)
    assert handler.seen == [("/index.yml", None)]


def test_a_token_is_sent_to_the_registry(servers):
    _, handler, url = servers[0]
    reg = RemoteRegistry(url, token="secret")
    assert handler.seen == [("/index.yml", "token secret")]
    with pytest.raises(RegistryError, match="does not match"):
        reg.read(reg.resolve("png"))  # the sha is wrong on purpose: the request is what matters
    assert ("/specs/png/png/png.fan", "token secret") in handler.seen


def test_a_token_does_not_follow_a_redirect_to_another_host(servers):
    (_, first, url_first), (_, second, url_second) = servers
    first.redirect_to = f"{url_second}/index.yml"
    # A different port is a different host as far as the token is concerned.
    assert _get(f"{url_first}/to-other", token="secret") == INDEX.encode()
    assert first.seen == [("/to-other", "token secret")]
    assert second.seen == [("/index.yml", None)]


def test_a_token_stays_on_a_redirect_within_the_host(servers):
    (_, first, url_first), _ = servers
    first.redirect_to = f"{url_first}/index.yml"
    _get(f"{url_first}/to-other", token="secret")
    assert first.seen[-1] == ("/index.yml", "token secret")


@pytest.mark.parametrize("url", ["http://example.org/x", "http://10.0.0.5/x"])
def test_a_token_is_not_sent_over_plain_http_to_another_machine(url):
    with pytest.raises(RegistryError, match="plain http"):
        _get(url, token="secret")


def test_an_unreachable_registry_is_unavailable():
    with pytest.raises(RegistryUnavailable):
        RemoteRegistry("http://127.0.0.1:9", token="secret")


def test_the_registrys_own_details_are_read_from_the_index(servers, monkeypatch):
    _, _, url = servers[0]
    monkeypatch.setattr(
        "fanbase.remote.parse_index_full",
        lambda text: ([], {"name": "acme", "description": "Acme's grammars"}),
    )
    assert RemoteRegistry(url).info["name"] == "acme"
