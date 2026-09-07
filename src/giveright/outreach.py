"""Talking to organisations, and letting their answers correct the corpus.

Needs go stale. The usual fix is to scrape harder; this one asks, in the
cheapest form an answer can take -- one email, three words:

    TAKE THESE   /   FULL   /   DON'T TAKE

There is nothing here for an organisation to log into, install, or adopt. They
reply to an email the way they already reply to email, and that reply writes
straight back into the needs data. The corpus stays accurate as a side effect
of orgs doing the thing they already want to do, which is receive donations.

Email is the only channel the agent uses. It could place calls, and an earlier
version did, gated on a consent flag. That was the wrong trade: an organisation
that starts receiving voice calls it never asked for stops answering its phone,
and for a project whose entire asset is the goodwill of local organisations
that is the failure worth avoiding. Their phone number is still shown to the
donor, who is a neighbour and may ring whoever they like.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from .models import Confidence, Item, Need, Org
from .text import label, plural


class Reply(str, Enum):
    TAKE_THESE = "take_these"
    FULL = "full"
    DONT_TAKE = "dont_take"
    NO_ANSWER = "no_answer"


@dataclass(frozen=True)
class Message:
    org_id: str
    channel: str
    subject: str
    body: str
    reply_options: tuple[str, ...] = tuple(r.value for r in Reply if r is not Reply.NO_ANSWER)

    def as_dict(self) -> dict:
        return {
            "org_id": self.org_id,
            "channel": self.channel,
            "subject": self.subject,
            "body": self.body,
            "reply_options": list(self.reply_options),
        }


@dataclass
class CorpusUpdate:
    """What an org's reply changed. Written back to the YAML, not kept in a cache."""

    org_id: str
    changes: list[str] = field(default_factory=list)
    verified_on: date | None = None

    def as_dict(self) -> dict:
        return {
            "org_id": self.org_id,
            "changes": self.changes,
            "verified_on": self.verified_on.isoformat() if self.verified_on else None,
        }


def _lines(items: list[Item]) -> str:
    return "\n".join(
        f"  - {i.quantity} x {label(i.category, i.quantity)}"
        + (f" ({i.description})" if i.description else "")
        for i in items
    )


def confirmation_message(org: Org, items: list[Item]) -> Message:
    """One email, answerable with one word in about four seconds."""
    return Message(
        org_id=org.id,
        channel=org.preferred_channel,
        subject=f"{len(items)} item(s) offered to {org.name}",
        body=(
            f"A neighbour has these to drop off:\n\n{_lines(items)}\n\n"
            "Reply with one of:\n"
            "  TAKE THESE   - we want them, send the donor over\n"
            "  FULL         - we take these normally, but not right now\n"
            "  DON'T TAKE   - we do not accept these at all\n\n"
            "Whatever you tap updates what we tell donors, so you stop getting "
            "offers you cannot use."
        ),
    )


def apply_reply(
    org: Org,
    reply: Reply,
    categories: list[str],
    *,
    today: date | None = None,
) -> CorpusUpdate:
    """Fold an org's answer back into its own record.

    FULL and DON'T TAKE are different facts and are stored differently: capacity
    is temporary and clears, policy is permanent and removes the category. A
    system that conflates them keeps sending an org things it will never accept.
    """
    today = today or date.today()
    update = CorpusUpdate(org_id=org.id, verified_on=today)

    if reply is Reply.TAKE_THESE:
        org.at_capacity = False
        for category in categories:
            existing = org.need_for(category)
            if existing is None:
                # They want them; they have not said how many they hold. Stock
                # stays unknown rather than being recorded as zero, which would
                # make a brand-new need outrank every measured one.
                org.needs.append(
                    Need(category=category, target_qty=1, on_hand_estimate=None,
                         last_verified=today, confirmed_by_org=True)
                )
                update.changes.append(f"recorded a confirmed need for {category}")
            else:
                existing.reconfirm(None, today)
                update.changes.append(f"re-confirmed {category} need on {today}")

    elif reply is Reply.FULL:
        org.at_capacity = True
        for category in categories:
            existing = org.need_for(category)
            if existing is not None:
                existing.reconfirm(existing.target_qty, today)
                update.changes.append(f"{category} marked stocked as of {today}")
        update.changes.append("marked at capacity")

    elif reply is Reply.DONT_TAKE:
        for category in categories:
            org.accepts.pop(category, None)
            org.needs = [n for n in org.needs if n.category != category]
            if category not in org.prohibited:
                org.prohibited.append(category)
            update.changes.append(f"{category} removed from intake permanently")

    else:  # NO_ANSWER
        update.changes.append("no reply; existing facts left untouched and still ageing")
        update.verified_on = None

    return update


def verification_message(
    org: Org, items: list[Item], categories: list[str], *, today: date | None = None
) -> Message:
    """Ask whether an aged need is still live, carrying the actual donation.

    Deliberately not a data-quality survey. Nobody answers "please confirm your
    inventory"; people answer "someone has four towels for you, do you still
    want them?". The corpus gets corrected as a side effect of a real offer.
    """
    ages = []
    for category in categories:
        need = org.need_for(category)
        days = need.days_since_verified(today) if need else None
        if days is not None:
            ages.append(f"{plural(category)} ({days} days ago)")

    return Message(
        org_id=org.id,
        channel=org.preferred_channel,
        subject=f"Still needed? {len(items)} item(s) for {org.name}",
        body=(
            f"A neighbour has these to drop off:\n\n{_lines(items)}\n\n"
            + (
                f"Our record of what you need was last updated: "
                f"{'; '.join(ages)}. Rather than send them over on old "
                f"information, we wanted to ask.\n\n"
                if ages
                else ""
            )
            + "Reply with one of:\n"
            "  TAKE THESE   - yes, still needed, send them over\n"
            "  FULL         - we take these normally, but not right now\n"
            "  DON'T TAKE   - we do not accept these at all\n\n"
            "Whatever you say updates what we tell donors, so you stop getting "
            "offers you cannot use."
        ),
    )


def stale_categories(org: Org, categories: list[str], *, today: date | None = None) -> list[str]:
    """The categories whose facts have aged past their shelf life."""
    return [
        c for c in categories
        if (need := org.need_for(c)) is not None
        and need.confidence(today) is Confidence.STALE
    ]
