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
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import registry
from .corpus import load_orgs
from .fallback import decline_reason_for, resolve
from .geo import haversine_km, km
from .limits import RateLimiter
from .llm import using_api_key
from .matching import apply_answers, build_plan, clarifications
from .models import ItemState
from .outreach import confirmation_message, verification_message
from .observations import ObservationLog, record_delivery
from .session import Workspace
from .state import transition
from .text import plural
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


STATIC = Path(__file__).resolve().parent / "static"
# Guards the one endpoint that spends money. See limits.py for why there are
# two limits rather than one.
LIMITER = RateLimiter()


def _client(request: Request) -> str:
    """Who to charge a request to. Behind a proxy the socket is the proxy, so
    the forwarded address is preferred where present -- spoofable, but this is
    a spend guard rather than an access control."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


MAP_LIMIT = 300   # plotted; the true count in radius is reported alongside
SAMPLE_PILE = Path(__file__).resolve().parents[2] / "data" / "fixtures" / "pile_01.jpg"


@app.get("/", include_in_schema=False)
def ui() -> FileResponse:
    """The donor-facing app. Mobile first, because the premise is photographing
    a pile in your hallway -- `capture="environment"` opens the phone camera."""
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "curated_organisations": len(load_orgs()),
        "registry_organisations": registry.count(),
        "auth": "bedrock api key" if using_api_key() else "aws credentials",
        "identifications_this_hour": LIMITER.used_this_hour(),
        "hourly_ceiling": LIMITER.hourly_ceiling,
    }


@app.get("/orgs")
def orgs(
    latitude: float | None = None,
    longitude: float | None = None,
    radius_miles: float = 5.0,
) -> dict:
    """The corpus, with the provenance of every need visible.

    A need shows either the organisation's own confirmation or the page it was
    read from and the date -- because a fact whose age you cannot see is a fact
    you cannot judge.
    """
    today = date.today()
    curated = ObservationLog.load().replay(load_orgs())

    # Without a location this is just the hand-curated corpus. With one, every
    # registry organisation inside the radius comes too -- that is the whole
    # point of the registry, and it is why the map can show a donor anywhere in
    # the country who is actually near them.
    shown_all = True
    if latitude is not None and longitude is not None:
        origin, radius_km = (latitude, longitude), km(radius_miles)

        # Curated organisations are filtered by the radius too. They used to be
        # returned wherever the pin was, which plotted three Washington
        # fixtures on a map of Los Angeles.
        curated = [
            o for o in curated
            if haversine_km(latitude, longitude, o.lat, o.lng) <= radius_km
        ]
        known = {o.id for o in curated}
        nearby = registry.near(origin, radius_km, limit=MAP_LIMIT)
        found = curated + [o for o in nearby if o.id not in known]
        in_radius = registry.count_near(origin, radius_km)
        shown_all = len(nearby) >= in_radius
    else:
        found = curated
        in_radius = 0

    return {
        "counts": {
            "curated": len(curated),
            "registry": len(found) - len(curated),
            "registry_in_radius": in_radius,
            "showing_all": shown_all,
            "registry_total": registry.count(),
        },
        "organisations": [
            {
                "id": o.id,
                "name": o.name,
                "source": "registry" if o.id.startswith("irs_") else "curated",
                "verification_source": o.verification_source,
                "lat": o.lat,
                "lng": o.lng,
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
            for o in found
        ]
    }


def _model_failure(exc: Exception) -> HTTPException:
    """Turn a Bedrock failure into something the person looking at it can act on.

    An unhandled botocore error becomes a bare 500 and "something went wrong",
    which is true and useless. These are the failures this project has actually
    hit, each with the thing to go and do about it. Identification is never
    faked when the model is unavailable -- an invented pile would be worse than
    an error.
    """
    from botocore.exceptions import ClientError, NoCredentialsError

    if isinstance(exc, NoCredentialsError):
        return HTTPException(
            503,
            "This server has no AWS credentials, so it cannot look at photos. "
            "Set them and restart.",
        )

    if isinstance(exc, ClientError):
        message = exc.response.get("Error", {}).get("Message", str(exc))
        code = exc.response.get("Error", {}).get("Code", "")

        if "not allowed for this account" in message:
            return HTTPException(
                503,
                "Amazon Bedrock is not enabled for this AWS account, so photos "
                "cannot be identified. This is an account-level setting, not a "
                "problem with the photo.",
            )
        if "use case details" in message:
            return HTTPException(
                503,
                "This model needs its one-time use case form submitted for the "
                "account before it can be called.",
            )
        if "being verified" in message:
            return HTTPException(
                503,
                "The AWS account is still being verified, which usually takes "
                "under two hours. Nothing is wrong with the photo.",
            )
        if code in ("ThrottlingException", "TooManyRequestsException"):
            return HTTPException(429, "The model is busy. Try that photo again.")

        return HTTPException(503, f"The model could not be reached ({code}).")

    return HTTPException(503, "The model could not be reached.")


_SESSIONS: dict[str, Workspace] = {}

# A demo holds runs in memory. One process, no eviction: fine for a single
# instance and a live demo, wrong for anything real -- a second instance would
# not find the session, and a restart drops them all.
MAX_SESSIONS = 64


def _remember(ws: Workspace) -> str:
    if len(_SESSIONS) >= MAX_SESSIONS:
        _SESSIONS.pop(next(iter(_SESSIONS)))
    run_id = uuid4().hex[:12]
    _SESSIONS[run_id] = ws
    return run_id


def _session(run_id: str) -> Workspace:
    ws = _SESSIONS.get(run_id)
    if ws is None:
        raise HTTPException(404, "that run has expired -- start again from the photo")
    return ws


@app.post("/runs")
async def start_run(
    request: Request,
    photo: UploadFile = File(..., description="a photograph of the pile"),
    latitude: float = Form(...),
    longitude: float = Form(...),
    radius_miles: float = Form(5.0),
) -> dict:
    """Photograph in, items and any questions out.

    Stops before planning, because the donor may need to answer something first.
    Only items whose condition would change their destination produce a
    question -- everything else is decided without asking.
    """
    if radius_miles <= 0:
        raise HTTPException(400, "radius_miles must be greater than zero")

    refused = LIMITER.check(_client(request))
    if refused:
        raise HTTPException(429, refused)

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
            ws.add(identify(path, ws.vocabulary, use_fixture=False))
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 -- narrowed inside the helper
            raise _model_failure(exc) from exc

    questions = clarifications(
        list(ws.items.values()), ws.orgs, ws.origin, ws.radius_km
    )
    return {
        "run_id": _remember(ws),
        "items": [_item_payload(i) for i in ws.items.values()],
        "questions": [q.as_dict() for q in questions],
    }


def _item_payload(i) -> dict:
    return {
        "item_id": i.id,
        "category": i.category,
        "label": plural(i.category),
        "description": i.description,
        "quantity": i.quantity,
        "condition": i.condition.name.lower() if i.condition else None,
        "note": i.attributes.get("note", ""),
    }


@app.post("/runs/sample")
def start_sample_run(
    latitude: float = 38.9150,
    longitude: float = -77.0200,
    radius_miles: float = 5.0,
) -> dict:
    """The same run, from a committed sample pile instead of a camera.

    Everything after identification is identical -- the same matcher, the same
    clarification rule, the same fallback chain, the same organisations. Only
    the photograph is pre-identified, from `data/fixtures/pile_01.json`, which
    is why this works with no credentials and no model call.

    It exists so the product can be shown when Bedrock is unavailable, and so
    anyone can see the whole flow without owning an AWS account. The response
    says plainly that it is a sample; nothing here is presented as a real
    photograph.
    """
    if radius_miles <= 0:
        raise HTTPException(400, "radius_miles must be greater than zero")

    ws = _workspace(latitude, longitude, radius_miles)
    ws.add(identify(SAMPLE_PILE, ws.vocabulary))

    questions = clarifications(
        list(ws.items.values()), ws.orgs, ws.origin, ws.radius_km
    )
    return {
        "run_id": _remember(ws),
        "sample": True,
        "note": (
            "A sample pile, identified in advance. Everything after this point "
            "is the real system."
        ),
        "items": [_item_payload(i) for i in ws.items.values()],
        "questions": [q.as_dict() for q in questions],
    }


class Answers(BaseModel):
    """Condition answers, keyed by item id. Free text -- donors say "a bit worn",
    not "POOR"."""

    answers: dict[str, str] = Field(default_factory=dict)


@app.post("/runs/{run_id}/plan")
def make_plan(run_id: str, body: Answers) -> dict:
    """Answer the questions and get the drop-off plan.

    Every unplaced item is resolved down the reuse-or-recycle chain here, so
    nothing comes back as "no match found".
    """
    ws = _session(run_id)

    items = list(ws.items.values())
    apply_answers(
        items,
        clarifications(items, ws.orgs, ws.origin, ws.radius_km),
        body.answers,
    )

    plan = build_plan(items, ws.orgs, ws.origin, ws.radius_km)
    ws.plan = plan

    resolved = []
    for item, why in plan.unplaced:
        transition(item, ItemState.DECLINED, reason=decline_reason_for(item), note=why)
        resolution = resolve(item, ws.pathways)
        transition(item, resolution.next_state, note=resolution.headline)
        payload = resolution.as_dict()
        payload["why_no_org_took_it"] = why
        resolved.append(payload)

    body_out = plan.as_dict()
    body_out["run_id"] = run_id
    body_out["resolved"] = resolved
    body_out["messages"] = [
        confirmation_message(stop.org, stop.manifest()).as_dict()
        for stop in plan.stops
    ]
    return body_out


@app.post("/runs/{run_id}/verify/{org_id}")
def draft_verification(run_id: str, org_id: str) -> dict:
    """The email that asks whether an aged need is still live.

    Returned as a draft for the donor to approve or add to. Nothing is sent by
    this endpoint, and nothing about the corpus changes by asking.
    """
    ws = _session(run_id)
    org = ws.org(org_id)
    if org is None or ws.plan is None:
        raise HTTPException(404, f"no organisation {org_id} on this run")

    stop = next((s for s in ws.plan.stops if s.org.id == org_id), None)
    if stop is None:
        raise HTTPException(404, f"{org_id} is not on the plan")

    aged = stop.aged_categories()
    message = verification_message(org, stop.manifest(), aged or [
        i.category for i in stop.manifest()
    ])
    return {"to": org.email, **message.as_dict()}


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
