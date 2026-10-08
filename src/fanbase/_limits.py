"""Run a command with limits: `python -m fanbase._limits MEGABYTES CPU_SECONDS COMMAND...`.

Used by `fanbase evaluate` to start the parsers it judges files with. A parser given a file made
to look odd may want all the memory there is, or loop, or crash; this makes that the parser's
problem and not the machine's. Core dumps are switched off: a dump of a crashed parser holds the
input that crashed it, and that is not to be left lying around.

The limits are set, and then the command takes this process's place, so what is measured and
killed is the command itself. Where `resource` does not exist (Windows) nothing is limited here.
"""

from __future__ import annotations

import os
import sys
from types import ModuleType


def main(argv: list[str]) -> int:
    megabytes, cpu_seconds, command = int(argv[0]), int(argv[1]), argv[2:]
    resource: ModuleType | None
    try:
        import resource as resource_module

        resource = resource_module
    except ImportError:  # pragma: no cover  (Windows)
        resource = None
    if resource is not None:
        for name, limit in (
            ("RLIMIT_CORE", 0),
            ("RLIMIT_AS", megabytes * 1024 * 1024 if megabytes else None),
            ("RLIMIT_CPU", cpu_seconds if cpu_seconds else None),
        ):
            if limit is None or not hasattr(resource, name):
                continue
            try:
                resource.setrlimit(getattr(resource, name), (limit, limit))
            except (ValueError, OSError):
                pass  # some systems refuse to lower one of them (macOS and RLIMIT_AS); the others still hold
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        print(f"cannot run {command[0]}: {exc.strerror or exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
