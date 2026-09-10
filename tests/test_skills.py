"""Skills, and the permissions they carry.

Strands reads `allowed-tools` and shows it to the model, but leaves every tool
callable. These tests are about the difference between telling an agent what it
may use and stopping it using anything else.
"""

import pytest

from giveright.agent import build_agent
from giveright.audit import AuditLog
from giveright.policy import GiveRightPolicy, load_skill_permissions
from giveright.session import Workspace
from stubmodel import StubModel, say, use

from conftest import ORIGIN, TODAY


@pytest.fixture
def ws(tmp_path):
    w = Workspace.open(ORIGIN, radius_miles=8, ledger_path=tmp_path / "ledger.jsonl",
                       holds_path=tmp_path / "held.jsonl")
    w.today = TODAY
    return w


def agent_for(ws, script, policy=None):
    return build_agent(
        ws, model=StubModel(script), audit=AuditLog(path=None, run="t"),
        policy=policy or GiveRightPolicy(ws),
    )


class TestTheSkillsThemselves:
    def test_all_three_load(self):
        perms = load_skill_permissions()
        assert set(perms) == {"donation-run", "org-outreach", "neighbourhood-watch"}

    def test_every_tool_belongs_to_exactly_one_skill(self, ws):
        """No overlap and no orphans: a tool in two skills makes the permission
        meaningless, and a tool in none can never be reached under a skill."""
        from giveright.tools import build_tools

        owned = [t for tools in load_skill_permissions().values() for t in tools]
        assert len(owned) == len(set(owned)), "a tool is claimed by two skills"
        assert set(owned) == {t.tool_name for t in build_tools(ws)}

    def test_instructions_are_loaded_on_demand_not_up_front(self, ws):
        """Progressive disclosure: the description is cheap, the body is not."""
        agent = agent_for(ws, [say("hi")])
        body = str(agent.tool.skills(skill_name="org-outreach"))

        assert "Talking to organisations" in body
        assert "Email is the only channel" in body


class TestPermissions:
    def activate(self, agent, name):
        agent.tool.skills(skill_name=name)

    def test_an_unscoped_agent_may_use_anything(self, ws):
        """Before a skill is activated the agent has not said what it is doing,
        so nothing is refused on those grounds."""
        policy = GiveRightPolicy(ws)
        agent = agent_for(ws, [say("hi")], policy)

        agent.tool.identify_pile(image_path="data/fixtures/pile_01.jpg")

        assert policy.denied == []
        assert len(ws.items) == 6

    def test_outreach_cannot_touch_the_ledger(self, ws):
        """The rule that motivated per-skill permissions in the first place."""
        policy = GiveRightPolicy(ws)
        agent = agent_for(ws, [say("hi")], policy)
        agent.tool.identify_pile(image_path="data/fixtures/pile_01.jpg")
        self.activate(agent, "org-outreach")

        agent.tool.record_dropoff(item_id="pile_01_01")

        assert policy.denied[-1][0] == "record_dropoff"
        assert "not part of the org-outreach skill" in policy.denied[-1][1]

    def test_a_skill_may_use_its_own_tools(self, ws):
        policy = GiveRightPolicy(ws)
        agent = agent_for(ws, [say("hi")], policy)
        self.activate(agent, "neighbourhood-watch")

        agent.tool.neighbourhood_dashboard()

        assert policy.denied == []

    def test_the_watch_skill_cannot_contact_anyone(self, ws):
        policy = GiveRightPolicy(ws)
        agent = agent_for(ws, [say("hi")], policy)
        self.activate(agent, "neighbourhood-watch")

        agent.tool.message_org(org_id="dev_northgate_shelter")

        assert policy.denied[-1][0] == "message_org"

    def test_activating_a_second_skill_widens_rather_than_replaces(self, ws):
        policy = GiveRightPolicy(ws)
        agent = agent_for(ws, [say("hi")], policy)
        self.activate(agent, "neighbourhood-watch")
        self.activate(agent, "donation-run")

        agent.tool.identify_pile(image_path="data/fixtures/pile_01.jpg")
        agent.tool.neighbourhood_dashboard()

        assert policy.denied == []

    def test_the_agent_can_always_change_skill(self, ws):
        """A permission that could trap the agent in one skill would be a bug."""
        policy = GiveRightPolicy(ws)
        agent = agent_for(ws, [say("hi")], policy)
        self.activate(agent, "neighbourhood-watch")

        self.activate(agent, "org-outreach")

        assert not any(t == "skills" for t, _why in policy.denied)
