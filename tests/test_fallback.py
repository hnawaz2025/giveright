from datetime import date

from giveright.fallback import Pathways, decline_reason_for, resolve
from giveright.models import DeclineReason, Item, ItemState
from giveright.state import transition

PATHWAYS = Pathways.load()


def it(category="winter_coats", id="i1"):
    return Item(id=id, category=category)


def test_first_answer_is_to_keep_trying_not_to_bin_it():
    r = resolve(it(), PATHWAYS)
    assert r.next_state is ItemState.HELD
    assert "watching" in r.headline
    assert not r.counts_as_recovery


def test_exhausted_hold_reaches_a_named_reuse_org():
    r = resolve(it(), PATHWAYS, hold_exhausted=True)
    assert r.next_state is ItemState.REROUTED
    assert r.destination is not None and r.destination.name
    assert r.counts_as_recovery


def test_recycling_is_offered_but_never_counted_as_recovery():
    r = resolve(it("mattresses"), PATHWAYS, hold_exhausted=True)
    assert r.next_state is ItemState.RECYCLED
    assert not r.counts_as_recovery
    assert "reported" in r.detail


def test_unsafe_items_skip_the_hold_and_are_never_passed_on():
    """A car seat past its date must not reach another family, however needed."""
    r = resolve(it("car_seats"), PATHWAYS)
    assert r.next_state is ItemState.RECYCLED
    assert not r.counts_as_recovery
    assert "should not be passed to another person" in r.headline
    assert decline_reason_for(it("car_seats")) is DeclineReason.SAFETY


def test_disposal_when_there_is_no_route_at_all():
    empty = Pathways({"default": {"reuse": [], "recycle": [], "disposal": "Kerbside."}})
    r = resolve(it("anvils"), empty, hold_exhausted=True)
    assert r.next_state is ItemState.DISPOSED
    assert not r.counts_as_recovery


def test_resolution_states_are_reachable_in_the_state_machine():
    """The fallback chain must not propose a move the state machine rejects."""
    for category, exhausted in [("winter_coats", False), ("winter_coats", True),
                                ("mattresses", True), ("car_seats", False)]:
        item = it(category, id=category)
        transition(item, ItemState.DECLINED, reason=decline_reason_for(item))
        r = resolve(item, PATHWAYS, hold_exhausted=exhausted)
        transition(item, r.next_state)     # raises if the chain is inconsistent
        assert item.state is r.next_state
