"""The agent's hands.

Every tool here is a thin wrapper over a deterministic function. The model
decides *what to do next*; it never decides what an organisation accepts, how
far away it is, or whether an item counts as recovered. That division is the
whole safety story: a hallucinated opening time sends a person across a city to
a locked door, so opening times do not pass through the model.

Two tools deliberately stop the agent and hand control back to a human, using
the Strands interrupt mechanism:

  * `ask_the_donor` -- and only for the items where the answer changes the
    destination;
  * `verify_with_org` -- the draft is shown to the donor to approve or add to
    before it is sent. Writing to an organisation on someone's behalf is not a
    decision an agent should make alone.
"""

from __future__ import annotations

from datetime import date

from strands import ToolContext, tool

from .fallback import decline_reason_for, resolve
from .geo import km, miles
from .matching import apply_answers, build_plan, candidates, clarifications, parse_condition
from .models import Condition, ItemState
from .observations import record_delivery, record_reply
from .outreach import (
    Reply,
    apply_reply,
    confirmation_message,
    stale_categories,
    verification_message,
)
from .session import Workspace
from .state import transition
from .trends import DonationEvent, dashboard
from .vision import identify

def build_tools(ws: Workspace) -> list:
    """Bind the toolset to one donation run."""

    @tool
    def set_radius(radius_miles: float) -> dict:
        """Set how far the donor is willing to travel. This is a hard promise:
        no organisation outside it is ever offered, however badly it needs the
        item. The donor sets this before taking the photo.

        Args:
            radius_miles: Travel radius in miles.
        """
        if radius_miles <= 0:
            return {"error": "Radius must be greater than zero."}
        ws.radius_km = km(radius_miles)
        return {
            "radius_miles": radius_miles,
            "organisations_in_range": sum(
                1 for o in ws.orgs if _distance(ws, o) <= ws.radius_km
            ),
            "note": "Organisations outside this radius will not be offered.",
        }

    @tool
    def identify_pile(image_path: str) -> dict:
        """Identify every donatable item in a photograph of a pile.

        Returns each item with an id you must use in later tool calls. Condition
        is only filled in where the photo actually showed it.

        Args:
            image_path: Path to the donor's photograph.
        """
        try:
            items = identify(image_path, ws.vocabulary)
        except (FileNotFoundError, ValueError) as exc:
            return {"error": str(exc)}

        ws.add(items)
        return {
            "items": [
                {
                    "item_id": i.id,
                    "category": i.category,
                    "description": i.description,
                    "quantity": i.quantity,
                    "condition": i.condition.name.lower() if i.condition else None,
                    "note": i.attributes.get("note", ""),
                }
                for i in items
            ],
            "unknown_to_the_corpus": sorted(
                {i.category for i in items} - set(ws.vocabulary)
            ),
        }

    @tool(context=True)
    def ask_the_donor(tool_context: ToolContext) -> dict:
        """Ask the donor the questions worth asking -- and only those.

        A photograph shows an object, not the facts about it that decide where
        it goes. Two of those matter often enough to ask about: what condition
        a thing is in, and who a garment is for. Either is raised only when the
        plausible answers would send the item somewhere different.

        Call this once after identifying the pile. If nothing is at stake it
        returns immediately without bothering anyone, which is the point.
        """
        items = list(ws.items.values())
        pending = clarifications(items, ws.orgs, ws.origin, ws.radius_km, today=ws.today)
        if not pending:
            return {
                "asked": [],
                "note": (
                    "Nothing to ask -- neither condition nor category changes any "
                    "destination."
                ),
            }

        replies: dict[str, str] = {}
        answered = []
        for question in pending:
            answer = tool_context.interrupt(
                name=f"{question.kind}:{question.item_id}",
                reason=question.as_dict(),
            )
            replies[question.item_id] = str(answer)
            answered.append(
                {
                    "item_id": question.item_id,
                    "asked_about": question.kind,
                    "answer": str(answer),
                }
            )

        changed = apply_answers(items, pending, replies)
        for question in pending:
            item = ws.item(question.item_id)
            if item is not None and item.state is ItemState.IDENTIFIED:
                transition(item, ItemState.CLARIFYING, note=f"donor asked about {question.kind}")

        return {"asked": answered, "changed": changed}

    @tool
    def plan_dropoffs() -> dict:
        """Build the drop-off plan: which items go where, why, and what is left
        over. Ranks by how short each organisation actually is, not by category,
        and never sends more of something than an organisation can use.
        """
        plan = build_plan(
            list(ws.items.values()), ws.orgs, ws.origin, ws.radius_km, today=ws.today
        )
        ws.plan = plan

        for stop in plan.stops:
            for item, _units, _ev in stop.lines:
                if item.state in (ItemState.IDENTIFIED, ItemState.CLARIFYING):
                    transition(item, ItemState.OFFERED, org_id=stop.org.id,
                               note="included in the drop-off plan")

        return plan.as_dict()

    @tool
    def compare_options(item_id: str) -> dict:
        """Show every organisation weighed for one item, in order, with the
        reason each was ranked where it was -- including the ones that declined.

        Args:
            item_id: The item to explain.
        """
        item = ws.item(item_id)
        if item is None:
            return {"error": f"No item {item_id}. Identify the pile first."}

        ranked = candidates(item, ws.orgs, ws.origin, ws.radius_km, today=ws.today)
        return {
            "item_id": item_id,
            "category": item.category,
            "considered": [
                {
                    "org_id": e.org_id,
                    "verdict": e.bucket.value,
                    "distance_mi": round(miles(e.distance_km), 1),
                    "score": round(e.score, 4),
                    "why": e.reason,
                    "confidence": e.confidence.value if e.confidence else None,
                }
                for e in ranked
            ],
        }

    @tool
    def resolve_leftovers() -> dict:
        """Give every unplaced item a terminal answer: hold and keep watching,
        then a named reuse organisation, then recycling, then disposal.

        Nothing is left as 'no match found'. Only reuse counts as recovery.
        """
        if ws.plan is None:
            return {"error": "Build the plan first with plan_dropoffs."}

        out = []
        for item, why in ws.plan.unplaced:
            if item.state not in (ItemState.DECLINED, ItemState.HELD):
                if item.state in (ItemState.IDENTIFIED, ItemState.CLARIFYING):
                    transition(item, ItemState.DECLINED, reason=decline_reason_for(item),
                               note=why)
            resolution = resolve(item, ws.pathways, today=ws.today)
            transition(item, resolution.next_state, note=resolution.headline)
            payload = resolution.as_dict()
            payload["why_no_org_took_it"] = why
            out.append(payload)

        return {"resolved": out}

    @tool
    def message_org(org_id: str) -> dict:
        """Draft the one-email confirmation for an organisation on the plan.

        They answer with one word, by email or on the phone. Nothing is built
        for them to log into. Their answer updates the corpus, so the data
        stays accurate as a side effect of them receiving donations.

        Args:
            org_id: Organisation to contact.
        """
        org = ws.org(org_id)
        if org is None:
            return {"error": f"No organisation {org_id} in the corpus."}
        if ws.plan is None:
            return {"error": "Build the plan first with plan_dropoffs."}

        stop = next((s for s in ws.plan.stops if s.org.id == org_id), None)
        if stop is None:
            return {"error": f"{org_id} is not on the plan."}

        return confirmation_message(org, stop.manifest()).as_dict()

    @tool
    def record_org_reply(org_id: str, reply: str, categories: list[str]) -> dict:
        """Fold an organisation's reply back into the corpus and save it.

        'full' is temporary and clears; 'dont_take' is permanent and removes the
        category. Conflating them keeps sending an organisation things it will
        never accept.

        Args:
            org_id: Organisation replying.
            reply: One of take_these, full, dont_take, no_answer.
            categories: The categories the reply is about.
        """
        org = ws.org(org_id)
        if org is None:
            return {"error": f"No organisation {org_id} in the corpus."}
        try:
            parsed = Reply(reply.strip().lower())
        except ValueError:
            return {"error": f"reply must be one of {[r.value for r in Reply]}"}

        today = ws.today or date.today()
        update = apply_reply(org, parsed, categories, today=today)
        record_reply(org.id, parsed.value, categories, on=today, log=ws.observations)
        return update.as_dict()

    @tool(context=True)
    def verify_with_org(org_id: str, tool_context: ToolContext) -> dict:
        """Ask an organisation whether an aged need is still live.

        Use this when `plan_dropoffs` reports a verification offer. Drafts a
        real message carrying the donor's actual items -- nobody answers "please
        confirm your inventory", people answer "someone has four towels for you,
        do you still want them?" -- then shows the draft to the donor to approve,
        approve or add to before it is sent.

        Sending is not an answer. When the organisation replies, record it with
        record_org_reply.

        Args:
            org_id: Organisation to contact.
        """
        org = ws.org(org_id)
        if org is None:
            return {"error": f"No organisation {org_id} in the corpus."}

        items = _items_for(ws, org.id)
        categories = sorted({i.category for i in items})
        aged = stale_categories(org, categories, today=ws.today)

        message = verification_message(
            org, items, aged or categories, today=ws.today
        )

        answer = tool_context.interrupt(
            name=f"verify:{org.id}",
            reason={
                "org": org.name,
                "to": org.email,
                "why": _why_verifying(org, aged, ws.today),
                "draft": message.body,
                "you_can": "approve it, add something, or decline",
            },
        )

        if _declined(answer):
            return {"sent": False, "reason": "The donor declined."}

        body = message.body
        addition = _addition(answer)
        if addition:
            body = f"{body}\n\nFrom the donor: {addition}"

        return {
            "sent": True,
            "simulated": True,
            "to": org.email,
            "subject": message.subject,
            "message": body,
            "donor_added": addition or None,
            "next": "Nothing is confirmed until they answer. Use record_org_reply then.",
        }

    @tool
    def record_dropoff(item_id: str) -> dict:
        """Confirm an item was actually delivered. This is what writes the
        neighbourhood ledger, so nothing is counted as recovered on the strength
        of a plan alone.

        Args:
            item_id: The delivered item.
        """
        item = ws.item(item_id)
        if item is None:
            return {"error": f"No item {item_id}."}
        if item.state is ItemState.OFFERED:
            transition(item, ItemState.CLAIMED, note="organisation accepted")
        if item.state not in (ItemState.CLAIMED, ItemState.REROUTED):
            return {
                "error": (
                    f"{item_id} is {item.state.value}; only a claimed or rerouted "
                    f"item can be dropped off."
                )
            }
        transition(item, ItemState.DROPPED_OFF, note="donor confirmed delivery")

        # Close the loop: what we routed changes what the next donor is told.
        # Without this, ten neighbours in one week are all sent to the same
        # shelter for the same fifty coats.
        delivered_to = _credit_delivery(ws, item)

        ws.ledger.append(
            DonationEvent(
                occurred=(ws.today or date.today()).isoformat(),
                category=item.category,
                quantity=item.quantity,
                outcome=item.state.value,
                org_id=item.assigned_org_id,
                recovered=item.recovered,
            )
        )
        return {
            "item_id": item_id,
            "state": item.state.value,
            "counted_as_recovery": True,
            "need_updated": delivered_to,
        }

    @tool
    def neighbourhood_dashboard() -> dict:
        """What the neighbourhood is giving and what it is still short of.

        Read by both sides: donors see where their item is scarce this week,
        organisations see what is about to arrive and edit their own needs --
        which flows straight back into the matching.
        """
        return dashboard(ws.ledger, ws.orgs, today=ws.today)

    return [
        set_radius,
        identify_pile,
        ask_the_donor,
        plan_dropoffs,
        compare_options,
        resolve_leftovers,
        message_org,
        record_org_reply,
        verify_with_org,
        record_dropoff,
        neighbourhood_dashboard,
    ]


# --------------------------------------------------------------------------


def _items_for(ws: Workspace, org_id: str):
    """The items actually headed to this org, or everything if there is no plan."""
    if ws.plan is not None:
        for stop in ws.plan.stops:
            if stop.org.id == org_id:
                return stop.manifest()
    return list(ws.items.values())


def _why_verifying(org, aged: list[str], today) -> str:
    if not aged:
        return f"Confirming the donation before sending the donor to {org.name}."
    oldest = max((org.need_for(c).days_since_verified(today) or 0) for c in aged)
    return (
        f"Our record of what {org.name} needs was last updated {oldest} days ago, "
        f"and the plan depends on it."
    )


def _credit_delivery(ws: Workspace, item) -> dict | None:
    """Count a delivery against the receiving org's need, and persist it.

    Recorded separately from the org's own attested figure, so the corpus never
    claims an organisation said something it did not. See `Need`.
    """
    if item.assigned_org_id is None:
        return None
    org = ws.org(item.assigned_org_id)
    if org is None:
        return None
    need = org.need_for(item.category)
    if need is None:
        return None

    today = ws.today or date.today()
    need.record_delivery(item.quantity)
    record_delivery(org.id, item.category, item.quantity, on=today, log=ws.observations)
    return {
        "org_id": org.id,
        "category": item.category,
        "delivered_since_verified": need.delivered_since_verified,
        "shortfall_now": need.shortfall,
    }


def _distance(ws: Workspace, org) -> float:
    from .geo import haversine_km

    return haversine_km(ws.origin[0], ws.origin[1], org.lat, org.lng)


def _text(answer) -> str:
    return ("" if answer is None else str(answer)).strip()


def _declined(answer) -> bool:
    if isinstance(answer, bool):
        return not answer
    text = _text(answer).lower()
    return text in {"n", "no", "cancel", "stop", "skip", "don\'t", "dont", "false"}


_APPROVALS = {"y", "yes", "ok", "okay", "go ahead", "true", "approved",
              "send", "send it", "looks good", "fine", ""}


def _addition(answer) -> str:
    """Anything the donor wrote that is not simply approval."""
    if isinstance(answer, bool):
        return ""
    text = _text(answer)
    if text.lower() in _APPROVALS:
        return ""
    return text
