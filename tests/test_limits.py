"""Rate limiting on the endpoint that spends money."""

import pytest
from fastapi.testclient import TestClient

from giveright.api import app
from giveright.limits import RateLimiter


@pytest.fixture
def client(tmp_path, monkeypatch):
    import json as _json

    from giveright.vision import Pile

    pile = Pile(**_json.loads(open("data/fixtures/pile_01.json").read()))
    monkeypatch.setattr("giveright.vision.RUNTIME_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(
        "giveright.vision._identify_with_model", lambda p, v, model=None: pile
    )
    return TestClient(app)


def test_a_normal_burst_is_allowed():
    rl = RateLimiter(per_minute=6, burst=3)
    assert [rl.check("a", now=0) for _ in range(3)] == [None, None, None]


def test_a_fourth_photo_in_the_same_instant_is_refused():
    rl = RateLimiter(per_minute=6, burst=3)
    for _ in range(3):
        rl.check("a", now=0)
    why = rl.check("a", now=0)
    assert why is not None and "Try again in" in why


def test_the_bucket_refills_over_time():
    rl = RateLimiter(per_minute=6, burst=3)
    for _ in range(3):
        rl.check("a", now=0)

    assert rl.check("a", now=5) is not None, "half a token is not a token"
    assert rl.check("a", now=10) is None, "at six a minute, one is back after ten seconds"


def test_one_client_cannot_exhaust_another():
    rl = RateLimiter(per_minute=6, burst=1)
    assert rl.check("a", now=0) is None
    assert rl.check("a", now=0) is not None
    assert rl.check("b", now=0) is None, "b is unaffected by a"


def test_the_hourly_ceiling_stops_a_crowd():
    """Per-client limits do nothing about a hundred clients. This is the limit
    that stands between a shared demo link and a bill."""
    rl = RateLimiter(per_minute=600, burst=10, hourly_ceiling=5)

    allowed = sum(1 for i in range(20) if rl.check(f"client{i}", now=0) is None)

    assert allowed == 5
    assert "hourly limit" in rl.check("someone-else", now=0)


def test_the_ceiling_is_a_rolling_hour_not_a_calendar_one():
    rl = RateLimiter(per_minute=600, burst=10, hourly_ceiling=2)
    rl.check("a", now=0)
    rl.check("a", now=0)
    assert rl.check("a", now=100) is not None
    assert rl.check("a", now=3700) is None, "the first hour has rolled off"


def test_a_refused_request_does_not_consume_the_budget():
    """Otherwise being rate limited would make the ceiling arrive sooner."""
    rl = RateLimiter(per_minute=6, burst=1, hourly_ceiling=100)
    rl.check("a", now=0)
    for _ in range(5):
        rl.check("a", now=0)

    assert rl.used_this_hour(now=0) == 1


class TestTheEndpoint:
    """Only the endpoint that calls a model is limited. Reading organisations
    is a bounded query and must not be rationed."""

    def upload(self, client):
        with open("data/fixtures/pile_01.json", "rb") as fh:
            return client.post(
                "/runs",
                files={"photo": ("p.jpg", fh, "image/jpeg")},
                data={"latitude": 38.9, "longitude": -77.0, "radius_miles": 8},
            )

    def test_a_flood_of_photos_is_refused_with_429(self, client, monkeypatch):
        from giveright import api

        monkeypatch.setattr(api, "LIMITER", RateLimiter(per_minute=1, burst=2))

        codes = [self.upload(client).status_code for _ in range(4)]

        assert codes[:2] == [200, 200]
        assert codes[2:] == [429, 429]
        assert "Try again" in self.upload(client).json()["detail"]

    def test_reading_organisations_is_never_rationed(self, client, monkeypatch):
        from giveright import api

        monkeypatch.setattr(api, "LIMITER", RateLimiter(per_minute=1, burst=0))

        assert client.get("/orgs").status_code == 200
        assert client.get("/dashboard").status_code == 200

    def test_the_sample_run_costs_nothing_and_is_not_limited(self, client, monkeypatch):
        """It calls no model, so rationing it would only stop people looking."""
        from giveright import api

        monkeypatch.setattr(api, "LIMITER", RateLimiter(per_minute=1, burst=0))

        assert client.post("/runs/sample", params={"radius_miles": 8}).status_code == 200

    def test_health_reports_the_budget(self, client):
        body = client.get("/health").json()
        assert "identifications_this_hour" in body
        assert body["hourly_ceiling"] > 0
