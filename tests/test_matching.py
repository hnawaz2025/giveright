from datetime import date, timedelta

from giveright.geo import haversine_km, km, proximity
from giveright.matching import (
    build_plan,
    candidates,
    clarification_for,
    evaluate,
)
from giveright.models import Condition, Confidence, DeclineReason, MatchBucket

from conftest import ORIGIN, TODAY, item, need, org

RADIUS = km(10)


def test_a_stale_need_is_not_pushed_down_the_list():
    """Knowing a claim is fifteen days old says nothing about how much less true
    it has become. It ranks on its merits; the agent offers to go and check."""
    stale = org("stale", north_km=1.0,
                needs=[need(verified=TODAY - timedelta(days=90), confirmed=False)])
    fresh = org("fresh", north_km=4.0, needs=[need(verified=TODAY, confirmed=True)])

    ranked = candidates(item(), [stale, fresh], ORIGIN, RADIUS, today=TODAY)

    assert ranked[0].org_id == "stale", "the nearer org still wins"
    assert ranked[0].confidence is Confidence.STALE
    assert "worth a call" in ranked[0].reason


def test_freshness_breaks_a_tie_but_never_beats_a_shorter_drive():
    same_distance_fresh = org("fresh", north_km=2.0, needs=[need(verified=TODAY)])
    same_distance_old = org(
        "old", north_km=2.0,
        needs=[need(verified=TODAY - timedelta(days=10), shelf_life=90)],
    )

    ranked = candidates(item(), [same_distance_old, same_distance_fresh],
                        ORIGIN, RADIUS, today=TODAY)

    assert ranked[0].org_id == "fresh"


def test_proximity_decides_when_the_stated_need_is_identical():
    close = org("close", north_km=1.0, needs=[need()])
    far = org("far", north_km=6.0, needs=[need()])

    ranked = candidates(item(), [close, far], ORIGIN, RADIUS, today=TODAY)

    assert ranked[0].org_id == "close"


def test_only_distance_moves_the_score():
    """A deliberate scope decision. Stock levels are unpublished, org-stated
    urgency is a number nobody writes down, and the age of a claim does not
    tell you how much less true it is. All three were removed as weights, so
    two orgs at the same distance that both listed the need score the same."""
    empty = org("empty", north_km=1.0, needs=[need(target=50, on_hand=0)])
    nearly_full = org("nearly_full", north_km=1.0, needs=[need(target=50, on_hand=49)])
    silent = org("silent", north_km=1.0, needs=[need(target=50, on_hand=None)])
    ancient = org("ancient", north_km=1.0,
                  needs=[need(verified=TODAY - timedelta(days=400), confirmed=False)])

    scores = {
        e.org_id: e.score
        for e in candidates(item(), [empty, nearly_full, silent, ancient],
                            ORIGIN, RADIUS, today=TODAY)
    }

    assert len(set(scores.values())) == 1


def test_prohibited_beats_accepts():
    o = org("o", accepts={"winter_coats": Condition.FAIR}, prohibited=["winter_coats"])
    ev = evaluate(item(), o, ORIGIN, today=TODAY)
    assert ev.bucket is MatchBucket.DECLINED
    assert ev.decline_reason is DeclineReason.POLICY


def test_condition_floor_declines():
    o = org("o", accepts={"winter_coats": Condition.GOOD}, needs=[need(on_hand=0)])
    ev = evaluate(item(condition=Condition.POOR), o, ORIGIN, today=TODAY)
    assert ev.bucket is MatchBucket.DECLINED
    assert ev.decline_reason is DeclineReason.CONDITION
    assert "good condition or better" in ev.reason


def test_full_org_is_accepted_but_not_needed():
    o = org("o", needs=[need(on_hand=50, target=50)])
    ev = evaluate(item(), o, ORIGIN, today=TODAY)
    assert ev.bucket is MatchBucket.ACCEPTED
    assert ev.usable


def test_stale_needs_rank_below_confirmed_ones():
    fresh = org("fresh", needs=[need(on_hand=2, verified=TODAY, confirmed=True)])
    stale = org(
        "stale",
        needs=[need(on_hand=2, verified=TODAY - timedelta(days=90), shelf_life=14)],
    )

    ranked = candidates(item(), [fresh, stale], ORIGIN, RADIUS, today=TODAY)

    assert ranked[0].org_id == "fresh"
    assert ranked[1].confidence is Confidence.STALE
    assert "worth a call" in ranked[1].reason


def test_radius_is_a_promise():
    desperate = org("desperate", north_km=30.0, needs=[need(on_hand=0)])
    assert candidates(item(), [desperate], ORIGIN, km(5), today=TODAY) == []


def test_org_is_not_over_filled():
    """One coat needed means one coat sent; the rest go elsewhere."""
    small = org("small", north_km=1.0, needs=[need(target=4, on_hand=3)])
    big = org("big", north_km=2.0, needs=[need(target=50, on_hand=40)])

    plan = build_plan(
        [item(id=f"c{i}") for i in range(3)], [small, big], ORIGIN, RADIUS, today=TODAY
    )

    per_org = {s.org.id: sum(u for _, u, _ in s.lines) for s in plan.stops}
    assert per_org["small"] == 1
    assert per_org["big"] == 2


def test_plan_consolidates_stops():
    a = org("a", north_km=1.0, needs=[need(target=50, on_hand=0)])
    b = org("b", north_km=1.2, needs=[need(target=50, on_hand=1)])

    plan = build_plan([item(id="c1"), item(id="c2")], [a, b], ORIGIN, RADIUS, today=TODAY)

    assert len(plan.stops) == 1


def test_unplaced_item_says_why():
    o = org("o", accepts={"towels": Condition.FAIR}, needs=[need("towels", on_hand=0)])
    plan = build_plan([item(id="c1", category="mattresses")], [o], ORIGIN, RADIUS, today=TODAY)

    assert plan.stops == []
    assert plan.unplaced[0][0].id == "c1"
    assert "an intake" in plan.unplaced[0][1].lower()


class TestClarification:
    """The donor is interrupted only when the answer changes the outcome."""

    def test_silent_when_both_answers_route_the_same(self):
        o = org("o", accepts={"winter_coats": Condition.POOR}, needs=[need(on_hand=0)])
        assert clarification_for(item(condition=None), [o], ORIGIN, RADIUS, today=TODAY) is None

    def test_asks_when_the_answer_changes_the_destination(self):
        picky = org("picky", north_km=1.0,
                    accepts={"winter_coats": Condition.GOOD}, needs=[need(on_hand=0)])
        forgiving = org("forgiving", north_km=2.0,
                        accepts={"winter_coats": Condition.POOR}, needs=[need(on_hand=25)])

        q = clarification_for(item(condition=None), [picky, forgiving], ORIGIN, RADIUS, today=TODAY)

        assert q is not None
        assert "picky" in q.at_stake and "forgiving" in q.at_stake

    def test_asks_when_a_worn_item_has_nowhere_to_go(self):
        picky = org("picky", accepts={"winter_coats": Condition.GOOD}, needs=[need(on_hand=0)])
        q = clarification_for(item(condition=None), [picky], ORIGIN, RADIUS, today=TODAY)
        assert q is not None
        assert "reuse-or-recycle" in q.at_stake

    def test_silent_when_the_donor_already_said(self):
        o = org("o", accepts={"winter_coats": Condition.GOOD}, needs=[need(on_hand=0)])
        assert clarification_for(item(condition=Condition.GOOD), [o], ORIGIN, RADIUS, today=TODAY) is None


def test_a_split_item_keeps_every_unit():
    """Three coats, an org that needs one: the other two must go somewhere.

    An earlier version placed the item once and dropped the remainder on the
    floor -- no stop, no unplaced entry, the units simply vanished.
    """
    small = org("small", north_km=1.0, needs=[need(target=4, on_hand=3)])
    big = org("big", north_km=2.0, needs=[need(target=50, on_hand=40)])

    plan = build_plan([item(id="c", qty=3)], [small, big], ORIGIN, RADIUS, today=TODAY)

    placed = sum(u for s in plan.stops for _i, u, _e in s.lines)
    assert placed + sum(1 for _ in plan.unplaced) * 0 == 3
    assert {s.org.id: sum(u for _i, u, _e in s.lines) for s in plan.stops} == {
        "small": 1, "big": 2,
    }


def test_surplus_is_labelled_rather_than_passed_off_as_needed():
    """When the only org can use one of three, the donor is told which is which."""
    only = org("only", north_km=1.0, needs=[need(target=4, on_hand=3)])

    plan = build_plan([item(id="c", qty=3)], [only], ORIGIN, RADIUS, today=TODAY)
    lines = plan.stops[0].as_dict()["items"]

    assert [(l["units"], l["needed_now"]) for l in lines] == [(1, True), (2, False)]
    assert "they had enough" in lines[1]["why"]


def test_deliveries_already_made_shrink_the_room_shown_to_the_next_donor():
    """Deliveries change capacity, and say so. They do not change the score --
    that is the point of taking stock out of the ranking."""
    o = org("o", needs=[need(target=50, on_hand=2)])
    before = candidates(item(), [o], ORIGIN, RADIUS, today=TODAY)[0]

    o.needs[0].record_delivery(20)
    after = candidates(item(), [o], ORIGIN, RADIUS, today=TODAY)[0]

    assert "room for 48 more" in before.reason
    assert "room for 28 more" in after.reason
    assert "20 already sent since they last said" in after.reason
    assert after.score == before.score


def test_room_is_left_unstated_when_the_org_never_said():
    """Silence is reported as silence, not as a number."""
    silent = org("silent", needs=[need(target=50, on_hand=None)])
    reason = candidates(item(), [silent], ORIGIN, RADIUS, today=TODAY)[0].reason

    assert "room for" not in reason
    assert "lists winter coats as a current need" in reason


def test_a_fresh_statement_from_the_org_resets_the_delivery_count():
    """Their new figure already includes what we sent -- counting it again would
    subtract the same donations twice."""
    n = need(target=50, on_hand=2)
    n.record_delivery(20)
    assert n.shortfall == 28

    n.reconfirm(30, TODAY)

    assert n.delivered_since_verified == 0
    assert n.shortfall == 20
    assert n.on_hand_estimate == 30


def test_a_split_item_appears_once_on_the_manifest():
    """Five they need plus one they will take is one thing in the boot of a car.
    Messages built from raw plan lines listed it twice, at the wrong quantity."""
    only = org("only", north_km=1.0, needs=[need(target=10, on_hand=5)])

    plan = build_plan([item(id="c", qty=6)], [only], ORIGIN, RADIUS, today=TODAY)
    stop = plan.stops[0]

    assert len(stop.lines) == 2
    assert [(m.id, m.quantity) for m in stop.manifest()] == [("c", 6)]
