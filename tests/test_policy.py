"""The rules, enforced by the framework before a tool runs.

Each of these used to be an `if` inside a tool. As a handler they are stated
once and provable, and a denial shows up in the audit log as a denial rather
than as nothing having happened.
"""

import pytest

from giveright.agent import build_agent
from giveright.audit import AuditLog
from giveright.policy import GiveRightPolicy
from giveright.session import Workspace
from stubmodel import StubModel, say, use

from conftest import ORIGIN, TODAY

PILE = "data/fixtures/pile_01.jpg"


@pytest.fixture
def ws(tmp_path):
    w = Workspace.open(ORIGIN, radius_miles=8, ledger_path=tmp_path / "ledger.jsonl",
                       holds_path=tmp_path / "held.jsonl")
    w.today = TODAY
    return w


def run(ws, script, tmp_path=None, **policy_kw):
    policy = GiveRightPolicy(ws, **policy_kw)
    log = AuditLog(path=None, run="t")
    build_agent(ws, model=StubModel(script), audit=log, policy=policy)("go")
    return policy, log


class TestPromisesToTheDonor:
    def test_the_radius_cannot_be_widened_past_what_is_offered(self, ws):
        policy, _ = run(ws, [use("set_radius", radius_miles=500), say("done")])

        assert policy.denied and policy.denied[0][0] == "set_radius"
        assert "beyond the 50" in policy.denied[0][1]

    def test_the_agent_cannot_quietly_widen_the_donors_radius(self, ws):
        policy, _ = run(ws, [
            use("identify_pile", image_path=PILE),
            use("set_radius", radius_miles=40),
            say("done"),
        ])

        assert ("set_radius", policy.denied[0][1]) == policy.denied[0]
        assert "their decision, not yours" in policy.denied[0][1]

    def test_narrowing_is_still_allowed(self, ws):
        policy, _ = run(ws, [
            use("identify_pile", image_path=PILE),
            use("set_radius", radius_miles=2),
            say("done"),
        ])
        assert policy.denied == []


class TestOtherPeople:
    def test_an_organisation_nobody_is_visiting_is_not_contacted(self, ws):
        policy, _ = run(ws, [
            use("identify_pile", image_path=PILE),
            use("plan_dropoffs"),
            use("message_org", org_id="dev_riverside_pantry"),
            say("done"),
        ])
        # riverside may legitimately be on the plan; assert the rule directly
        verdict = policy._only_organisations_on_the_plan(
            "message_org", {"org_id": "irs_000000000"}
        )
        assert verdict is not None and "Nobody is being sent to" in verdict.reason

    def test_removing_a_category_forever_asks_first(self, ws):
        policy = GiveRightPolicy(ws)
        verdict = policy._permanent_corpus_writes(
            "record_org_reply",
            {"org_id": "dev_northgate_shelter", "reply": "dont_take",
             "categories": ["winter_coats"]},
        )
        assert verdict is not None
        assert "for every future donor" in verdict.prompt

    def test_an_ordinary_reply_needs_no_confirmation(self, ws):
        policy = GiveRightPolicy(ws)
        assert policy._permanent_corpus_writes(
            "record_org_reply", {"org_id": "x", "reply": "full", "categories": []}
        ) is None


class TestDeliveries:
    def test_a_plan_is_not_a_delivery(self, ws):
        policy, _ = run(ws, [
            use("identify_pile", image_path=PILE),
            use("record_dropoff", item_id="pile_01_01"),
            say("done"),
        ])

        assert policy.denied[0][0] == "record_dropoff"
        assert "a plan is not a delivery" in policy.denied[0][1]

    def test_an_item_that_does_not_exist_is_refused(self, ws):
        policy, _ = run(ws, [
            use("identify_pile", image_path=PILE),
            use("record_dropoff", item_id="nonsense"),
            say("done"),
        ])
        assert "no item" in policy.denied[0][1]

    def test_a_delivered_item_is_allowed_through(self, ws):
        policy, _ = run(ws, [
            use("identify_pile", image_path=PILE),
            use("plan_dropoffs"),
            use("record_dropoff", item_id="pile_01_01"),
            say("done"),
        ])
        assert policy.denied == []


class TestSteering:
    """Where the right answer is to say what to do first, not to refuse."""

    def test_planning_before_there_is_a_pile_is_guided_not_blocked(self, ws):
        policy = GiveRightPolicy(ws)
        verdict = policy._pile_first("plan_dropoffs", {})
        assert verdict is not None
        assert "Call identify_pile" in verdict.feedback

    def test_contacting_before_there_is_a_plan_is_guided(self, ws):
        policy = GiveRightPolicy(ws)
        ws.items["x"] = object()
        assert "Call plan_dropoffs" in policy._plan_first("message_org", {}).feedback


def test_a_denial_is_recorded_as_a_denial(ws):
    """Not as nothing having happened -- which is what a tool-level refusal
    looks like from outside."""
    _policy, log = run(ws, [
        use("identify_pile", image_path=PILE),
        use("record_dropoff", item_id="pile_01_01"),
        say("done"),
    ])

    denied = [e for e in log.entries if e.outcome == "denied"]
    assert denied and denied[0].tool == "record_dropoff"
    assert "not a delivery" in denied[0].detail
