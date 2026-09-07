"""The item state machine.

Every item that enters GiveRight ends in exactly one terminal state, which is
what makes the recovery rate computable rather than asserted. Transitions are
explicit so an illegal one raises instead of silently corrupting the metric.
"""

from __future__ import annotations

from datetime import date

from .models import (
    NOT_RECOVERED_STATES,
    RECOVERED_STATES,
    DeclineReason,
    Item,
    ItemState,
)

HOLD_EXPIRY_DAYS = 21

TRANSITIONS: dict[ItemState, frozenset[ItemState]] = {
    ItemState.IDENTIFIED: frozenset(
        {ItemState.CLARIFYING, ItemState.OFFERED, ItemState.DECLINED}
    ),
    ItemState.CLARIFYING: frozenset({ItemState.OFFERED, ItemState.DECLINED}),
    ItemState.OFFERED: frozenset({ItemState.CLAIMED, ItemState.DECLINED}),
    ItemState.CLAIMED: frozenset({ItemState.DROPPED_OFF, ItemState.DECLINED}),
    ItemState.DECLINED: frozenset(
        {
            ItemState.HELD,
            ItemState.REROUTED,
            ItemState.RECYCLED,
            ItemState.DISPOSED,
        }
    ),
    ItemState.HELD: frozenset(
        {
            ItemState.REMATCHED,
            ItemState.REROUTED,
            ItemState.RECYCLED,
            ItemState.DISPOSED,
            ItemState.EXPIRED,
        }
    ),
    ItemState.REMATCHED: frozenset({ItemState.CLAIMED, ItemState.DECLINED}),
    ItemState.REROUTED: frozenset({ItemState.DROPPED_OFF}),
    # terminal
    ItemState.DROPPED_OFF: frozenset(),
    ItemState.RECYCLED: frozenset(),
    ItemState.DISPOSED: frozenset(),
    ItemState.EXPIRED: frozenset(),
}

TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)


class IllegalTransition(ValueError):
    pass


def can_transition(src: ItemState, dst: ItemState) -> bool:
    return dst in TRANSITIONS[src]


def transition(
    item: Item,
    dst: ItemState,
    *,
    note: str = "",
    reason: DeclineReason | None = None,
    org_id: str | None = None,
    today: date | None = None,
) -> Item:
    """Move an item, recording why. Raises on an illegal move."""
    if not can_transition(item.state, dst):
        raise IllegalTransition(f"{item.id}: {item.state.value} -> {dst.value}")

    if dst is ItemState.DECLINED and reason is None:
        raise IllegalTransition(f"{item.id}: decline requires a reason code")

    item.history.append((item.state, note))
    item.state = dst

    if reason is not None:
        item.decline_reason = reason
    if org_id is not None:
        item.assigned_org_id = org_id
    if dst is ItemState.HELD:
        item.held_since = today or date.today()
    if dst in (ItemState.CLAIMED, ItemState.REMATCHED, ItemState.REROUTED):
        item.decline_reason = None

    return item


def expired(item: Item, today: date | None = None) -> bool:
    return (
        item.state is ItemState.HELD
        and item.held_days(today) >= HOLD_EXPIRY_DAYS
    )


def recovery_rate(items: list[Item]) -> dict[str, float | int]:
    """Reuse by a named org, over items that have reached a terminal answer.

    Recycling and disposal are reported alongside, never folded in -- a metric
    that counts "here is how to throw it away" as a save is not a metric.
    """
    settled = [i for i in items if i.state in RECOVERED_STATES | NOT_RECOVERED_STATES]
    if not settled:
        return {"total": 0, "recovered": 0, "rate": 0.0, "recycled": 0, "disposed": 0}

    recovered = sum(1 for i in settled if i.recovered)
    return {
        "total": len(settled),
        "recovered": recovered,
        "rate": round(recovered / len(settled), 3),
        "recycled": sum(1 for i in settled if i.state is ItemState.RECYCLED),
        "disposed": sum(1 for i in settled if i.state is ItemState.DISPOSED),
    }
