"""No Dead Ends: what happens to an item nobody will take.

Most donation tools stop at "no match found", which is exactly the moment the
item goes in a bin. GiveRight keeps going, in descending order of what is
actually recovered:

    hold and keep watching  ->  named reuse organisation  ->  material
    recycling  ->  disposal instructions

Only the first two count towards the recovery rate. Recycling is reported
separately and disposal is reported as a failure, because a metric that counts
"here is how to bin it" as a save cannot be wrong and so means nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from .models import DeclineReason, Item, ItemState
from .text import label as _label
from .state import HOLD_EXPIRY_DAYS

PATHWAYS_FILE = Path(__file__).resolve().parents[2] / "data" / "pathways.yaml"

# Items that must never be passed to another person, whatever an org's rules
# say. Safety outranks recovery.
UNSAFE_TO_REUSE = {
    "car_seats": "expired or crash-involved car seats cannot be passed on",
    "cribs": "cribs recalled or of unknown age cannot be passed on",
    "helmets": "a helmet that has taken an impact cannot be passed on",
}


@dataclass(frozen=True)
class Destination:
    name: str
    note: str = ""
    url: str | None = None


@dataclass(frozen=True)
class Resolution:
    """One terminal answer for one item: where it goes and what to do."""

    item_id: str
    next_state: ItemState
    headline: str
    detail: str = ""
    destination: Destination | None = None
    counts_as_recovery: bool = False

    def as_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "outcome": self.next_state.value,
            "headline": self.headline,
            "detail": self.detail,
            "destination": (
                {
                    "name": self.destination.name,
                    "note": self.destination.note,
                    "url": self.destination.url,
                }
                if self.destination
                else None
            ),
            "counts_as_recovery": self.counts_as_recovery,
        }


class Pathways:
    """The reuse/recycle/dispose table, loaded from data, never from a model."""

    def __init__(self, raw: dict):
        self._default = raw.get("default") or {}
        self._categories = raw.get("categories") or {}

    @classmethod
    def load(cls, path: Path | None = None) -> "Pathways":
        return cls(yaml.safe_load((path or PATHWAYS_FILE).read_text()))

    def _for(self, category: str) -> dict:
        return self._categories.get(category, {})

    def hold_days(self) -> int:
        return int(self._default.get("hold_days", HOLD_EXPIRY_DAYS))

    def reuse(self, category: str) -> list[Destination]:
        entries = self._for(category).get("reuse")
        if entries is None:
            entries = self._default.get("reuse") or []
        return [Destination(**e) for e in entries]

    def recycle(self, category: str) -> list[Destination]:
        entries = self._for(category).get("recycle")
        if entries is None:
            entries = self._default.get("recycle") or []
        return [Destination(**e) for e in entries]

    def disposal(self, category: str) -> str:
        return self._for(category).get("disposal") or self._default.get("disposal", "")


def resolve(
    item: Item,
    pathways: Pathways,
    *,
    today: date | None = None,
    hold_exhausted: bool = False,
) -> Resolution:
    """The next rung down for an item no organisation would take.

    Called once when matching fails, and again for each held item whose hold has
    run out. `hold_exhausted` is what separates "we are still trying" from
    "we tried for three weeks".
    """
    label = _label(item.category, item.quantity)
    unsafe = UNSAFE_TO_REUSE.get(item.category)

    if not hold_exhausted and not unsafe:
        days = pathways.hold_days()
        return Resolution(
            item_id=item.id,
            next_state=ItemState.HELD,
            headline=f"Holding the {label} and watching for a new need.",
            detail=(
                f"Nothing nearby needs it today. Needs move weekly, so the agent "
                f"re-checks every organisation in your radius for {days} days and "
                f"messages you the moment one comes up. You do not have to do "
                f"anything until then."
            ),
            counts_as_recovery=False,
        )

    if unsafe:
        recyclers = pathways.recycle(item.category)
        return Resolution(
            item_id=item.id,
            next_state=ItemState.RECYCLED if recyclers else ItemState.DISPOSED,
            headline=f"The {label} should not be passed to another person.",
            detail=(
                f"{unsafe.capitalize()}. "
                + (
                    f"{recyclers[0].note} "
                    if recyclers
                    else ""
                )
                + pathways.disposal(item.category)
            ).strip(),
            destination=recyclers[0] if recyclers else None,
            counts_as_recovery=False,
        )

    for destination in pathways.reuse(item.category):
        return Resolution(
            item_id=item.id,
            next_state=ItemState.REROUTED,
            headline=f"{destination.name} will take the {label}.",
            detail=destination.note,
            destination=destination,
            counts_as_recovery=True,
        )

    for destination in pathways.recycle(item.category):
        return Resolution(
            item_id=item.id,
            next_state=ItemState.RECYCLED,
            headline=f"The {label} can be recycled at {destination.name}.",
            detail=(
                destination.note
                + " This recovers material, not the item -- it is reported "
                "separately from reuse."
            ).strip(),
            destination=destination,
            counts_as_recovery=False,
        )

    return Resolution(
        item_id=item.id,
        next_state=ItemState.DISPOSED,
        headline=f"There is no reuse or recycling route for the {label} here.",
        detail=pathways.disposal(item.category)
        or "Municipal solid waste collection.",
        counts_as_recovery=False,
    )


def decline_reason_for(item: Item) -> DeclineReason:
    """Why an item entered the fallback chain at all."""
    if item.category in UNSAFE_TO_REUSE:
        return DeclineReason.SAFETY
    return item.decline_reason or DeclineReason.NO_PATHWAY
