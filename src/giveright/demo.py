"""A terminal walkthrough of one donation run.

Two modes, deliberately:

    python -m giveright.demo                  # deterministic, no model, no cost
    python -m giveright.demo --agent          # the real Strands agent on Bedrock

The first exists because the interesting parts of this system -- the ranking,
the clarification rule, the fallback chain -- are decidable without a model, and
being able to show them running with the network unplugged is the point.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from .fallback import decline_reason_for, resolve
from .matching import build_plan, clarifications
from .models import ItemState
from .outreach import verification_message
from .session import Workspace
from .state import transition
from .text import label, plural
from .vision import identify

DEFAULT_PHOTO = "data/fixtures/pile_01.jpg"
DEFAULT_ORIGIN = (38.9150, -77.0200)      # Washington, DC


def rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n" + "-" * max(24, len(title)))


def run_deterministic(ws: Workspace, photo: str) -> None:
    rule(f"1. The donor sets a radius: {ws.radius_km / 1.609344:.0f} miles")
    print(f"   {len(ws.orgs)} organisations in the corpus.")

    rule("2. Photograph the pile")
    items = identify(photo, ws.vocabulary)
    ws.add(items)
    for item in items:
        seen = item.condition.name.lower() if item.condition else "condition not visible"
        print(f"   {item.quantity} x {label(item.category, item.quantity):<22} {seen}")

    rule("3. Ask only what changes the answer")
    pending = clarifications(items, ws.orgs, ws.origin, ws.radius_km, today=ws.today)
    if not pending:
        print("   Nothing to ask.")
    for question in pending:
        print(f"   ? {question.question}")
        print(f"     at stake: {question.at_stake}")
    print(f"\n   {len(items) - len(pending)} of {len(items)} items needed no question.")

    rule("4. The plan")
    plan = build_plan(items, ws.orgs, ws.origin, ws.radius_km, today=ws.today)
    for n, stop in enumerate(plan.stops, start=1):
        print(f"   Stop {n}: {stop.org.name} -- {stop.org.address}")
        print(f"           {stop.org.hours}")
        for item, units, ev in stop.lines:
            print(f"           {units} x {label(item.category, units)}")
            print(f"             why: {ev.reason}")

    rule("5. No dead ends")
    for item, why in plan.unplaced:
        transition(item, ItemState.DECLINED, reason=decline_reason_for(item), note=why)
        resolution = resolve(item, ws.pathways, today=ws.today)
        transition(item, resolution.next_state, note=resolution.headline)
        mark = "reuse" if resolution.counts_as_recovery else "not counted as recovery"
        print(f"   {resolution.headline}  [{mark}]")
        print(f"     {resolution.detail}")

    rule("6. Aged information is checked, not discounted")
    offers = plan.verification_offers(ws.today)
    if not offers:
        print("   Every fact behind this plan is inside its shelf life.")
    for offer in offers:
        org = next(o for o in ws.orgs if o.id == offer["org_id"])
        print(
            f"   {offer['org']}: {', '.join(plural(c) for c in offer['categories'])} "
            f"last confirmed {offer['days_since_confirmed']} days ago."
        )
        items = next(s for s in plan.stops if s.org.id == org.id).manifest()
        print(f"     -> would email {org.email} and ask. Draft:")
        body = verification_message(org, items, offer["categories"], today=ws.today).body
        for line in body.splitlines():
            print(f"        {line}")
        print("     -> the donor approves it or adds to it before it goes.")

    rule("7. Where it ends")
    from .state import recovery_rate

    print(f"   {recovery_rate(list(ws.items.values()))}")
    print("\n   Recovery counts reuse by a named organisation. Recycling and")
    print("   disposal are reported separately and never folded in.")


def run_agent(ws: Workspace, photo: str) -> None:
    from .agent import build_agent

    agent = build_agent(ws)
    agent(
        f"I have a pile of things to donate. The photo is at {photo}. "
        f"I can travel {ws.radius_km / 1.609344:.0f} miles. Sort it out."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One GiveRight donation run.")
    parser.add_argument("--photo", default=DEFAULT_PHOTO)
    parser.add_argument("--radius", type=float, default=8.0, help="miles")
    parser.add_argument("--lat", type=float, default=DEFAULT_ORIGIN[0])
    parser.add_argument("--lng", type=float, default=DEFAULT_ORIGIN[1])
    parser.add_argument("--today", default=None, help="ISO date, for reproducible demos")
    parser.add_argument("--agent", action="store_true", help="drive it with the model")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="ignore the local observation log, so the run is reproducible",
    )
    args = parser.parse_args(argv)

    # Deliveries and org replies accumulate in data/observations.jsonl and
    # legitimately change the plan. That is correct for real use and unhelpful
    # when recording a demo, so --fresh starts from the curated corpus alone.
    ws = Workspace.open(
        (args.lat, args.lng),
        radius_miles=args.radius,
        observations_path=Path(os.devnull) if args.fresh else None,
    )
    ws.today = date.fromisoformat(args.today) if args.today else date.today()
    if args.fresh:
        print("(--fresh: ignoring local observations; curated corpus only)")

    if not Path(args.photo).exists() and not (
        Path("data/fixtures") / f"{Path(args.photo).stem}.json"
    ).exists():
        print(f"No photo at {args.photo} and no cached identification for it.", file=sys.stderr)
        return 1

    (run_agent if args.agent else run_deterministic)(ws, args.photo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
