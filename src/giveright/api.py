"""HTTP surface: one endpoint for a donor, one page both sides read.

There is deliberately nothing here for an organisation to log into. Orgs are
not asked to adopt software, hold an account, or maintain a profile -- the only
thing built for them is the dashboard below, and it is the same dashboard
donors see. When an org needs to correct a fact they do it the way they already
correct facts: they reply to an email, or they answer the phone.

The dashboard is deliberately the same payload for donors and organisations.
A donor learns that coats are pouring in and car seats are not, so the coat in
their hallway is worth less this week than they assumed. An organisation learns
the same thing a week before it arrives, and can say so on the next donation
email or phone call -- which is the only place its needs ever change. The two
sides look at one number, not at two dashboards that disagree.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from .corpus import load_orgs
from .fallback import decline_reason_for, resolve
from .geo import km
from .matching import build_plan
from .models import ItemState
from .outreach import confirmation_message
from .observations import ObservationLog, record_delivery
from .session import Workspace
from .state import transition
from .trends import DonationEvent, Ledger, dashboard
from .vision import identify

app = FastAPI(
    title="GiveRight",
    description=(
        "Matching what a neighbourhood has to give with what local "
        "organisations are actually short of."
    ),
    version="0.1.0",
)


def _workspace(lat: float, lng: float, radius_miles: float) -> Workspace:
    return Workspace.open((lat, lng), radius_miles=radius_miles)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "organisations": len(load_orgs())}


@app.get("/orgs")
def orgs() -> dict:
    """The corpus, with the provenance of every need visible.

    A need shows either the organisation's own confirmation or the page it was
    read from and the date -- because a fact whose age you cannot see is a fact
    you cannot judge.
    """
    today = date.today()
    return {
        "organisations": [
            {
                "id": o.id,
                "name": o.name,
                "address": o.address,
                "hours": o.hours,
                "accepts": sorted(o.accepts),
                "prohibited": o.prohibited,
                "at_capacity": o.at_capacity,
                "email": o.email,
                "needs": [
                    {
                        "category": n.category,
                        "shortfall": n.shortfall,
                        "target": n.target_qty,
                        "on_hand": n.on_hand_estimate,
                        "stock_known": n.stock_known,
                        "delivered_since_verified": n.delivered_since_verified,
                        "confidence": n.confidence(today).value,
                        "last_verified": n.last_verified.isoformat() if n.last_verified else None,
                        "source_url": n.source_url,
                    }
                    for n in o.needs
                ],
            }
            for o in ObservationLog.load().replay(load_orgs())
        ]
    }


@app.post("/donations")
async def donate(
    photo: UploadFile = File(..., description="a photograph of the pile"),
    latitude: float = Form(...),
    longitude: float = Form(...),
    radius_miles: float = Form(5.0),
) -> dict:
    """Photograph in, drop-off plan out.

    The radius is applied before anything else and is never widened server-side.
    Items nobody will take are resolved down the reuse-or-recycle chain rather
    than returned as "no match".
    """
    if radius_miles <= 0:
        raise HTTPException(400, "radius_miles must be greater than zero")

    ws = _workspace(latitude, longitude, radius_miles)

    # The upload keeps its own filename inside a scratch directory rather than
    # getting a random one, because identification is cached under the file's
    # stem. A random name would silently re-run vision on a pile already
    # identified -- a Bedrock call to learn something we already knew.
    filename = Path(photo.filename or "pile.jpg").name
    if Path(filename).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        raise HTTPException(422, f"unsupported image type: {filename}")

    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / filename
        with path.open("wb") as fh:
            shutil.copyfileobj(photo.file, fh)
        try:
            ws.add(identify(path, ws.vocabulary))
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    plan = build_plan(list(ws.items.values()), ws.orgs, ws.origin, ws.radius_km)

    resolved = []
    for item, why in plan.unplaced:
        transition(item, ItemState.DECLINED, reason=decline_reason_for(item), note=why)
        resolution = resolve(item, ws.pathways)
        transition(item, resolution.next_state, note=resolution.headline)
        payload = resolution.as_dict()
        payload["why_no_org_took_it"] = why
        resolved.append(payload)

    body = plan.as_dict()
    body["resolved"] = resolved
    body["messages"] = [
        confirmation_message(stop.org, [i for i, _u, _e in stop.lines]).as_dict()
        for stop in plan.stops
    ]
    return body


@app.get("/dashboard")
def board() -> dict:
    """One page, read by donors and organisations alike."""
    return dashboard(Ledger.load(), ObservationLog.load().replay(load_orgs()))


@app.post("/dropoffs/{category}")
def confirm_dropoff(category: str, quantity: int = 1, org_id: str | None = None) -> dict:
    """Confirm a delivery actually happened.

    Nothing reaches the ledger on the strength of a plan. If people stop
    confirming, the recovery rate falls, which is the correct behaviour for a
    number that is supposed to mean something.
    """
    quantity = max(1, quantity)
    ledger = Ledger.load()
    ledger.append(
        DonationEvent(
            occurred=date.today().isoformat(),
            category=category,
            quantity=quantity,
            outcome=ItemState.DROPPED_OFF.value,
            org_id=org_id,
            recovered=True,
        )
    )

    # A delivery is the one stock fact GiveRight observes first-hand. It is
    # credited against the need so the next donor is not sent to an org this
    # one has already filled -- and kept apart from the org's own figure, so
    # the corpus never puts words in their mouth.
    updated = None
    if org_id:
        log = ObservationLog.load()
        org = next((o for o in log.replay(load_orgs()) if o.id == org_id), None)
        if org is None:
            raise HTTPException(404, f"no organisation {org_id}")
        need = org.need_for(category)
        if need is not None:
            record_delivery(org.id, category, quantity, on=date.today(), log=log)
            need.record_delivery(quantity)
            updated = {"shortfall_now": need.shortfall,
                       "delivered_since_verified": need.delivered_since_verified}

    return {"recorded": True, "category": category, "quantity": quantity,
            "need_updated": updated}
