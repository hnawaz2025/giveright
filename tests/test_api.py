"""The HTTP surface. The donation endpoint runs against the cached fixture, so
these tests need no model and no credentials either."""

import pytest
from fastapi.testclient import TestClient

from giveright.api import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("giveright.trends.LEDGER_FILE", tmp_path / "ledger.jsonl")
    return TestClient(app)


def test_every_need_shown_to_a_donor_carries_its_age(client):
    for org in client.get("/orgs").json()["organisations"]:
        for need in org["needs"]:
            assert need["confidence"] in {"confirmed", "published", "stale"}
            assert need["last_verified"]


def test_a_donation_returns_a_plan_and_leaves_nothing_unanswered(client):
    with open("data/fixtures/pile_01.json", "rb") as fh:
        response = client.post(
            "/donations",
            files={"photo": ("pile_01.jpg", fh, "image/jpeg")},
            data={"latitude": 38.9150, "longitude": -77.0200, "radius_miles": 8},
        )

    body = response.json()
    assert response.status_code == 200
    assert body["stops"]
    assert len(body["resolved"]) == len(body["unplaced"])
    assert body["messages"], "each stop gets a one-tap confirmation to send"


def test_a_zero_radius_is_rejected_rather_than_quietly_widened(client):
    with open("data/fixtures/pile_01.json", "rb") as fh:
        response = client.post(
            "/donations",
            files={"photo": ("pile_01.jpg", fh, "image/jpeg")},
            data={"latitude": 38.9, "longitude": -77.0, "radius_miles": 0},
        )
    assert response.status_code == 400


def test_the_dashboard_only_counts_confirmed_drop_offs(client):
    assert client.get("/dashboard").json()["items_routed"] == 0

    client.post("/dropoffs/winter_coats", params={"quantity": 3, "org_id": "dev_northgate_shelter"})

    board = client.get("/dashboard").json()
    assert board["items_routed"] == 3
    assert board["reused_by_an_org"] == 3


def test_there_is_nothing_for_an_organisation_to_log_into(client):
    """The org side is a scope decision, not an oversight: no account, no
    profile, no write endpoint. An org's only surface is the shared dashboard."""
    paths = {r.path for r in app.routes}
    assert not [p for p in paths if p.startswith("/orgs/")]
    assert "/dashboard" in paths
