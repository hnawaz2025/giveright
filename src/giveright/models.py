"""Core domain types for GiveRight.

Facts about an organization -- hours, phone numbers, what it will accept -- come
from the corpus and never from a model. These types are the shape of that corpus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum


class ItemState(str, Enum):
    IDENTIFIED = "identified"      # vision produced it
    CLARIFYING = "clarifying"      # waiting on a donor answer
    OFFERED = "offered"            # routed to an org
    CLAIMED = "claimed"            # org said yes
    DROPPED_OFF = "dropped_off"    # donor confirmed delivery
    DECLINED = "declined"          # org said no
    HELD = "held"                  # no home yet, agent still watching
    REMATCHED = "rematched"        # held item found a later home
    REROUTED = "rerouted"          # sent to a named reuse org
    RECYCLED = "recycled"          # material recovery, not reuse
    DISPOSED = "disposed"          # landfill instructions
    EXPIRED = "expired"            # held too long, gave up


class DeclineReason(str, Enum):
    CONDITION = "condition"
    POLICY = "policy"
    CAPACITY = "capacity"
    SAFETY = "safety"
    NO_PATHWAY = "no_pathway"      # nobody in radius takes this at all


class MatchBucket(str, Enum):
    NEEDED_NOW = "needed_now"      # matches a standing need
    ACCEPTED = "accepted"          # allowed, but not currently needed
    UNMATCHED = "unmatched"        # usable, no recipient found
    DECLINED = "declined"          # refused on condition/policy/capacity/safety


class Condition(int, Enum):
    """Ordered so a category's condition floor is a simple comparison."""

    BROKEN = 0
    POOR = 1
    FAIR = 2
    GOOD = 3
    NEW = 4


class Confidence(str, Enum):
    CONFIRMED = "confirmed"    # the org told us
    PUBLISHED = "published"    # read off their public page, still fresh
    STALE = "stale"            # published, past its shelf life


# Recovery counts reuse by a named organization. Recycling and disposal are
# reported separately -- collapsing them would make the headline metric
# unfalsifiable, since disposal instructions always exist.
RECOVERED_STATES = frozenset(
    {ItemState.CLAIMED, ItemState.DROPPED_OFF, ItemState.REMATCHED, ItemState.REROUTED}
)
NOT_RECOVERED_STATES = frozenset(
    {ItemState.RECYCLED, ItemState.DISPOSED, ItemState.EXPIRED}
)


@dataclass
class Need:
    """One standing need. Shelf life is what makes the corpus self-correcting.

    Stock is split across two fields on purpose.

    `on_hand_estimate` is what the organisation attested, as of `last_verified`.
    It is never edited by GiveRight, so it stays auditable -- you can always see
    what they actually said.

    `delivered_since_verified` is what GiveRight itself has routed and had
    confirmed since that date. Keeping it separate means the shortfall reflects
    donations already on their way without pretending the organisation told us
    a number it never told us. When they next confirm, their fresh figure
    already includes those deliveries, so the counter resets to zero.

    `on_hand_estimate = None` means nobody has ever said, which is the usual
    case: almost no organisation publishes its stock levels. That is why
    shortfall caps how much is *sent* but has no weight in the *ranking* -- a
    score leaning on a number we rarely hold would be a guess wearing a
    decimal point.
    """

    category: str
    target_qty: int
    on_hand_estimate: int | None = None
    delivered_since_verified: int = 0
    source_url: str | None = None
    last_verified: date | None = None
    confirmed_by_org: bool = False
    shelf_life_days: int = 14

    @property
    def stock_known(self) -> bool:
        return self.on_hand_estimate is not None

    @property
    def shortfall(self) -> int:
        """How many more they can take, counting what we have already sent.

        This is a capacity cap, not a ranking weight. With stock unknown it
        falls back to the whole target: not knowing how full they are is no
        reason to refuse a donation they asked for.
        """
        on_hand = self.on_hand_estimate or 0
        return max(0, self.target_qty - on_hand - self.delivered_since_verified)

    def record_delivery(self, quantity: int) -> None:
        """Count a confirmed drop-off against this need.

        Called only once a donor has confirmed delivery. A plan is not a
        delivery, so routing an item changes nothing here.
        """
        self.delivered_since_verified += max(0, quantity)

    def reconfirm(self, on_hand: int | None, today: date) -> None:
        """Fold in a fresh statement from the organisation.

        Their new figure already accounts for anything we sent, so the delivery
        counter resets -- leaving it would subtract the same donations twice.
        """
        if on_hand is not None:
            self.on_hand_estimate = on_hand
        self.delivered_since_verified = 0
        self.last_verified = today
        self.confirmed_by_org = True

    def days_since_verified(self, today: date | None = None) -> int | None:
        if self.last_verified is None:
            return None
        return ((today or date.today()) - self.last_verified).days

    def staleness(self, today: date | None = None) -> float:
        """0.0 = just verified, 1.0 = at shelf life, >1.0 = overdue."""
        days = self.days_since_verified(today)
        if days is None:
            return 1.0
        return days / self.shelf_life_days

    def confidence(self, today: date | None = None) -> Confidence:
        if self.staleness(today) > 1.0:
            return Confidence.STALE
        return Confidence.CONFIRMED if self.confirmed_by_org else Confidence.PUBLISHED


@dataclass
class Org:
    id: str
    name: str
    lat: float
    lng: float
    address: str = ""
    hours: str = ""
    phone: str = ""
    email: str = ""
    preferred_channel: str = "email"
    verification_source: str = ""      # how we know it is a real 501(c)(3)
    accepts: dict[str, Condition] = field(default_factory=dict)   # category -> floor
    prohibited: list[str] = field(default_factory=list)
    needs: list[Need] = field(default_factory=list)
    at_capacity: bool = False

    def need_for(self, category: str) -> Need | None:
        return next((n for n in self.needs if n.category == category), None)


@dataclass
class Item:
    id: str
    category: str
    description: str = ""
    quantity: int = 1
    condition: Condition | None = None       # None until asked or assumed
    attributes: dict[str, str] = field(default_factory=dict)
    state: ItemState = ItemState.IDENTIFIED
    assigned_org_id: str | None = None
    decline_reason: DeclineReason | None = None
    held_since: date | None = None
    history: list[tuple[ItemState, str]] = field(default_factory=list)

    @property
    def recovered(self) -> bool:
        return self.state in RECOVERED_STATES

    def held_days(self, today: date | None = None) -> int:
        if self.held_since is None:
            return 0
        return ((today or date.today()) - self.held_since).days
