import pytest
from datetime import date, timedelta

from giveright.models import DeclineReason, Item, ItemState
from giveright.state import (
    HOLD_EXPIRY_DAYS,
    IllegalTransition,
    expired,
    recovery_rate,
    transition,
)


def item(state=ItemState.IDENTIFIED, **kw):
    return Item(id=kw.pop("id", "i1"), category=kw.pop("category", "towels"),
                state=state, **kw)


def test_legal_path_to_drop_off():
    it = item()
    transition(it, ItemState.OFFERED, org_id="org1")
    transition(it, ItemState.CLAIMED)
    transition(it, ItemState.DROPPED_OFF)
    assert it.recovered
    assert it.assigned_org_id == "org1"


def test_illegal_transition_raises():
    with pytest.raises(IllegalTransition):
        transition(item(), ItemState.DROPPED_OFF)


def test_decline_requires_a_reason():
    with pytest.raises(IllegalTransition):
        transition(item(), ItemState.DECLINED)


def test_decline_then_held_then_rematched_is_recovery():
    it = item()
    transition(it, ItemState.OFFERED)
    transition(it, ItemState.DECLINED, reason=DeclineReason.CAPACITY)
    transition(it, ItemState.HELD)
    transition(it, ItemState.REMATCHED, org_id="org2")
    assert it.recovered
    assert it.decline_reason is None   # cleared once it found a home


def test_hold_expiry():
    it = item()
    transition(it, ItemState.OFFERED)
    transition(it, ItemState.DECLINED, reason=DeclineReason.NO_PATHWAY)
    transition(it, ItemState.HELD, today=date(2026, 9, 1))
    assert not expired(it, today=date(2026, 9, 1))
    assert expired(it, today=date(2026, 9, 1) + timedelta(days=HOLD_EXPIRY_DAYS))


def test_disposal_is_not_recovery():
    """The headline metric is meaningless if 'here is how to bin it' counts."""
    disposed = item(id="a")
    transition(disposed, ItemState.DECLINED, reason=DeclineReason.CONDITION)
    transition(disposed, ItemState.DISPOSED)

    rerouted = item(id="b")
    transition(rerouted, ItemState.DECLINED, reason=DeclineReason.CAPACITY)
    transition(rerouted, ItemState.REROUTED, org_id="goodwill")

    stats = recovery_rate([disposed, rerouted])
    assert stats == {"total": 2, "recovered": 1, "rate": 0.5,
                     "recycled": 0, "disposed": 1}


def test_unsettled_items_are_excluded():
    stats = recovery_rate([item(), item(id="i2", state=ItemState.OFFERED)])
    assert stats["total"] == 0
