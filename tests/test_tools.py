"""The toolset, driven without a model.

Every tool is a wrapper over a deterministic function, which is exactly what
makes this possible: the whole donation run is exercised here with no Bedrock
call, no credentials and no network.
"""

from datetime import date, timedelta

import pytest

from giveright.models import Condition, Item, ItemState
from giveright.session import Workspace
from giveright.tools import build_tools

TODAY = date(2026, 9, 7)
ORIGIN = (38.9150, -77.0200)


class FakeContext:
    """Stands in for the Strands ToolContext. Answers each interrupt from a
    script, and records what the agent chose to interrupt about -- which is the
    thing worth asserting on."""

    def __init__(self, answers: dict | None = None, default=None):
        self.answers = answers or {}
        self.default = default
        self.raised: list[tuple[str, dict]] = []

    def interrupt(self, name: str, reason=None, response=None):
        self.raised.append((name, reason))
        return self.answers.get(name, self.default)


@pytest.fixture
def ws(tmp_path):
    w = Workspace.open(ORIGIN, radius_miles=8, ledger_path=tmp_path / "ledger.jsonl")
    w.today = TODAY
    return w


@pytest.fixture
def tools(ws):
    return {t.tool_name: t for t in build_tools(ws)}


@pytest.fixture
def loaded(tools):
    tools["identify_pile"]("data/fixtures/pile_01.jpg")
    return tools


def test_identification_reads_the_cached_fixture_without_a_model(tools):
    out = tools["identify_pile"]("data/fixtures/pile_01.jpg")
    assert [i["category"] for i in out["items"]] == [
        "winter_coats", "towels", "car_seats", "books", "mattresses",
        "childrens_clothing",
    ]
    assert out["unknown_to_the_corpus"] == []


def test_a_missing_photo_is_an_error_not_an_empty_pile(tools):
    """An empty list here would read as 'nothing to donate'."""
    assert "error" in tools["identify_pile"]("data/fixtures/no_such_pile.jpg")


def test_radius_is_enforced_as_a_promise(ws, loaded):
    loaded["set_radius"](0.5)
    plan = loaded["plan_dropoffs"]()
    assert plan["stops"] == []
    assert len(plan["unplaced"]) == 6


def test_the_donor_is_asked_only_about_what_changes_the_plan(loaded):
    ctx = FakeContext(default="a bit worn")
    out = loaded["ask_donor_about_condition"](tool_context=ctx)

    asked = {a["item_id"] for a in out["asked"]}
    assert asked == {"pile_01_02"}, "only the towels' condition changes a destination"
    assert ctx.raised[0][1]["at_stake"]


def test_the_answer_actually_moves_the_item(ws, loaded):
    loaded["ask_donor_about_condition"](tool_context=FakeContext(default="worn and stained"))
    assert ws.item("pile_01_02").condition is Condition.POOR

    stop = next(
        s for s in loaded["plan_dropoffs"]()["stops"]
        if any(i["item_id"] == "pile_01_02" for i in s["items"])
    )
    assert stop["org_id"] == "dev_eastside_closet"


def test_nothing_is_asked_when_nothing_is_at_stake(ws, tools):
    ws.add([Item(id="solo", category="books", condition=Condition.GOOD)])
    ctx = FakeContext()
    assert tools["ask_donor_about_condition"](tool_context=ctx)["asked"] == []
    assert ctx.raised == []


def test_every_item_reaches_a_terminal_answer(ws, loaded):
    loaded["plan_dropoffs"]()
    loaded["resolve_leftovers"]()

    unfinished = [
        i.id for i in ws.items.values()
        if i.state in (ItemState.IDENTIFIED, ItemState.CLARIFYING)
    ]
    assert unfinished == []


def test_leftovers_say_what_they_are_and_are_not_called_donations(loaded):
    loaded["plan_dropoffs"]()
    resolved = {r["item_id"]: r for r in loaded["resolve_leftovers"]()["resolved"]}

    car_seat = resolved["pile_01_03"]
    assert car_seat["outcome"] == "recycled"
    assert car_seat["counts_as_recovery"] is False
    assert resolved["pile_01_05"]["outcome"] == "held"


class TestVerifying:
    """Aged information is resolved by asking, not by discounting the score."""

    def test_the_agent_writes_and_never_dials(self, loaded):
        """The agent's only channel is email. A donor who wants to ring somewhere
        has the number from the plan and is a neighbour, not an automated call."""
        out = loaded["verify_with_org"](
            org_id="dev_eastside_closet", tool_context=FakeContext(default="yes")
        )

        assert out["sent"] is True
        assert out["to"] == "dev-null@example.invalid"
        assert "phone" not in out and "channel" not in out

    def test_the_draft_is_shown_to_the_donor_before_anything_is_sent(self, loaded):
        ctx = FakeContext(default="yes")
        loaded["verify_with_org"](org_id="dev_eastside_closet", tool_context=ctx)

        name, reason = ctx.raised[0]
        assert name == "verify:dev_eastside_closet"
        assert "days ago" in reason["why"]
        assert reason["draft"]

    def test_the_email_carries_the_actual_donation_not_a_data_survey(self, loaded):
        loaded["plan_dropoffs"]()
        out = loaded["verify_with_org"](
            org_id="dev_riverside_pantry", tool_context=FakeContext(default="yes")
        )

        assert "4 x towels" in out["message"]
        assert "TAKE THESE" in out["message"]

    def test_what_the_donor_adds_goes_into_the_message(self, loaded):
        out = loaded["verify_with_org"](
            org_id="dev_eastside_closet",
            tool_context=FakeContext(default="say I can drop off after 5pm"),
        )
        assert out["donor_added"] == "say I can drop off after 5pm"
        assert "after 5pm" in out["message"]

    def test_a_donor_saying_no_sends_nothing(self, loaded):
        out = loaded["verify_with_org"](
            org_id="dev_eastside_closet", tool_context=FakeContext(default="no")
        )
        assert out["sent"] is False

    def test_sending_is_not_an_answer(self, ws, loaded):
        """The corpus must not change because we asked a question."""
        before = ws.org("dev_eastside_closet").need_for("winter_coats").last_verified
        loaded["verify_with_org"](
            org_id="dev_eastside_closet", tool_context=FakeContext(default="yes")
        )
        assert ws.org("dev_eastside_closet").need_for("winter_coats").last_verified == before
        assert ws.observations.entries == []


class TestOrgReplies:
    def test_take_these_reconfirms_and_dates_the_need(self, ws, tools):
        out = tools["record_org_reply"]("dev_eastside_closet", "take_these", ["winter_coats"])
        assert out["verified_on"] == TODAY.isoformat()
        assert ws.org("dev_eastside_closet").need_for("winter_coats").confirmed_by_org

    def test_full_is_temporary_and_dont_take_is_permanent(self, ws, tools):
        tools["record_org_reply"]("dev_northgate_shelter", "full", ["winter_coats"])
        shelter = ws.org("dev_northgate_shelter")
        assert shelter.at_capacity
        assert "winter_coats" in shelter.accepts        # still a category they take

        tools["record_org_reply"]("dev_northgate_shelter", "dont_take", ["winter_coats"])
        assert "winter_coats" not in shelter.accepts
        assert "winter_coats" in shelter.prohibited

    def test_a_reply_survives_a_reload_without_touching_the_curated_file(self, tmp_path):
        """Machine-observed facts are replayed onto the corpus, not written into
        it. The YAML keeps its sources and its comments."""
        from giveright.corpus import DATA_DIR
        from giveright.session import Workspace

        before = (DATA_DIR / "dev_eastside_closet.yaml").read_text()

        first = Workspace.open(ORIGIN, radius_miles=8)
        first.today = TODAY
        {t.tool_name: t for t in build_tools(first)}["record_org_reply"](
            "dev_eastside_closet", "dont_take", ["books"]
        )

        reloaded = Workspace.open(ORIGIN, radius_miles=8).org("dev_eastside_closet")
        assert "books" in reloaded.prohibited
        assert reloaded.need_for("books") is None
        assert (DATA_DIR / "dev_eastside_closet.yaml").read_text() == before


def test_a_plan_is_not_a_delivery(ws, loaded):
    loaded["plan_dropoffs"]()
    assert loaded["neighbourhood_dashboard"]()["items_routed"] == 0

    loaded["record_dropoff"]("pile_01_01")

    board = loaded["neighbourhood_dashboard"]()
    assert board["items_routed"] == 2          # two coats
    assert board["reused_by_an_org"] == 2


def test_an_undelivered_item_cannot_be_counted(loaded):
    loaded["plan_dropoffs"]()
    loaded["resolve_leftovers"]()
    out = loaded["record_dropoff"]("pile_01_05")     # held, not claimed
    assert "error" in out


def test_a_delivery_changes_what_the_next_donor_is_told(ws, loaded):
    """The loop the whole product depends on: ten neighbours in one week must
    not all be sent to the same shelter for the same fifty coats."""
    loaded["plan_dropoffs"]()
    before = ws.org("dev_northgate_shelter").need_for("winter_coats").shortfall

    out = loaded["record_dropoff"]("pile_01_01")     # two coats

    assert out["need_updated"]["shortfall_now"] == before - 2

    from giveright.session import Workspace

    next_donor = Workspace.open(ORIGIN, radius_miles=8)
    assert next_donor.org("dev_northgate_shelter").need_for(
        "winter_coats"
    ).shortfall == before - 2
