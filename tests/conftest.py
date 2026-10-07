import functools
import http.server
import subprocess
import threading
from pathlib import Path

import pytest

from fanbase.manifest import INDEX_FILENAME, dump_index, reindex
from fanbase.registry import Registry


def write_spec(root: Path, fmt: str, kind: str, text: str, description: str = "", **meta) -> None:
    folder = root / "specs" / fmt / kind
    folder.mkdir(parents=True)
    (folder / f"{kind}.fan").write_text(text, encoding="utf-8")
    if description or meta:
        import yaml

        (folder / "metadata.yml").write_text(
            yaml.safe_dump({"description": description, **meta}), encoding="utf-8"
        )


@pytest.fixture
def registry(tmp_path) -> Path:
    """A small registry checkout with an index.yml."""
    root = tmp_path / "registry"
    write_spec(root, "png", "png", "<start> ::= 'png'\n", "plain png", extensions=["png"])
    write_spec(root, "png", "png-apng", "<start> ::= 'apng'\n", "animated png", extensions=["png"])
    write_spec(root, "gif", "gif", "<start> ::= 'gif'\n", "plain gif", extensions=["gif"])
    reg = Registry(root)
    rows, _, _ = reindex(reg)
    (root / INDEX_FILENAME).write_text(dump_index(rows), encoding="utf-8")
    return root


@pytest.fixture
def acme(tmp_path) -> Path:
    """A registry of someone else's: it calls itself acme, and has a spec the default one lacks."""
    import yaml

    root = tmp_path / "acme-registry"
    write_spec(root, "png", "png-strict", "<start> ::= 'strict'\n", "strict png", extensions=["png"])
    write_spec(root, "png", "png", "<start> ::= 'acme png'\n", "acme's own png", extensions=["png"])
    write_spec(root, "bmp", "bmp", "<start> ::= 'bmp'\n", "plain bmp", extensions=["bmp"])
    (root / "registry.yml").write_text(yaml.safe_dump({"name": "acme", "description": "Acme"}))
    reg = Registry(root)
    rows, _, _ = reindex(reg)
    (root / INDEX_FILENAME).write_text(dump_index(rows, reg.info), encoding="utf-8")
    return root


@pytest.fixture
def served(registry):
    """The same registry, served over HTTP; yields its URL."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(registry))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def pip_calls(monkeypatch):
    """No test runs a real pip: installs are recorded, and succeed."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("fanbase.manager.subprocess.run", fake_run)
    return calls


@pytest.fixture(autouse=True)
def own_config(tmp_path_factory, monkeypatch):
    """No test reads or writes the config file of whoever runs the tests."""
    monkeypatch.setenv("FANBASE_CONFIG", str(tmp_path_factory.mktemp("config") / "config.yml"))
