from datetime import date

from giveright.corpus import load_orgs
from giveright.models import Condition, Confidence


def test_dev_corpus_loads():
    orgs = load_orgs()
    assert {o.id for o in orgs} == {
        "dev_eastside_closet",
        "dev_northgate_shelter",
        "dev_riverside_pantry",
    }


def test_conditions_and_prohibitions_parse():
    pantry = next(o for o in load_orgs() if o.id == "dev_riverside_pantry")
    assert pantry.accepts["towels"] is Condition.FAIR
    assert "car_seats" in pantry.prohibited


def test_shortfall_drives_ranking_not_category_match():
    """A towel is worth more to the pantry (34 short) than the shelter (1 short)."""
    orgs = {o.id: o for o in load_orgs()}
    pantry = orgs["dev_riverside_pantry"].need_for("towels")
    shelter = orgs["dev_northgate_shelter"].need_for("towels")
    assert pantry.shortfall == 34
    assert shelter.shortfall == 1
    assert pantry.shortfall > shelter.shortfall


def test_confidence_reflects_source_and_age():
    orgs = {o.id: o for o in load_orgs()}
    today = date(2026, 9, 7)
    confirmed = orgs["dev_riverside_pantry"].need_for("towels")
    old = orgs["dev_northgate_shelter"].need_for("towels")
    assert confirmed.confidence(today) is Confidence.CONFIRMED
    assert old.confidence(today) is Confidence.STALE


def test_every_fact_a_donor_acts_on_carries_a_provenance():
    """A need is either confirmed by the org or traceable to a page and a date.
    An untraceable need is worse than a missing one, because it looks certain."""
    for org in load_orgs():
        for need in org.needs:
            assert need.confirmed_by_org or need.source_url, (
                f"{org.id}/{need.category} has neither confirmation nor a source"
            )
            assert need.last_verified is not None, f"{org.id}/{need.category} undated"


def test_every_org_has_somewhere_to_write_to():
    """Email is the only channel the agent uses, so an org without one cannot be
    asked whether an aged need is still live."""
    for org in load_orgs():
        assert org.email, f"{org.id} has no email address"
