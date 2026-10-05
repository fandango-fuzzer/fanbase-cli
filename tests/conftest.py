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
