"""What GiveRight observed, kept apart from what people curated.

`data/orgs/*.yaml` is written by hand. Every fact in it carries a source and a
date, and the comments explaining those sources are part of the record. So the
agent never writes to it -- an earlier version did, and a single test run
silently replaced a curated file with a machine dump, comments and all.

Machine-observed facts go here instead, append-only, and are replayed onto the
corpus at load time. Two kinds:

  * `delivery` -- a donor confirmed a drop-off. The one stock fact GiveRight
    knows first-hand.
  * `reply` -- an organisation answered an email or a phone call.

Replay is ordered and idempotent: loading twice gives the same corpus, and an
observation older than the need's `last_verified` is skipped, because the human
who curated that line already knew.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .models import Org

OBSERVATIONS_FILE = Path(__file__).resolve().parents[2] / "data" / "observations.jsonl"


@dataclass(frozen=True)
class Observation:
    observed: str                 # ISO date
    org_id: str
    kind: str                     # delivery | reply
    category: str | None = None
    quantity: int = 0
    reply: str | None = None
    categories: list[str] = field(default_factory=list)

    @property
    def day(self) -> date:
        return date.fromisoformat(self.observed)


class ObservationLog:
    """Append-only. Small enough to read whole, ordered enough to replay."""

    def __init__(self, entries: list[Observation] | None = None, path: Path | None = None):
        self.entries = list(entries or [])
        self.path = path or OBSERVATIONS_FILE

    @classmethod
    def load(cls, path: Path | None = None) -> "ObservationLog":
        path = path or OBSERVATIONS_FILE
        if not path.exists():
            return cls([], path)
        return cls(
            [Observation(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()],
            path,
        )

    def append(self, entry: Observation) -> None:
        self.entries.append(entry)
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(asdict(entry)) + "\n")

    def replay(self, orgs: list[Org]) -> list[Org]:
        """Fold observations onto a freshly loaded corpus, oldest first."""
        by_id = {o.id: o for o in orgs}

        for entry in sorted(self.entries, key=lambda e: e.observed):
            org = by_id.get(entry.org_id)
            if org is None:
                continue
            if entry.kind == "delivery":
                self._replay_delivery(org, entry)
            elif entry.kind == "reply":
                self._replay_reply(org, entry)

        return orgs

    @staticmethod
    def _replay_delivery(org: Org, entry: Observation) -> None:
        need = org.need_for(entry.category or "")
        if need is None:
            return
        # A delivery the curator already accounted for must not be counted
        # twice. Their figure is dated; anything on or before that date is
        # theirs, not ours.
        if need.last_verified and entry.day <= need.last_verified:
            return
        need.record_delivery(entry.quantity)

    @staticmethod
    def _replay_reply(org: Org, entry: Observation) -> None:
        from .outreach import Reply, apply_reply

        try:
            reply = Reply((entry.reply or "").strip().lower())
        except ValueError:
            return
        apply_reply(org, reply, list(entry.categories), today=entry.day)


def record_delivery(
    org_id: str, category: str, quantity: int, *, on: date, log: ObservationLog
) -> Observation:
    entry = Observation(
        observed=on.isoformat(), org_id=org_id, kind="delivery",
        category=category, quantity=max(0, quantity),
    )
    log.append(entry)
    return entry


def record_reply(
    org_id: str, reply: str, categories: list[str], *, on: date, log: ObservationLog
) -> Observation:
    entry = Observation(
        observed=on.isoformat(), org_id=org_id, kind="reply",
        reply=reply, categories=list(categories),
    )
    log.append(entry)
    return entry
