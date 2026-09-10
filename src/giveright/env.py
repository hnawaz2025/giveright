"""Reading `.env`, without a dependency and without surprises.

A Bedrock API key is a bearer token that botocore picks up from
`AWS_BEARER_TOKEN_BEDROCK`, and the natural place to keep one is a `.env` file
that is already gitignored. Python does not read those by itself.

Two rules, both about not surprising anyone:

  * **The real environment always wins.** A value already exported is never
    overwritten by the file, so `AWS_BEARER_TOKEN_BEDROCK=... python -m ...`
    behaves the way anyone would expect, and CI is never quietly overridden by
    a developer's file.
  * **Nothing is echoed.** The loader reports how many names it set, never
    which values.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def load(path: Path | None = None, *, override: bool = False) -> list[str]:
    """Set variables from a `.env` file. Returns the names set, not the values."""
    path = path or ENV_FILE
    if not path.exists():
        return []

    applied = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        value = value.strip()

        # Quotes are stripped so a pasted key with them still works, but only
        # matching ones -- a value that genuinely starts with a quote is rare
        # and mangling it silently would be worse than leaving it.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        if not name or (name in os.environ and not override):
            continue

        os.environ[name] = value
        applied.append(name)

    return applied
