"""The HTTP surface. The donation endpoint runs against the cached fixture, so
these tests need no model and no credentials either."""

import pytest
from fastapi.testclient import TestClient

from giveright.api import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The API path no longer consults the name-keyed fixtures, so the model is
    stubbed here rather than smuggled in through a cache. Same bytes, same
    result, still no network."""
    import json as _json

    from giveright.vision import Pile

    pile = Pile(**_json.loads(open("data/fixtures/pile_01.json").read()))
    monkeypatch.setattr("giveright.trends.LEDGER_FILE", tmp_path / "ledger.jsonl")
    monkeypatch.setattr("giveright.vision.RUNTIME_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(
        "giveright.vision._identify_with_model", lambda p, v, model=None: pile
    )
    return TestClient(app)


def test_every_need_shown_to_a_donor_carries_its_age(client):
    for org in client.get("/orgs").json()["organisations"]:
        for need in org["needs"]:
            assert need["confidence"] in {"confirmed", "published", "stale"}
            assert need["last_verified"]


def start_run(client, radius=8):
    with open("data/fixtures/pile_01.json", "rb") as fh:
        return client.post(
            "/runs",
            files={"photo": ("pile_01.jpg", fh, "image/jpeg")},
            data={"latitude": 38.9150, "longitude": -77.0200, "radius_miles": radius},
        )


def test_a_run_stops_to_ask_before_it_plans(client):
    """The donor answers before the plan is built, not after -- otherwise the
    question is decoration."""
    body = start_run(client).json()

    assert len(body["items"]) == 6
    assert len(body["questions"]) == 1, "only the towels' condition changes a destination"
    towels = next(i for i in body["items"] if i["category"] == "towels")
    assert body["questions"][0]["item_id"] == towels["item_id"]
    assert body["questions"][0]["at_stake"]
    assert body["run_id"]


def test_the_answer_changes_the_plan(client):
    run = start_run(client).json()
    towels = next(i for i in run["items"] if i["category"] == "towels")["item_id"]

    worn = client.post(f"/runs/{run['run_id']}/plan",
                       json={"answers": {towels: "worn"}}).json()
    stop = next(s for s in worn["stops"]
                if any(i["item_id"] == towels for i in s["items"]))

    assert stop["org_id"] == "dev_eastside_closet"


def test_a_plan_leaves_nothing_unanswered(client):
    run = start_run(client).json()["run_id"]
    body = client.post(f"/runs/{run}/plan", json={"answers": {}}).json()

    assert body["stops"]
    assert len(body["resolved"]) == len(body["unplaced"])
    assert body["messages"], "each stop gets a confirmation email to send"


def test_an_expired_run_says_so_rather_than_failing_oddly(client):
    r = client.post("/runs/nosuchrun/plan", json={"answers": {}})
    assert r.status_code == 404
    assert "expired" in r.json()["detail"]


def test_the_verification_draft_asks_without_changing_anything(client):
    run = start_run(client).json()["run_id"]
    plan = client.post(f"/runs/{run}/plan", json={"answers": {}}).json()
    offer = plan["verification_offers"][0]

    draft = client.post(f"/runs/{run}/verify/{offer['org_id']}").json()

    assert "TAKE THESE" in draft["body"]
    assert "days ago" in draft["body"]
    assert draft["to"]


def test_a_zero_radius_is_rejected_rather_than_quietly_widened(client):
    assert start_run(client, radius=0).status_code == 400


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


def test_orgs_carry_coordinates_so_the_map_can_place_them(client):
    """The radius is only meaningful if you can see who is inside it."""
    for org in client.get("/orgs").json()["organisations"]:
        assert isinstance(org["lat"], float) and isinstance(org["lng"], float)
        assert -90 <= org["lat"] <= 90 and -180 <= org["lng"] <= 180


class TestModelFailures:
    """A photo the model cannot be asked about must fail legibly. "Something
    went wrong (500)" tells the person nothing and hides a fixable cause."""

    def upload(self, client):
        with open("data/fixtures/pile_01.json", "rb") as fh:
            return client.post(
                "/runs",
                files={"photo": ("p.jpg", fh, "image/jpeg")},
                data={"latitude": 38.9, "longitude": -77.0, "radius_miles": 8},
            )

    def raising(self, monkeypatch, exc):
        def boom(*a, **k):
            raise exc
        monkeypatch.setattr("giveright.vision._identify_with_model", boom)

    def test_a_disabled_bedrock_account_says_so(self, client, monkeypatch):
        from botocore.exceptions import ClientError

        self.raising(monkeypatch, ClientError(
            {"Error": {"Code": "ValidationException",
                       "Message": "Error 002: Access to Bedrock models is not "
                                  "allowed for this account"}},
            "ConverseStream"))

        r = self.upload(client)
        assert r.status_code == 503
        assert "not enabled for this AWS account" in r.json()["detail"]
        assert "not a problem with the photo" in r.json()["detail"]

    def test_missing_credentials_say_so(self, client, monkeypatch):
        from botocore.exceptions import NoCredentialsError

        self.raising(monkeypatch, NoCredentialsError())

        r = self.upload(client)
        assert r.status_code == 503
        assert "no AWS credentials" in r.json()["detail"]

    def test_throttling_is_worth_retrying_and_says_so(self, client, monkeypatch):
        from botocore.exceptions import ClientError

        self.raising(monkeypatch, ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "ConverseStream"))

        r = self.upload(client)
        assert r.status_code == 429
        assert "Try that photo again" in r.json()["detail"]

    def test_nothing_is_invented_when_the_model_is_down(self, client, monkeypatch):
        """An empty or made-up pile would be worse than an error."""
        from botocore.exceptions import NoCredentialsError

        self.raising(monkeypatch, NoCredentialsError())
        assert "items" not in self.upload(client).json()


class TestSampleRun:
    """The sample exists so the product can be shown without credentials. It
    must be the real system downstream, and must never pretend to be a photo."""

    def test_it_runs_the_whole_flow_with_no_model(self, client):
        run = client.post("/runs/sample", params={"radius_miles": 8}).json()

        assert run["sample"] is True
        assert "sample pile" in run["note"].lower()
        assert len(run["items"]) == 6
        assert len(run["questions"]) == 1

        plan = client.post(f"/runs/{run['run_id']}/plan", json={"answers": {}}).json()
        assert plan["stops"] and plan["resolved"]

    def test_it_is_the_same_matcher_as_a_real_photo(self, client):
        """Only identification is pre-computed. If the sample diverged from the
        real path it would be a demo of something that does not exist."""
        sample = client.post("/runs/sample", params={"radius_miles": 8}).json()
        real = start_run(client).json()

        assert ([i["category"] for i in sample["items"]]
                == [i["category"] for i in real["items"]])

    def test_a_zero_radius_is_still_refused(self, client):
        assert client.post("/runs/sample", params={"radius_miles": 0}).status_code == 400


def test_organisations_far_from_the_pin_are_not_returned(client):
    """Dragging the pin to another state must not keep plotting Washington."""
    dc = client.get("/orgs", params={"latitude": 38.9150, "longitude": -77.0200,
                                     "radius_miles": 10}).json()
    la = client.get("/orgs", params={"latitude": 34.0522, "longitude": -118.2437,
                                     "radius_miles": 10}).json()

    assert dc["counts"]["curated"] == 3
    assert la["counts"]["curated"] == 0
    assert la["organisations"] == []
