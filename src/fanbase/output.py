"""What the commands print: text that is safe to show, and JSON for scripts."""

from __future__ import annotations

import json
import re

# Anything a registry tells us may end up on the terminal. Control characters could be
# escape sequences that rewrite what is on screen, so they are shown as `?`. Tabs and line
# breaks stay.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(text: object) -> str:
    return _CONTROL.sub("?", str(text))


def dump_json(data: object) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)
