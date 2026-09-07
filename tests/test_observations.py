"""The curated corpus is read-only. Everything the agent learns goes here."""

from datetime import date, timedelta

from giveright.corpus import DATA_DIR, load_orgs
from giveright.observations import (
    ObservationLog,
    record_delivery,
    record_reply,
)

TODAY = date(2026, 9, 7)


def log(tmp_path):
    return ObservationLog(path=tmp_path / "observations.jsonl")


def shelter(orgs):
    return next(o for o in orgs if o.id == "dev_northgate_shelter")


def test_a_delivery_reduces_what_the_next_donor_is_told(tmp_path):
    lg = log(tmp_path)
    record_delivery("dev_northgate_shelter", "winter_coats", 6, on=TODAY, log=lg)

    need = shelter(lg.replay(load_orgs())).need_for("winter_coats")

    assert need.shortfall == 42                 # was 48
    assert need.on_hand_estimate == 2, "the org's own figure is left alone"
    assert need.delivered_since_verified == 6


def test_replaying_the_same_log_twice_gives_the_same_corpus(tmp_path):
    lg = log(tmp_path)
    record_delivery("dev_northgate_shelter", "winter_coats", 6, on=TODAY, log=lg)

    first = shelter(lg.replay(load_orgs())).need_for("winter_coats").shortfall
    second = shelter(lg.replay(load_orgs())).need_for("winter_coats").shortfall

    assert first == second == 42


def test_a_delivery_the_curator_already_counted_is_not_counted_again(tmp_path):
    """The shelter's figure is dated 2026-09-06. A drop-off on the 5th was
    already in the number they gave us."""
    lg = log(tmp_path)
    record_delivery("dev_northgate_shelter", "winter_coats", 6,
                    on=date(2026, 9, 5), log=lg)

    assert shelter(lg.replay(load_orgs())).need_for("winter_coats").shortfall == 48


def test_a_reply_is_replayed_onto_the_corpus(tmp_path):
    lg = log(tmp_path)
    record_reply("dev_northgate_shelter", "dont_take", ["winter_coats"],
                 on=TODAY, log=lg)

    org = shelter(lg.replay(load_orgs()))

    assert "winter_coats" in org.prohibited
    assert "winter_coats" not in org.accepts


def test_the_log_round_trips_through_the_file(tmp_path):
    path = tmp_path / "observations.jsonl"
    lg = ObservationLog(path=path)
    record_delivery("dev_northgate_shelter", "winter_coats", 3, on=TODAY, log=lg)
    record_reply("dev_riverside_pantry", "full", ["towels"], on=TODAY, log=lg)

    kinds = [e.kind for e in ObservationLog.load(path).entries]
    assert kinds == ["delivery", "reply"]


def test_an_observation_about_an_unknown_org_is_ignored_not_fatal(tmp_path):
    lg = log(tmp_path)
    record_delivery("org_that_left_the_corpus", "winter_coats", 3, on=TODAY, log=lg)
    assert lg.replay(load_orgs())


def test_nothing_here_writes_to_the_curated_corpus(tmp_path):
    """The provenance comments in those YAML files are part of the record."""
    before = {p.name: p.read_text() for p in DATA_DIR.glob("*.yaml")}

    lg = log(tmp_path)
    record_delivery("dev_northgate_shelter", "winter_coats", 6, on=TODAY, log=lg)
    record_reply("dev_northgate_shelter", "dont_take", ["towels"], on=TODAY, log=lg)
    lg.replay(load_orgs())

    assert {p.name: p.read_text() for p in DATA_DIR.glob("*.yaml")} == before
