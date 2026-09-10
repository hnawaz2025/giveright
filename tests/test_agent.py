"""The agent loop itself, driven through Strands with no credentials."""

import pytest

from giveright.agent import build_agent
from giveright.session import Workspace
from stubmodel import StubModel, say, use

from conftest import ORIGIN, TODAY


@pytest.fixture
def ws(tmp_path):
    w = Workspace.open(ORIGIN, radius_miles=8, ledger_path=tmp_path / "ledger.jsonl")
    w.today = TODAY
    return w


def test_every_tool_is_registered_with_the_agent(ws):
    agent = build_agent(ws, model=StubModel())

    assert len(agent.tool_names) == 13, "twelve tools, plus the skills tool"
    assert "plan_dropoffs" in agent.tool_names
    assert "ask_the_donor" in agent.tool_names
    assert "skills" in agent.tool_names


def test_the_tools_reach_the_model_as_specs(ws):
    """If a tool's schema does not render, the model can never call it."""
    model = StubModel([say("done")])
    build_agent(ws, model=model)("hello")

    names = {spec["name"] for spec in model.tool_specs}
    assert "identify_pile" in names
    spec = next(s for s in model.tool_specs if s["name"] == "set_radius")
    assert "radius_miles" in str(spec["inputSchema"])


def test_the_loop_runs_a_tool_and_carries_the_result_back(ws):
    """model -> tool -> result -> model, through the real agent."""
    model = StubModel([
        use("identify_pile", image_path="data/fixtures/pile_01.jpg"),
        say("Six things."),
    ])

    result = build_agent(ws, model=model)("what is in this pile?")

    assert len(ws.items) == 6, "the tool actually ran and mutated the workspace"
    assert model.turn == 2, "the agent came back to the model with the result"
    assert "Six things" in str(result)


def test_a_direct_tool_call_goes_through_the_agent(ws):
    """agent.tool.<name>() exercises the registry without a model turn."""
    agent = build_agent(ws, model=StubModel())

    agent.tool.identify_pile(image_path="data/fixtures/pile_01.jpg")

    assert len(ws.items) == 6


def test_a_multi_step_run_reaches_a_plan(ws):
    model = StubModel([
        use("set_radius", radius_miles=8),
        use("identify_pile", image_path="data/fixtures/pile_01.jpg"),
        use("plan_dropoffs"),
        use("resolve_leftovers"),
        say("Three stops."),
    ])

    build_agent(ws, model=model)("sort out my pile")

    assert ws.plan is not None and ws.plan.stops
    assert model.turn == 5
    assert all(i.state.value != "identified" for i in ws.items.values())


class TestInterrupts:
    """The human-in-the-loop round trip: the agent stops, a person answers, the
    agent resumes with that answer. It had never executed outside a fake tool
    context, which made the most interesting behaviour in the product the least
    verified."""

    def pile_then_ask(self):
        return StubModel([
            use("identify_pile", image_path="data/fixtures/pile_01.jpg"),
            use("ask_the_donor"),
            say("Planned."),
        ])

    def test_the_agent_stops_and_hands_back_the_question(self, ws):
        result = build_agent(ws, model=self.pile_then_ask())("sort my pile")

        assert result.interrupts, "the run paused instead of guessing"
        reason = result.interrupts[0].reason
        assert reason["kind"] == "condition"
        assert "at_stake" in reason and reason["at_stake"]

    def test_the_answer_reaches_the_item_on_resume(self, ws):
        agent = build_agent(ws, model=self.pile_then_ask())
        paused = agent("sort my pile")

        agent([{
            "interruptResponse": {
                "interruptId": paused.interrupts[0].id,
                "response": "worn and stained",
            }
        }])

        towels = next(i for i in ws.items.values() if i.category == "towels")
        assert towels.condition.name == "POOR", "the donor's words moved the item"

    def test_the_answer_changes_where_the_item_goes(self, ws):
        agent = build_agent(ws, model=StubModel([
            use("identify_pile", image_path="data/fixtures/pile_01.jpg"),
            use("ask_the_donor"),
            use("plan_dropoffs"),
            say("Planned."),
        ]))
        paused = agent("sort my pile")

        agent([{
            "interruptResponse": {
                "interruptId": paused.interrupts[0].id,
                "response": "worn",
            }
        }])

        towels = next(i for i in ws.items.values() if i.category == "towels")
        stop = next(s for s in ws.plan.stops
                    if any(i.id == towels.id for i, _u, _e in s.lines))
        assert stop.org.id == "dev_eastside_closet"

    def test_nothing_is_asked_when_nothing_is_at_stake(self, ws):
        """The agent must not pause for the sake of pausing."""
        result = build_agent(ws, model=StubModel([
            use("identify_pile", image_path="data/fixtures/pile_01.jpg"),
            use("plan_dropoffs"),
            say("Planned."),
        ]))("sort my pile")

        assert not result.interrupts
