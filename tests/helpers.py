"""Fakes and builders shared by the tests of evaluate and of the private record."""

import subprocess
from pathlib import Path

import yaml

from fanbase.cli import main

# The suite replaces subprocess.run for every test (see conftest); this is the real one.
REAL_RUN = subprocess.run

ACCEPT = "import sys\nsys.exit(0)\n"
STRICT = (  # rejects what starts with "bad", saying so
    "import sys, pathlib\n"
    "if pathlib.Path(sys.argv[1]).read_bytes().startswith(b'bad'):\n"
    "    print('invalid header in', sys.argv[1], file=sys.stderr); sys.exit(1)\n"
)


class FakeFandango:
    """Stands in for `fandango fuzz`: makes the inputs it is told to, where it is told to.

    The inputs are a running sequence, different from each other, whatever the number of runs.
    """

    def __init__(self, content=None, limit=None, returncode=0, make=True, delay=0.0, overrun_on=None):
        # by default every fourth input is bad
        self.content = content or (lambda i: (b"bad input " if i % 4 == 0 else b"good input ") + str(i).encode())
        self.limit, self.returncode, self.make, self.delay, self.overrun_on = limit, returncode, make, delay, overrun_on
        self.calls = []
        self.made = 0

    def __call__(self, cmd, **kwargs):
        if cmd[0] != "/fake/fandango":
            return REAL_RUN(cmd, **kwargs)
        import pathlib
        import time

        n = int(cmd[cmd.index("-n") + 1])
        out = pathlib.Path(cmd[cmd.index("-d") + 1])
        self.calls.append({"cmd": cmd, "env": kwargs["env"], "n": n, "timeout": kwargs.get("timeout")})
        if self.overrun_on is not None and len(self.calls) - 1 == self.overrun_on:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        if self.make:
            room = n if self.limit is None else max(0, min(n, self.limit - self.made))
            if room or self.limit is None:
                out.mkdir(parents=True)
            for _ in range(room):
                time.sleep(self.delay)
                (out / f"fandango-{self.made:04d}.bin").write_bytes(self.content(self.made))
                self.made += 1
        return subprocess.CompletedProcess(cmd, self.returncode, "", "could not find enough" if self.returncode else "")


def target(reg, name, harness, formats=("png",), **config):
    folder = reg / "targets" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "harness.py").write_text(harness)
    (folder / "target.yml").write_text(yaml.safe_dump({
        "title": name.title(), "formats": list(formats), "run": ["{python}", "{dir}/harness.py", "{file}"], **config}))


def settle(reg):
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["--registry", str(reg), "reindex"]) == 0




class FakeAge:
    """Stands in for `age`: writes what it is asked to encrypt behind a header, and reads it back."""

    HEADER = b"FAKE-AGE\n"

    def __init__(self, fail=False):
        self.fail, self.plaintexts, self.calls = fail, [], []

    def __call__(self, cmd, **kwargs):
        if cmd[0] != "age":
            return REAL_RUN(cmd, **kwargs)
        self.calls.append(cmd)
        if self.fail:
            return subprocess.CompletedProcess(cmd, 1, b"", b"age: no such recipient")
        if "-d" in cmd:
            data = Path(cmd[-1]).read_bytes()
            if not data.startswith(self.HEADER):
                return subprocess.CompletedProcess(cmd, 1, b"", b"age: error: failed to read header")
            return subprocess.CompletedProcess(cmd, 0, data[len(self.HEADER):], b"")
        data = kwargs.get("input", b"")
        self.plaintexts.append(data)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(self.HEADER + data)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
