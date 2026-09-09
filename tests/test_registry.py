"""The national registry: who exists and where, and nothing more than that."""

from datetime import date

import pytest

from giveright import registry
from giveright.matching import candidates, evaluate
from giveright.models import Condition, Item, MatchBucket

from conftest import ORIGIN, TODAY, need, org

TODAY_ISO = date(2026, 9, 8).isoformat()


def entry(ein="123", ntee="P20", lat=ORIGIN[0], lng=ORIGIN[1], name="EXAMPLE SHELTER DC"):
    return registry.Entry(
        ein=ein, name=name, street="1 Example St", city="Washington",
        state="DC", zip="20001", ntee=ntee, lat=lat, lng=lng,
        source="IRS Exempt Organizations Business Master File",
        imported_on=TODAY_ISO,
    )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "registry.sqlite"
    conn = registry.connect(path)
    yield path, conn
    conn.close()


def test_a_registry_org_never_claims_a_need(db):
    """The IRS says a shelter exists. It cannot say they are short of coats."""
    o = entry().as_org()
    assert o.needs == []


def test_provenance_names_the_source_and_the_date(db):
    o = entry().as_org()
    assert "IRS Exempt Organizations BMF" in o.verification_source
    assert TODAY_ISO in o.verification_source
    assert "NTEE P20" in o.verification_source


def test_hours_are_left_empty_rather_than_invented(db):
    """The BMF has no opening times, and a wrong one sends someone to a locked
    door."""
    assert entry().as_org().hours == ""


def test_the_ntee_code_decides_what_it_would_accept(db):
    assert set(entry(ntee="K31").as_org().accepts) == {"canned_food"}
    assert "childrens_clothing" in entry(ntee="P30").as_org().accepts
    assert entry(ntee="Z99").as_org().accepts == {}, "unknown classification claims nothing"


def test_an_unclassified_org_declines_rather_than_guesses(db):
    coat = Item(id="c", category="winter_coats", condition=Condition.GOOD)
    ev = evaluate(coat, entry(ntee="Z99").as_org(), ORIGIN, today=TODAY)
    assert ev.bucket is MatchBucket.DECLINED


def test_a_registry_org_ranks_below_one_that_actually_asked(db):
    """Somewhere that would take it must never outrank somewhere that said it
    needs it."""
    asked = org("asked", north_km=6.0, needs=[need("winter_coats", target=40, on_hand=2)])
    listed = entry(ntee="P20", lat=ORIGIN[0] + 0.001).as_org()
    coat = Item(id="c", category="winter_coats", condition=Condition.GOOD)

    ranked = candidates(coat, [asked, listed], ORIGIN, 30.0, today=TODAY)

    assert ranked[0].org_id == "asked"
    assert ranked[1].bucket is MatchBucket.ACCEPTED
    assert "has not listed it as a current need" in ranked[1].reason


def test_only_organisations_inside_the_radius_come_back(db):
    path, conn = db
    registry.save(conn, [
        entry(ein="near", lat=ORIGIN[0] + 0.01, lng=ORIGIN[1]),
        entry(ein="far", lat=ORIGIN[0] + 3.0, lng=ORIGIN[1]),
    ])
    found = registry.near(ORIGIN, 5.0, path=path)
    assert [o.id for o in found] == ["irs_near"]


def test_results_come_back_nearest_first(db):
    path, conn = db
    registry.save(conn, [
        entry(ein="b", lat=ORIGIN[0] + 0.02),
        entry(ein="a", lat=ORIGIN[0] + 0.005),
    ])
    assert [o.id for o in registry.near(ORIGIN, 20.0, path=path)] == ["irs_a", "irs_b"]


def test_a_missing_registry_is_empty_not_an_error(tmp_path):
    """A clone of the repo has no registry until someone builds one."""
    assert registry.near(ORIGIN, 10.0, path=tmp_path / "nope.sqlite") == []


@pytest.mark.parametrize("shouted,readable", [
    ("YMCA OF METROPOLITAN WASHINGTON DC", "YMCA of Metropolitan Washington DC"),
    ("THE HOUSE OF RUTH", "The House of Ruth"),
    ("MARTHAS TABLE INC", "Marthas Table Inc"),
    ("BREAD FOR THE CITY", "Bread for the City"),
])
def test_shouted_irs_names_are_made_readable(shouted, readable, db):
    """Known initialisms survive; nothing else is guessed at."""
    assert entry(name=shouted).display_name == readable


def test_reimporting_replaces_rather_than_duplicates(db):
    path, conn = db
    registry.save(conn, [entry(ein="x", name="OLD NAME")])
    registry.save(conn, [entry(ein="x", name="NEW NAME")])
    found = registry.near(ORIGIN, 10.0, path=path)
    assert len(found) == 1 and found[0].name == "New Name"
