"""The background sweep.

"We will hold it and keep looking" was a sentence in the product before it was
behaviour. These tests are the difference.
"""

from datetime import date, timedelta

import pytest

from giveright.fallback import Pathways
from giveright.models import Condition, Item, ItemState
from giveright.state import HOLD_EXPIRY_DAYS
from giveright.watch import Hold, HoldLog, hold_from, sweep

from conftest import ORIGIN, TODAY, need, org

PATHWAYS = Pathways.load()
RADIUS_KM = 12.0


def log(tmp_path, *holds):
    lg = HoldLog(path=tmp_path / "held.jsonl")
    for h in holds:
        lg.hold(h)
    return lg


def held(category="winter_coats", days_ago=3, qty=1, item_id="h1"):
    return Hold(
        item_id=item_id, category=category, quantity=qty,
        lat=ORIGIN[0], lng=ORIGIN[1], radius_km=RADIUS_KM,
        held_since=(TODAY - timedelta(days=days_ago)).isoformat(),
        condition="good",
    )


def test_a_quiet_sweep_says_nothing(tmp_path):
    """The normal outcome. An agent that reports 'nothing happened' every
    morning is a notification the donor turns off."""
    nobody = org("nobody", accepts={"towels": Condition.FAIR})

    found = sweep(log(tmp_path, held()), [nobody], PATHWAYS, today=TODAY)

    assert found.quiet
    assert found.notifications == []
    assert (found.checked, found.still_waiting) == (1, 1)


def test_a_new_need_reaches_the_donor(tmp_path):
    """The promise: needs move weekly, and the agent is the one watching."""
    late = org("late", needs=[need("winter_coats", target=40, on_hand=1)])

    found = sweep(log(tmp_path, held(days_ago=9)), [late], PATHWAYS, today=TODAY)

    assert not found.quiet
    note = found.notifications[0]
    assert note.kind == "rematched"
    assert note.org_id == "late"
    assert "somewhere to go after all" in note.headline
    assert "9 days" in note.detail


def test_a_rematched_item_stops_being_held(tmp_path):
    late = org("late", needs=[need("winter_coats", target=40, on_hand=1)])
    lg = log(tmp_path, held())

    sweep(lg, [late], PATHWAYS, today=TODAY)

    assert lg.open() == {}
    assert sweep(lg, [late], PATHWAYS, today=TODAY).checked == 0


def test_an_expired_hold_is_told_to_the_donor_too(tmp_path):
    nobody = org("nobody", accepts={"towels": Condition.FAIR})
    lg = log(tmp_path, held(days_ago=HOLD_EXPIRY_DAYS))

    found = sweep(lg, [nobody], PATHWAYS, today=TODAY)

    note = found.notifications[0]
    assert note.kind == "hold_expired"
    assert f"{HOLD_EXPIRY_DAYS} days" in note.detail
    assert lg.open() == {}, "a hold that ran out is no longer waiting"


def test_the_radius_promised_at_the_time_still_binds(tmp_path):
    """A hold carries the donor's radius. Re-matching it against a wider one
    later would offer something they never agreed to."""
    far = org("far", north_km=30.0, needs=[need("winter_coats", target=40, on_hand=0)])

    assert sweep(log(tmp_path, held()), [far], PATHWAYS, today=TODAY).quiet


def test_holds_survive_the_process_that_made_them(tmp_path):
    path = tmp_path / "held.jsonl"
    HoldLog(path=path).hold(held(item_id="h9"))

    assert list(HoldLog.load(path).open()) == ["h9"]


def test_a_release_closes_a_hold_without_rewriting_history(tmp_path):
    path = tmp_path / "held.jsonl"
    lg = HoldLog(path=path)
    lg.hold(held(item_id="h1"))
    lg.release("h1")

    assert HoldLog.load(path).open() == {}
    assert len(HoldLog.load(path).entries) == 2, "the promise is still on the record"


def test_hold_from_captures_what_the_promise_was_made_against():
    item = Item(id="m1", category="mattresses", quantity=1,
                state=ItemState.HELD, held_since=TODAY)
    h = hold_from(item, (38.9, -77.0), RADIUS_KM, today=TODAY)

    assert (h.lat, h.lng, h.radius_km) == (38.9, -77.0, RADIUS_KM)
    assert h.held_since == TODAY.isoformat()


def test_resolving_leftovers_records_the_hold(tmp_path):
    """The wiring: the sentence promising a re-check must create the thing that
    makes a re-check possible."""
    from giveright.session import Workspace
    from giveright.tools import build_tools

    ws = Workspace.open(ORIGIN, radius_miles=8, holds_path=tmp_path / "held.jsonl")
    ws.today = TODAY
    tools = {t.tool_name: t for t in build_tools(ws)}
    tools["identify_pile"]("data/fixtures/pile_01.jpg")
    tools["plan_dropoffs"]()
    resolved = tools["resolve_leftovers"]()["resolved"]

    holds = ws.holds.open()
    assert [r["item_id"] for r in resolved if r["outcome"] == "held"] == list(holds)
    assert holds, "something in the demo pile is held, and must be recorded"
