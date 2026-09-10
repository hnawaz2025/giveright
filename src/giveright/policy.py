"""What the agent is not allowed to do, in one place.

These rules used to live inside the tools, as `if` statements returning error
dictionaries. That worked, but it made the safety story unauditable: to know
what the agent could not do you had to read twelve functions and trust that
none of them had drifted. Worse, a tool can only refuse *itself* -- there was no
level at which "never contact an organisation that is not on the plan" was
stated as a fact about the system.

As a Strands `InterventionHandler` it is stated once, enforced by the framework
before the tool runs, and shown in the audit log as a denial rather than as
nothing happening at all.

Three of the four actions earn their place:

  * `Deny` for things that must not happen -- writing a delivery that was never
    made, contacting an organisation nobody is being sent to, widening a radius
    the donor set.
  * `Confirm` for the one action that changes another organisation's record
    permanently.
  * `Guide` for a model that has got ahead of itself, where the right answer is
    to say what to do first rather than to refuse.

Denials are written in the second person and say what to do instead, because
the model reads them and a refusal it cannot act on becomes a retry loop.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import BeforeToolCallEvent
from strands.interventions import Confirm, Deny, Guide, InterventionHandler, Proceed

from pathlib import Path

from .geo import miles
from .models import ItemState
from .session import Workspace

# The donor sets the radius. The agent may narrow it, never widen it past the
# hard ceiling the interface offers.
MAX_RADIUS_MILES = 50.0

# Tools that reach outside the system to a real organisation.
OUTBOUND = {"message_org", "verify_with_org"}

# Nothing can be planned or resolved before there is something to plan.
NEEDS_A_PILE = {"plan_dropoffs", "ask_the_donor", "compare_options", "resolve_leftovers"}

# Nothing can be resolved or delivered before a plan exists.
NEEDS_A_PLAN = {"resolve_leftovers", "message_org", "verify_with_org"}

# Always reachable: the agent must be able to change which skill it is in.
ALWAYS = {"skills"}


SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


def load_skill_permissions(directory: Path | None = None) -> dict[str, list[str]]:
    """`allowed-tools` from every SKILL.md, as a permission table."""
    from strands import Skill

    directory = directory or SKILLS_DIR
    if not directory.exists():
        return {}
    return {s.name: list(s.allowed_tools or []) for s in Skill.from_directory(directory)}


class GiveRightPolicy(InterventionHandler):
    """The rules, enforced before a tool runs."""

    name = "giveright-policy"

    def __init__(
        self,
        ws: Workspace,
        *,
        confirm_corpus_writes: bool = True,
        skills: dict[str, list[str]] | None = None,
    ):
        self.ws = ws
        self.confirm_corpus_writes = confirm_corpus_writes
        self.skills = skills if skills is not None else load_skill_permissions()
        self.denied: list[tuple[str, str]] = []      # (tool, why), for tests and logs

    def before_tool_call(self, event: BeforeToolCallEvent, **kwargs: Any):
        use = event.tool_use or {}
        tool = use.get("name", "")
        args = use.get("input") or {}

        for check in (
            self._within_the_active_skill,
            self._pile_first,
            self._plan_first,
            self._radius_is_the_donors,
            self._only_organisations_on_the_plan,
            self._only_deliveries_that_happened,
            self._permanent_corpus_writes,
        ):
            verdict = check(tool, args)
            if verdict is not None:
                if isinstance(verdict, Deny):
                    self.denied.append((tool, verdict.reason))
                return verdict

        return Proceed()

    # -- least privilege ----------------------------------------------------

    def _within_the_active_skill(self, tool: str, args: dict):
        """A skill's `allowed-tools` is a permission, not a suggestion.

        Strands reads that list and shows it to the model, but leaves every
        tool callable -- so an agent working through `org-outreach` could still
        write to the ledger if it decided to. Here it cannot. The outreach
        skill can talk to organisations and nothing else; the watch skill can
        read and nothing else.

        Before any skill is activated the agent is unscoped, which is correct:
        it has not yet said what it is doing.
        """
        active = self._active_skills()
        if not active or tool in ALWAYS:
            return None

        permitted = set().union(*(self.tools_for(name) for name in active))
        if not permitted or tool in permitted:
            return None

        return Deny(
            f"{tool} is not part of the {', '.join(sorted(active))} skill. "
            f"Activate the skill that owns it, or stay within this one."
        )

    def _active_skills(self) -> set[str]:
        state = getattr(self.ws, "skill_state", None)
        if callable(state):
            state = state()
        return set(state or ())

    def tools_for(self, skill_name: str) -> set[str]:
        return set(self.skills.get(skill_name, ()))

    # -- ordering -----------------------------------------------------------

    def _pile_first(self, tool: str, args: dict):
        if tool in NEEDS_A_PILE and not self.ws.items:
            return Guide(
                "There is no pile yet. Call identify_pile with the donor's "
                "photograph before planning or asking about it."
            )
        return None

    def _plan_first(self, tool: str, args: dict):
        if tool in NEEDS_A_PLAN and self.ws.plan is None:
            return Guide(
                "There is no plan yet. Call plan_dropoffs first -- which "
                "organisations are involved is decided there, not here."
            )
        return None

    # -- promises to the donor ---------------------------------------------

    def _radius_is_the_donors(self, tool: str, args: dict):
        if tool != "set_radius":
            return None
        asked = args.get("radius_miles")
        if not isinstance(asked, (int, float)):
            return None
        if asked > MAX_RADIUS_MILES:
            return Deny(
                f"{asked} miles is beyond the {MAX_RADIUS_MILES:.0f} the donor "
                f"can choose. Ask them to widen it themselves if they want to."
            )
        if asked > miles(self.ws.radius_km) and self.ws.items:
            return Deny(
                f"The donor chose {miles(self.ws.radius_km):.0f} miles after "
                f"seeing what was in range. Widening it to {asked} is their "
                f"decision, not yours -- offer it, do not do it."
            )
        return None

    def _only_organisations_on_the_plan(self, tool: str, args: dict):
        if tool not in OUTBOUND:
            return None
        org_id = args.get("org_id")
        if not org_id or self.ws.plan is None:
            return None
        if any(stop.org.id == org_id for stop in self.ws.plan.stops):
            return None
        return Deny(
            f"Nobody is being sent to {org_id}, so there is nothing to ask them "
            f"about. Only contact organisations that are on the plan."
        )

    def _only_deliveries_that_happened(self, tool: str, args: dict):
        if tool != "record_dropoff":
            return None
        item = self.ws.item(args.get("item_id", ""))
        if item is None:
            return Deny(f"There is no item {args.get('item_id')!r} in this run.")
        if item.state not in (ItemState.OFFERED, ItemState.CLAIMED, ItemState.REROUTED):
            return Deny(
                f"{item.id} is {item.state.value}. A delivery is only recorded "
                f"once the donor confirms it happened -- a plan is not a delivery."
            )
        return None

    # -- other people's records ---------------------------------------------

    def _permanent_corpus_writes(self, tool: str, args: dict):
        if tool != "record_org_reply" or not self.confirm_corpus_writes:
            return None
        if str(args.get("reply", "")).strip().lower() != "dont_take":
            return None
        org_id = args.get("org_id", "an organisation")
        categories = ", ".join(args.get("categories") or [])
        return Confirm(
            prompt=(
                f"Recording that {org_id} does not accept {categories} removes "
                f"those permanently, for every future donor. Did they actually "
                f"say that?"
            )
        )
