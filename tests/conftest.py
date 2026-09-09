"""Builders for the matching tests.

Distances are constructed by nudging latitude: 0.01 degrees is roughly 1.1 km,
which is precise enough to test ordering without pinning the tests to a
particular haversine constant.
"""

from datetime import date

import pytest

from giveright.models import Condition, Item, Need, Org

ORIGIN = (38.9000, -77.0000)
TODAY = date(2026, 9, 7)


def org(id, *, north_km=1.0, accepts=None, needs=(), **kw):
    return Org(
        id=id,
        name=kw.pop("name", id.replace("_", " ").title()),
        lat=ORIGIN[0] + north_km / 111.0,
        lng=ORIGIN[1],
        address=kw.pop("address", f"{id} address"),
        hours=kw.pop("hours", "Mon-Fri 9:00-17:00"),
        accepts=accepts if accepts is not None else {"winter_coats": Condition.FAIR},
        needs=list(needs),
        **kw,
    )


def need(category="winter_coats", target=50, on_hand=0,
         verified=TODAY, confirmed=True, shelf_life=14):
    return Need(
        category=category,
        target_qty=target,
        on_hand_estimate=on_hand,
        last_verified=verified,
        confirmed_by_org=confirmed,
        shelf_life_days=shelf_life,
    )


def item(id="i1", category="winter_coats", condition=Condition.GOOD, qty=1,
         alternatives=None, **kw):
    return Item(id=id, category=category, condition=condition, quantity=qty,
                alternatives=list(alternatives or []), **kw)


@pytest.fixture(autouse=True)
def never_touch_the_real_data_directory(tmp_path, monkeypatch):
    """No test may write into `data/`.

    An earlier version of the delivery path wrote back to the curated corpus,
    and one test run silently replaced a hand-sourced YAML file with a machine
    dump. Machine writes now go to append-only logs, and this redirects those
    logs so a test cannot reach the repository's own data either way.
    """
    monkeypatch.setattr("giveright.observations.OBSERVATIONS_FILE",
                        tmp_path / "observations.jsonl")
    monkeypatch.setattr("giveright.trends.LEDGER_FILE", tmp_path / "ledger.jsonl")
    monkeypatch.setattr("giveright.watch.HELD_FILE", tmp_path / "held.jsonl")
    monkeypatch.setattr("giveright.vision.RUNTIME_CACHE_DIR", tmp_path / "cache")


@pytest.fixture
def origin():
    return ORIGIN


@pytest.fixture
def today():
    return TODAY
