"""The part that runs when nobody is watching.

When no organisation will take an item, GiveRight tells the donor it will hold
it and keep looking. That sentence was a promise the code did not keep: items
went into HELD and nothing ever looked again.

This is the looking. It is deliberately the least interactive module in the
project -- it wakes up, re-checks every held item against a corpus that has
moved since, and in the ordinary case does nothing at all and says nothing at
all. Two things are worth a donor's attention and only two:

  * something they gave up on now has a home;
  * a hold has run out, and the answer is the reuse-or-recycle chain instead.

A sweep that finds neither produces no notification. An agent that reports "I
checked and there was nothing" every morning is a notification the donor turns
off, and then the one that mattered is off too.

Holds outlive the process that created them, so they live in an append-only
log next to the observations, replayed on load.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .fallback import Pathways, resolve
from .geo import miles
from .matching import candidates
from .models import Condition, Item, ItemState, Org
from .state import HOLD_EXPIRY_DAYS, transition
from .text import label

HELD_FILE = Path(__file__).resolve().parents[2] / "data" / "held.jsonl"


@dataclass(frozen=True)
class Hold:
    """One item waiting for the neighbourhood to change its mind.

    Carries the donor's radius and origin because a hold is only meaningful
    against the promise that was made -- re-matching it against a wider radius
    later would be offering something the donor never agreed to.
    """

    item_id: str
    category: str
    quantity: int
    lat: float
    lng: float
    radius_km: float
    held_since: str                       # ISO date
    condition: str | None = None
    description: str = ""
    donor: str = ""                       # opaque handle, not an identity
    released: bool = False                # a closing record, appended later

    @property
    def since(self) -> date:
        return date.fromisoformat(self.held_since)

    def as_item(self) -> Item:
        return Item(
            id=self.item_id,
            category=self.category,
            description=self.description,
            quantity=self.quantity,
            condition=Condition[self.condition.upper()] if self.condition else None,
            state=ItemState.HELD,
            held_since=self.since,
        )

    def days(self, today: date | None = None) -> int:
        return ((today or date.today()) - self.since).days


class HoldLog:
    """Append-only, like the observation log. A hold is closed by appending a
    release, never by rewriting history -- so "what did we promise, and when"
    stays answerable."""

    def __init__(self, entries: list[Hold] | None = None, path: Path | None = None):
        self.entries = list(entries or [])
        self.path = path or HELD_FILE

    @classmethod
    def load(cls, path: Path | None = None) -> "HoldLog":
        path = path or HELD_FILE
        if not path.exists():
            return cls([], path)
        return cls(
            [Hold(**json.loads(l)) for l in path.read_text().splitlines() if l.strip()],
            path,
        )

    def _append(self, hold: Hold) -> None:
        self.entries.append(hold)
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(asdict(hold)) + "\n")

    def hold(self, hold: Hold) -> None:
        self._append(hold)

    def release(self, item_id: str) -> None:
        open_hold = self.open().get(item_id)
        if open_hold is not None:
            self._append(Hold(**{**asdict(open_hold), "released": True}))

    def open(self) -> dict[str, Hold]:
        """Holds still waiting, oldest record wins for the facts, newest for
        whether it is closed."""
        state: dict[str, Hold] = {}
        for entry in self.entries:
            if entry.released:
                state.pop(entry.item_id, None)
            else:
                state.setdefault(entry.item_id, entry)
        return state


@dataclass(frozen=True)
class Notification:
    """Something a donor should actually be told."""

    item_id: str
    kind: str                 # "rematched" | "hold_expired"
    headline: str
    detail: str = ""
    org_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "headline": self.headline,
            "detail": self.detail,
            "org_id": self.org_id,
        }


@dataclass
class Sweep:
    """What one pass over the held items found. `quiet` is the normal case."""

    notifications: list[Notification] = field(default_factory=list)
    still_waiting: int = 0
    checked: int = 0

    @property
    def quiet(self) -> bool:
        return not self.notifications

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "still_waiting": self.still_waiting,
            "notifications": [n.as_dict() for n in self.notifications],
            "quiet": self.quiet,
        }


def sweep(
    log: HoldLog,
    orgs: list[Org],
    pathways: Pathways,
    *,
    today: date | None = None,
) -> Sweep:
    """Re-check every open hold against the corpus as it stands today.

    Pure with respect to the corpus: it reads organisations, it never edits
    them. It does close holds, because a hold that has been answered is no
    longer waiting.
    """
    today = today or date.today()
    result = Sweep()

    for hold in list(log.open().values()):
        result.checked += 1
        item = hold.as_item()
        origin = (hold.lat, hold.lng)

        best = next(
            (
                e
                for e in candidates(item, orgs, origin, hold.radius_km, today=today)
                if e.usable
            ),
            None,
        )

        if best is not None:
            org = next(o for o in orgs if o.id == best.org_id)
            transition(item, ItemState.REMATCHED, org_id=org.id,
                       note="a new need appeared while it was held", today=today)
            log.release(hold.item_id)
            result.notifications.append(
                Notification(
                    item_id=hold.item_id,
                    kind="rematched",
                    org_id=org.id,
                    headline=(
                        f"Your {label(hold.category, hold.quantity)} has somewhere "
                        f"to go after all."
                    ),
                    detail=(
                        f"{best.reason} You have been holding it "
                        f"{hold.days(today)} days. {org.hours}"
                    ),
                )
            )
            continue

        if hold.days(today) >= HOLD_EXPIRY_DAYS:
            resolution = resolve(item, pathways, today=today, hold_exhausted=True)
            transition(item, resolution.next_state, note=resolution.headline, today=today)
            log.release(hold.item_id)
            result.notifications.append(
                Notification(
                    item_id=hold.item_id,
                    kind="hold_expired",
                    headline=resolution.headline,
                    detail=(
                        f"Nothing within {miles(hold.radius_km):.0f} mi needed it in "
                        f"{HOLD_EXPIRY_DAYS} days. {resolution.detail}"
                    ),
                    org_id=(resolution.destination.name if resolution.destination else None),
                )
            )
            continue

        result.still_waiting += 1

    return result


def hold_from(item: Item, origin: tuple[float, float], radius_km: float,
              *, today: date | None = None, donor: str = "") -> Hold:
    """Record a held item so a later sweep can honour the promise."""
    return Hold(
        item_id=item.id,
        category=item.category,
        quantity=item.quantity,
        lat=origin[0],
        lng=origin[1],
        radius_km=radius_km,
        held_since=(item.held_since or today or date.today()).isoformat(),
        condition=item.condition.name.lower() if item.condition else None,
        description=item.description,
        donor=donor,
    )


def main(argv: list[str] | None = None) -> int:
    """One sweep, for cron.

        */30 * * * *  cd /srv/giveright && .venv/bin/python -m giveright.watch

    Exit code 0 whether or not anything was found -- a quiet sweep is the
    normal, healthy outcome, not a failure. Prints only what a donor would be
    told, so an empty run is genuinely silent and cron stays quiet with it.
    """
    import argparse

    from .corpus import load_orgs
    from .observations import ObservationLog

    parser = argparse.ArgumentParser(description="Re-check every held item.")
    parser.add_argument("--today", default=None, help="ISO date, for reproducible runs")
    parser.add_argument("--verbose", action="store_true",
                        help="also report the holds that are still waiting")
    args = parser.parse_args(argv)

    today = date.fromisoformat(args.today) if args.today else date.today()
    log = HoldLog.load()
    orgs = ObservationLog.load().replay(load_orgs())

    found = sweep(log, orgs, Pathways.load(), today=today)

    for note in found.notifications:
        print(f"{note.headline}\n  {note.detail}\n")

    if args.verbose:
        print(f"checked {found.checked}, still waiting {found.still_waiting}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
