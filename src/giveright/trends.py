"""What the neighbourhood is giving, and what it is still short of.

Every donation GiveRight routes leaves a line in a ledger. Aggregated, those
lines answer a question neither side can answer alone:

  * a donor sees that coats are pouring in and car seats are not, so the coat in
    their hallway is worth less this week than they thought;
  * an org sees a surge coming before it arrives, and edits its own needs --
    which flows straight back into the matching, because needs are the corpus.

Two rules keep this from being decorative.

**A surge must clear a floor.** One donation last week and three this week is
not a 200% surge, it is noise. `MIN_BASELINE` is what stops the dashboard
manufacturing stories.

**Supply and demand are counted separately.** Items donated and shortfall
outstanding are different quantities, and the gap between them is the only
number on the page an org can act on.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

from .models import Org
from .text import plural

LEDGER_FILE = Path(__file__).resolve().parents[2] / "data" / "ledger.jsonl"

WEEK = timedelta(days=7)

# Below this many events in the baseline window, a percentage change is noise
# and is reported as a count instead of a trend.
MIN_BASELINE = 5

# A change smaller than this is not worth a donor's attention.
SURGE_THRESHOLD = 0.25


@dataclass(frozen=True)
class DonationEvent:
    """One item, one outcome. The only thing written to the ledger."""

    occurred: str          # ISO date
    category: str
    quantity: int
    outcome: str           # ItemState value
    org_id: str | None = None
    recovered: bool = False

    @property
    def day(self) -> date:
        return date.fromisoformat(self.occurred)


class Ledger:
    """Append-only JSONL. Small enough to read whole, honest enough to audit."""

    def __init__(self, events: list[DonationEvent] | None = None,
                 path: Path | None = None):
        self.events = list(events or [])
        self.path = path or LEDGER_FILE

    @classmethod
    def load(cls, path: Path | None = None) -> "Ledger":
        path = path or LEDGER_FILE
        if not path.exists():
            return cls([], path)
        events = [
            DonationEvent(**json.loads(line))
            for line in path.read_text().splitlines()
            if line.strip()
        ]
        return cls(events, path)

    def append(self, event: DonationEvent) -> None:
        self.events.append(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(asdict(event)) + "\n")

    def window(self, start: date, end: date) -> list[DonationEvent]:
        """Half-open [start, end), so adjacent windows never double-count."""
        return [e for e in self.events if start <= e.day < end]


@dataclass(frozen=True)
class CategoryTrend:
    category: str
    this_period: int
    last_period: int
    change: float | None       # None when the baseline is too thin to divide by
    headline: str
    significant: bool

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "this_period": self.this_period,
            "last_period": self.last_period,
            "change_pct": None if self.change is None else round(self.change * 100),
            "headline": self.headline,
            "significant": self.significant,
        }


def _label(category: str) -> str:
    return plural(category)


def trends(
    ledger: Ledger,
    *,
    today: date | None = None,
    period: timedelta = WEEK,
) -> list[CategoryTrend]:
    """Volume by category this period against the one before it."""
    today = today or date.today()
    this_start, last_start = today - period, today - 2 * period

    now = Counter()
    for e in ledger.window(this_start, today + timedelta(days=1)):
        now[e.category] += e.quantity
    before = Counter()
    for e in ledger.window(last_start, this_start):
        before[e.category] += e.quantity

    out = []
    for category in sorted(set(now) | set(before)):
        current, baseline = now[category], before[category]

        if baseline < MIN_BASELINE:
            out.append(
                CategoryTrend(
                    category=category,
                    this_period=current,
                    last_period=baseline,
                    change=None,
                    headline=(
                        f"{current} {_label(category)} this week "
                        f"(too few last week to call it a trend)."
                    ),
                    significant=False,
                )
            )
            continue

        change = (current - baseline) / baseline
        significant = abs(change) >= SURGE_THRESHOLD
        direction = "up" if change > 0 else "down"
        headline = (
            f"{_label(category).capitalize()} {direction} {abs(change) * 100:.0f}% "
            f"this week -- {current} against {baseline}."
            if significant
            else f"{_label(category).capitalize()} steady at {current} this week."
        )
        out.append(
            CategoryTrend(
                category=category,
                this_period=current,
                last_period=baseline,
                change=change,
                headline=headline,
                significant=significant,
            )
        )

    return sorted(out, key=lambda t: (-abs(t.change or 0), -t.this_period))


@dataclass(frozen=True)
class Gap:
    """Where the neighbourhood's supply and its organisations disagree."""

    category: str
    donated: int          # arriving, this period
    shortfall: int        # still wanted, across every org in the corpus
    orgs_wanting: int
    headline: str

    @property
    def oversupplied(self) -> bool:
        return self.shortfall == 0 and self.donated > 0

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "donated": self.donated,
            "shortfall": self.shortfall,
            "orgs_wanting": self.orgs_wanting,
            "oversupplied": self.oversupplied,
            "headline": self.headline,
        }


def gaps(
    ledger: Ledger,
    orgs: list[Org],
    *,
    today: date | None = None,
    period: timedelta = WEEK,
) -> list[Gap]:
    """The number an org can act on: what is coming in against what is wanted."""
    today = today or date.today()
    donated = Counter()
    for e in ledger.window(today - period, today + timedelta(days=1)):
        donated[e.category] += e.quantity

    shortfall: dict[str, int] = defaultdict(int)
    wanting: dict[str, set[str]] = defaultdict(set)
    for org in orgs:
        for n in org.needs:
            if n.shortfall > 0:
                shortfall[n.category] += n.shortfall
                wanting[n.category].add(org.id)

    out = []
    for category in sorted(set(donated) | set(shortfall)):
        d, s = donated[category], shortfall[category]
        if s == 0 and d > 0:
            headline = (
                f"{d} {_label(category)} arriving and no organisation is short of "
                f"them -- worth telling donors to hold off."
            )
        elif s > 0 and d == 0:
            headline = (
                f"{s} {_label(category)} wanted across {len(wanting[category])} "
                f"organisation(s) and none donated this week."
            )
        elif s > d:
            headline = (
                f"{_label(category).capitalize()}: {d} in, {s} still wanted."
            )
        else:
            headline = (
                f"{_label(category).capitalize()} demand is close to met "
                f"({d} in, {s} still wanted)."
            )
        out.append(
            Gap(
                category=category,
                donated=d,
                shortfall=s,
                orgs_wanting=len(wanting[category]),
                headline=headline,
            )
        )

    return sorted(out, key=lambda g: (-g.shortfall, -g.donated))


def dashboard(
    ledger: Ledger,
    orgs: list[Org],
    *,
    today: date | None = None,
) -> dict:
    """One payload, read by both sides. Donors see where their thing is scarce;
    orgs see what is about to arrive."""
    total = sum(e.quantity for e in ledger.events)
    recovered = sum(e.quantity for e in ledger.events if e.recovered)

    return {
        "as_of": (today or date.today()).isoformat(),
        "items_routed": total,
        "reused_by_an_org": recovered,
        "recovery_rate": round(recovered / total, 3) if total else 0.0,
        "trends": [t.as_dict() for t in trends(ledger, today=today)],
        "gaps": [g.as_dict() for g in gaps(ledger, orgs, today=today)],
        "note": (
            "Recovery counts reuse by a named organisation. Recycling and "
            "disposal are reported separately and never folded in."
        ),
    }
