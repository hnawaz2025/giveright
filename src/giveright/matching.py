"""Deciding where each item should go, and why.

Two ideas do the work here.

**Score only what is measured.** One number decides the order: distance. It is
the single input we compute rather than believe. Everything else the corpus
knows -- whether they listed the need, how old that claim is, how much room
they have -- is a filter, a label, or a prompt to go and check, never a
multiplier.

Three things were tried as weights and removed, for the same reason each time.
Stock levels: almost nobody publishes them. Org-stated urgency: nobody writes
down a 0.7. And the age of a claim, which we *do* know exactly -- but knowing a
fact is fifteen days old tells you nothing about how much less true it has
become, so pricing it at 0.55 was inventing a number to dress up a real one.

**Age is surfaced, not discounted.** A stale need ranks on its merits and
carries its date, and the agent offers to email or ring the organisation to
find out. Resolving the uncertainty beats guessing at its size.

**Only ask when the answer changes the answer.** A photograph shows an object,
not the facts about it that decide where it goes. Two of those facts are
unobservable often enough to matter: what condition a thing is in, and -- for
clothing especially -- who it is for. The donor is asked about either one only
when the plausible answers produce different plans. Everything else is decided
from the corpus without bothering anyone.

Nothing in this module calls a model. Every `reason` string is assembled from
corpus facts, so it can be shown to a donor as a claim about the world.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

from .geo import haversine_km, miles, proximity
from .text import label, plural, verb
from .models import (
    Condition,
    Confidence,
    DeclineReason,
    Item,
    MatchBucket,
    Need,
    Org,
)

# An org that accepts a category but has not listed it as a current need is
# still a real destination, just a much weaker one. This is a bucket separator,
# not a tuned weight.
NOT_CURRENTLY_NEEDED = 0.05

# An org already on the route wins ties within this margin. Nobody drives to six
# places, and a plan people abandon recovers nothing.
CONSOLIDATION_TOLERANCE = 0.15

# What we assume when the donor has not said. Used only to decide whether the
# question is worth asking.
OPTIMISTIC = Condition.NEW
PESSIMISTIC = Condition.POOR


@dataclass(frozen=True)
class Evaluation:
    """One item weighed against one org. The unit the ranking sorts."""

    item_id: str
    org_id: str
    bucket: MatchBucket
    distance_km: float
    score: float
    reason: str
    units: int = 0                            # how many of this item they can use
    decline_reason: DeclineReason | None = None
    confidence: Confidence | None = None
    assumed_condition: bool = False

    @property
    def usable(self) -> bool:
        return self.bucket in (MatchBucket.NEEDED_NOW, MatchBucket.ACCEPTED)


def _refusal(item: Item, org: Org) -> tuple[DeclineReason, str] | None:
    """Hard rules, in the order an org would apply them at the door."""
    if item.category in org.prohibited:
        return DeclineReason.POLICY, f"{org.name} does not accept {_label(item)} at all."
    if item.category not in org.accepts:
        return DeclineReason.POLICY, f"{org.name} has no intake for {_label(item)}."
    if org.at_capacity:
        return DeclineReason.CAPACITY, f"{org.name} is not taking drop-offs right now."
    return None


def _freshness_note(need: Need, today: date | None = None) -> str:
    """How old the claim is, in words. This is the input the ranking leans on
    hardest, so the donor gets to see it rather than infer it."""
    days = need.days_since_verified(today)
    confidence = need.confidence(today)

    if days is None:
        return "undated"
    when = "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"

    if confidence is Confidence.STALE:
        return f"last confirmed {when} -- past its shelf life, worth a call"
    if confidence is Confidence.CONFIRMED:
        return f"they confirmed it {when}"
    return f"read off their site {when}"


def _capacity_note(need: Need, shortfall: int) -> str:
    """Room to receive, stated only when an organisation actually told us.

    This caps how much is sent; it is deliberately not part of the score.
    """
    if not need.stock_known:
        return ""
    note = f" They have room for {shortfall} more"
    if need.delivered_since_verified:
        note += f" ({need.delivered_since_verified} already sent since they last said)"
    return note + "."


def _label(item: Item) -> str:
    """Plural: these strings describe a need an organisation listed."""
    return plural(item.category)


def _condition_gate(
    item: Item, org: Org, assume: Condition | None = None
) -> tuple[DeclineReason, str] | None:
    floor = org.accepts[item.category]
    condition = item.condition if item.condition is not None else assume
    if condition is None or condition >= floor:
        return None
    return (
        DeclineReason.CONDITION,
        f"{org.name} takes {_label(item)} in {floor.name.lower()} condition or better; "
        f"this one is {condition.name.lower()}.",
    )


def evaluate(
    item: Item,
    org: Org,
    origin: tuple[float, float],
    *,
    today: date | None = None,
    assume: Condition | None = None,
    remaining: dict[tuple[str, str], int] | None = None,
) -> Evaluation:
    """Weigh one item against one org.

    `remaining` lets a caller shrink an org's shortfall as items are assigned,
    so a second coat is not offered to an org that only needed one.
    """
    distance = haversine_km(origin[0], origin[1], org.lat, org.lng)

    refused = _refusal(item, org) or _condition_gate(item, org, assume)
    if refused is not None:
        reason_code, text = refused
        return Evaluation(
            item_id=item.id,
            org_id=org.id,
            bucket=MatchBucket.DECLINED,
            distance_km=distance,
            score=0.0,
            reason=text,
            decline_reason=reason_code,
        )

    need = org.need_for(item.category)
    assumed = item.condition is None
    if need is None:
        return Evaluation(
            item_id=item.id,
            org_id=org.id,
            bucket=MatchBucket.ACCEPTED,
            distance_km=distance,
            score=NOT_CURRENTLY_NEEDED * proximity(distance),
            reason=(
                f"{org.name} accepts {_label(item)} but has not listed it as a "
                f"current need."
            ),
            units=item.quantity,
            assumed_condition=assumed,
        )

    shortfall = need.shortfall
    if remaining is not None:
        shortfall = remaining.get((org.id, item.category), shortfall)

    confidence = need.confidence(today)

    if shortfall <= 0:
        return Evaluation(
            item_id=item.id,
            org_id=org.id,
            bucket=MatchBucket.ACCEPTED,
            distance_km=distance,
            score=NOT_CURRENTLY_NEEDED * proximity(distance),
            reason=(
                f"{org.name} accepts {_label(item)} but told us on "
                f"{need.last_verified} they had enough."
            ),
            units=item.quantity,
            confidence=confidence,
            assumed_condition=assumed,
        )

    units = min(item.quantity, shortfall)

    # Distance, and nothing else. They listed the need; that earned them the
    # bucket. How stale the claim is decides whether we offer to go and check,
    # not how far down the list they sit.
    score = proximity(distance)

    return Evaluation(
        item_id=item.id,
        org_id=org.id,
        bucket=MatchBucket.NEEDED_NOW,
        distance_km=distance,
        score=score,
        reason=(
            f"{org.name} lists {_label(item)} as a current need, "
            f"{_freshness_note(need, today)}, {miles(distance):.1f} mi away."
            f"{_capacity_note(need, shortfall)}"
        ),
        units=units,
        confidence=confidence,
        assumed_condition=assumed,
    )


def candidates(
    item: Item,
    orgs: list[Org],
    origin: tuple[float, float],
    radius_km: float,
    *,
    today: date | None = None,
    assume: Condition | None = None,
    remaining: dict[tuple[str, str], int] | None = None,
) -> list[Evaluation]:
    """Every org inside the radius, best first. Declines sort last, not out --
    the donor is told who said no and why."""
    evals = [
        evaluate(item, org, origin, today=today, assume=assume, remaining=remaining)
        for org in orgs
        if haversine_km(origin[0], origin[1], org.lat, org.lng) <= radius_km
    ]
    # Freshness breaks ties. It does not move an org up the list: a claim's age
    # tells us how much to trust it, not how much it is worth.
    staleness = {
        org.id: (org.need_for(item.category).staleness(today)
                 if org.need_for(item.category) else 1.0)
        for org in orgs
    }
    return sorted(evals, key=lambda e: (-e.score, staleness.get(e.org_id, 1.0), e.org_id))


# --------------------------------------------------------------------------
# Clarification: the only moment the donor is interrupted
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Clarification:
    """One question worth interrupting a donor for."""

    item_id: str
    kind: str                                  # "condition" | "category"
    question: str
    at_stake: str                              # what changes depending on the answer
    options: tuple[tuple[str, str], ...] = ()  # (value, label the donor sees)

    def as_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "question": self.question,
            "at_stake": self.at_stake,
            "options": [{"value": v, "label": l} for v, l in self.options],
        }


CONDITION_WORDS = {c.name.lower(): c for c in Condition}


def parse_condition(answer) -> Condition | None:
    """Donors answer in words, not enums.

    Lives here rather than with the tools because it is half of applying a
    clarification, and the API, the agent and the tests all need the same
    reading of "a bit worn".
    """
    text = str(answer).strip().lower()
    for word, condition in CONDITION_WORDS.items():
        if word in text:
            return condition
    if any(w in text for w in ("fine", "wearable", "works", "clean", "hand it straight")):
        return Condition.GOOD
    if any(w in text for w in ("worn", "stained", "torn", "ripped", "shabby")):
        return Condition.POOR
    if any(w in text for w in ("doesn't work", "does not work", "cracked", "snapped")):
        return Condition.BROKEN
    return None


def _best_org(
    item: Item,
    orgs: list[Org],
    origin: tuple[float, float],
    radius_km: float,
    today: date | None,
    *,
    assume: Condition | None = None,
) -> str | None:
    top = candidates(item, orgs, origin, radius_km, today=today, assume=assume)
    usable = [e for e in top if e.usable]
    return usable[0].org_id if usable else None


def category_clarification(
    item: Item,
    orgs: list[Org],
    origin: tuple[float, float],
    radius_km: float,
    *,
    today: date | None = None,
) -> Clarification | None:
    """Ask which category an item really is, when the photograph cannot say.

    Vision reports the object it can see -- `shirts` -- and lists the corpus
    categories the photograph would equally support, because whether a shirt is
    children's clothing is a fact about its owner and owners are not visible.
    The question is asked only if those candidates would be routed differently.
    """
    if len(item.alternatives) < 2:
        return None

    # Only the candidates are weighed. The observed category is what the item
    # stays as if the donor does not answer -- it is the fallback, not a choice,
    # and offering it made unambiguous piles look ambiguous.
    destinations = {
        category: _best_org(
            replace(item, category=category), orgs, origin, radius_km, today
        )
        for category in item.alternatives
    }

    if len(set(destinations.values())) < 2:
        return None                              # same answer whichever it is

    placed = [c for c, org in destinations.items() if org is not None]
    homeless = [c for c, org in destinations.items() if org is None]

    if homeless and placed:
        at_stake = (
            f"{plural(placed[0]).capitalize()} has somewhere to go within your "
            f"radius; {plural(homeless[0])} does not."
        )
    else:
        at_stake = (
            f"{plural(placed[0]).capitalize()} and {plural(placed[-1])} go to "
            f"different places."
        )

    return Clarification(
        item_id=item.id,
        kind="category",
        question=f"The photo shows {plural(item.category)} -- which is it?",
        at_stake=at_stake,
        options=tuple((c, plural(c).capitalize()) for c in item.alternatives),
    )


def clarification_for(
    item: Item,
    orgs: list[Org],
    origin: tuple[float, float],
    radius_km: float,
    *,
    today: date | None = None,
) -> Clarification | None:
    """Ask about condition only when the two plausible answers route differently.

    An agent that asks about every item is a form. An agent that asks about the
    one item whose answer matters is worth having.
    """
    if item.condition is not None:
        return None

    def best(assume: Condition) -> Evaluation | None:
        top = candidates(item, orgs, origin, radius_km, today=today, assume=assume)
        usable = [e for e in top if e.usable]
        return usable[0] if usable else None

    good, bad = best(OPTIMISTIC), best(PESSIMISTIC)
    if good is None and bad is None:
        return None                                  # nobody takes it either way
    if good is not None and bad is not None and good.org_id == bad.org_id:
        return None                                  # same destination either way

    if bad is None:
        at_stake = (
            f"If it is worn, no organisation within {miles(radius_km):.0f} mi will "
            f"take it and it goes down the reuse-or-recycle path instead."
        )
    else:
        at_stake = f"It changes the drop-off from {good.org_id} to {bad.org_id}."

    return Clarification(
        item_id=item.id,
        kind="condition",
        question=(
            f"What condition {verb(item.quantity, 'is', 'are')} the "
            f"{label(item.category, item.quantity)} in -- good enough to hand "
            f"straight to someone, or worn?"
        ),
        at_stake=at_stake,
        options=(("good", "Good"), ("worn", "Worn")),
    )


def clarifications(
    items: list[Item],
    orgs: list[Org],
    origin: tuple[float, float],
    radius_km: float,
    *,
    today: date | None = None,
) -> list[Clarification]:
    """Every question worth asking, and no others.

    Category comes first: what a thing *is* decides who could take it, and the
    condition question may not even arise once that is settled.
    """
    out: list[Clarification] = []
    for item in items:
        found = category_clarification(item, orgs, origin, radius_km, today=today)
        if found is None:
            found = clarification_for(item, orgs, origin, radius_km, today=today)
        if found is not None:
            out.append(found)
    return out


def apply_answers(
    items: list[Item], questions: list[Clarification], answers: dict[str, str]
) -> list[str]:
    """Fold the donor's replies back into the items. Returns what changed.

    Shared by the API and the agent so a donor's "a bit worn" means the same
    thing wherever it was typed.
    """
    by_id = {i.id: i for i in items}
    asked = {q.item_id: q for q in questions}
    changed = []

    for item_id, answer in answers.items():
        item, question = by_id.get(item_id), asked.get(item_id)
        if item is None or question is None:
            continue

        if question.kind == "category":
            allowed = {v for v, _l in question.options}
            if str(answer) in allowed:
                item.category = str(answer)
                item.alternatives = []
                changed.append(f"{item_id} is {plural(item.category)}")
        else:
            condition = parse_condition(answer)
            if condition is not None:
                item.condition = condition
                changed.append(f"{item_id} is {condition.name.lower()}")

    return changed


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


@dataclass
class Stop:
    org: Org
    lines: list[tuple[Item, int, Evaluation]] = field(default_factory=list)

    @property
    def distance_km(self) -> float:
        return self.lines[0][2].distance_km if self.lines else 0.0

    def manifest(self) -> list[Item]:
        """What the donor is actually bringing here, one entry per item.

        An item split across two lines at the same stop -- five they need and
        one more they will take -- is one thing in the boot of a car, and must
        appear once, at the total. Messages built from raw lines listed it twice
        and at the wrong quantity.
        """
        totals: dict[str, int] = {}
        first: dict[str, Item] = {}
        for item, units, _ev in self.lines:
            totals[item.id] = totals.get(item.id, 0) + units
            first.setdefault(item.id, item)
        return [replace(first[i], quantity=n) for i, n in totals.items()]

    def aged_categories(self, today: date | None = None) -> list[str]:
        """Categories on this stop whose information is past its shelf life."""
        return sorted(
            {
                item.category
                for item, _u, ev in self.lines
                if ev.confidence is Confidence.STALE
            }
        )

    def as_dict(self) -> dict:
        return {
            "org_id": self.org.id,
            "org": self.org.name,
            "address": self.org.address,
            "hours": self.org.hours,
            "phone": self.org.phone,
            "distance_mi": round(miles(self.distance_km), 1),
            "items": [
                {
                    "item_id": it.id,
                    "category": it.category,
                    "units": units,
                    "needed_now": ev.bucket is MatchBucket.NEEDED_NOW,
                    "why": ev.reason,
                    "confidence": ev.confidence.value if ev.confidence else None,
                }
                for it, units, ev in self.lines
            ],
        }


@dataclass
class Plan:
    radius_km: float
    stops: list[Stop] = field(default_factory=list)
    unplaced: list[tuple[Item, str]] = field(default_factory=list)
    pending: list[Clarification] = field(default_factory=list)
    _today: date | None = None

    def verification_offers(self, today: date | None = None) -> list[dict]:
        """Stops running on aged information, and how old.

        The agent reads this and offers to go and check -- which is the whole
        answer to "how much is a fifteen-day-old fact worth?". Find out.
        """
        offers = []
        for stop in self.stops:
            aged = stop.aged_categories(today)
            if not aged:
                continue
            oldest = max(
                (stop.org.need_for(c).days_since_verified(today) or 0) for c in aged
            )
            offers.append(
                {
                    "org_id": stop.org.id,
                    "org": stop.org.name,
                    "categories": aged,
                    "days_since_confirmed": oldest,
                    "email": stop.org.email,
                }
            )
        return offers

    def as_dict(self) -> dict:
        return {
            "radius_mi": round(miles(self.radius_km), 1),
            "stops": [s.as_dict() for s in self.stops],
            "verification_offers": self.verification_offers(self._today),
            "unplaced": [
                {"item_id": i.id, "category": i.category, "why": why}
                for i, why in self.unplaced
            ],
            "questions": [c.as_dict() for c in self.pending],
        }


def build_plan(
    items: list[Item],
    orgs: list[Org],
    origin: tuple[float, float],
    radius_km: float,
    *,
    today: date | None = None,
) -> Plan:
    """Assign every item, consolidating stops, and never over-filling an org.

    Items are placed strongest match first, so a scarce need is claimed by the
    donation that fits it best rather than by whichever item came first out of
    the photo.
    """
    by_org: dict[str, Org] = {o.id: o for o in orgs}
    remaining: dict[tuple[str, str], int] = {}
    for org in orgs:
        for need in org.needs:
            remaining[(org.id, need.category)] = need.shortfall

    plan = Plan(radius_km=radius_km, _today=today)
    plan.pending = clarifications(items, orgs, origin, radius_km, today=today)

    # Units still looking for a home. An item of quantity three may be split
    # across two organisations, so placing it once is not the same as finishing
    # with it -- an earlier version dropped the remainder on the floor.
    outstanding = {item.id: item.quantity for item in items}
    queue = list(items)
    stops: dict[str, Stop] = {}

    while queue:
        scored = []
        for item in queue:
            best = next(
                (
                    e
                    for e in candidates(
                        item, orgs, origin, radius_km, today=today, remaining=remaining
                    )
                    if e.usable
                ),
                None,
            )
            scored.append((item, best))

        placeable = [(i, e) for i, e in scored if e is not None]
        if not placeable:
            for item, _ in scored:
                plan.unplaced.append(
                    (item, _why_unplaced(item, orgs, origin, radius_km, today,
                                         outstanding[item.id]))
                )
            break

        item, best = max(placeable, key=lambda pair: pair[1].score)

        # Prefer somewhere we are already going, if it is nearly as good.
        ranked = [
            e
            for e in candidates(item, orgs, origin, radius_km, today=today, remaining=remaining)
            if e.usable
        ]
        chosen = next(
            (
                e
                for e in ranked
                if e.org_id in stops and e.score >= best.score * (1 - CONSOLIDATION_TOLERANCE)
            ),
            best,
        )

        org = by_org[chosen.org_id]
        left = outstanding[item.id]
        units = max(1, min(left, chosen.units or left))
        stop = stops.setdefault(org.id, Stop(org=org))
        stop.lines.append((item, units, chosen))

        key = (org.id, item.category)
        if key in remaining:
            remaining[key] = max(0, remaining[key] - units)

        outstanding[item.id] = left - units
        if outstanding[item.id] <= 0:
            queue.remove(item)

    plan.stops = sorted(stops.values(), key=lambda s: s.distance_km)
    return plan


def _why_unplaced(
    item: Item,
    orgs: list[Org],
    origin: tuple[float, float],
    radius_km: float,
    today: date | None,
    outstanding: int | None = None,
) -> str:
    """The most common reason the door was shut, in the donor's words.

    `outstanding` says how many units are still homeless, which matters when
    part of an item was placed and part was not -- "2 of your 3 coats" is a
    different message from "your coats".
    """
    left = item.quantity if outstanding is None else outstanding
    prefix = f"{left} of {item.quantity}: " if left < item.quantity else ""

    evals = candidates(item, orgs, origin, radius_km, today=today)
    if not evals:
        return f"{prefix}No organisation within {miles(radius_km):.0f} mi."
    reasons = [e.decline_reason for e in evals if e.decline_reason]
    if not reasons:
        return (
            f"{prefix}Nobody within {miles(radius_km):.0f} mi is short of "
            f"{_label(item)} right now."
        )
    top = max(set(reasons), key=reasons.count)
    return prefix + {
        DeclineReason.CONDITION: f"Every nearby org sets a higher condition bar for {_label(item)}.",
        DeclineReason.POLICY: f"No nearby org has an intake for {_label(item)}.",
        DeclineReason.CAPACITY: f"Every nearby org that takes {_label(item)} is full.",
        DeclineReason.SAFETY: f"{_label(item).capitalize()} cannot be passed on safely.",
        DeclineReason.NO_PATHWAY: f"No reuse pathway for {_label(item)} nearby.",
    }[top]
