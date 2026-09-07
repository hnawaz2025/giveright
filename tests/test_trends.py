from datetime import date, timedelta

from giveright.trends import (
    MIN_BASELINE,
    DonationEvent,
    Ledger,
    dashboard,
    gaps,
    trends,
)

from conftest import TODAY, need, org


def events(category, n, day, recovered=True):
    return [
        DonationEvent(
            occurred=day.isoformat(),
            category=category,
            quantity=1,
            outcome="dropped_off",
            org_id="o",
            recovered=recovered,
        )
        for _ in range(n)
    ]


def ledger_with(*groups):
    return Ledger([e for g in groups for e in g], path=None)


LAST_WEEK = TODAY - timedelta(days=9)
THIS_WEEK = TODAY - timedelta(days=2)


def test_a_real_surge_is_reported_as_a_trend():
    led = ledger_with(events("winter_coats", 10, LAST_WEEK),
                      events("winter_coats", 25, THIS_WEEK))

    t = trends(led, today=TODAY)[0]

    assert t.significant
    assert t.change == 1.5
    assert "up 150%" in t.headline


def test_a_thin_baseline_is_not_a_surge():
    """One last week and three this week is noise, not a story."""
    led = ledger_with(events("car_seats", 1, LAST_WEEK),
                      events("car_seats", 3, THIS_WEEK))

    t = trends(led, today=TODAY)[0]

    assert not t.significant
    assert t.change is None
    assert "too few last week" in t.headline
    assert MIN_BASELINE > 1


def test_small_movement_over_a_real_baseline_is_steady_not_a_surge():
    led = ledger_with(events("books", 20, LAST_WEEK), events("books", 22, THIS_WEEK))
    assert not trends(led, today=TODAY)[0].significant


def test_adjacent_windows_do_not_double_count():
    boundary = TODAY - timedelta(days=7)
    led = ledger_with(events("towels", 1, boundary))
    t = trends(led, today=TODAY)[0]
    assert (t.this_period, t.last_period) == (1, 0)


def test_gap_flags_oversupply_so_orgs_can_tell_donors_to_stop():
    stocked = org("stocked", needs=[need("winter_coats", target=50, on_hand=50)])
    led = ledger_with(events("winter_coats", 12, THIS_WEEK))

    g = next(g for g in gaps(led, [stocked], today=TODAY) if g.category == "winter_coats")

    assert g.oversupplied
    assert "hold off" in g.headline


def test_gap_surfaces_a_need_nobody_is_donating_to():
    hungry = org("hungry", needs=[need("car_seats", target=10, on_hand=0)])
    g = gaps(ledger_with(), [hungry], today=TODAY)[0]

    assert (g.donated, g.shortfall, g.orgs_wanting) == (0, 10, 1)
    assert "none donated this week" in g.headline


def test_dashboard_reports_recovery_without_folding_in_recycling():
    led = ledger_with(events("winter_coats", 3, THIS_WEEK, recovered=True),
                      events("mattresses", 1, THIS_WEEK, recovered=False))

    d = dashboard(led, [], today=TODAY)

    assert d["items_routed"] == 4
    assert d["reused_by_an_org"] == 3
    assert d["recovery_rate"] == 0.75


def test_ledger_round_trips_through_the_file(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = Ledger(path=path)
    for e in events("books", 2, THIS_WEEK):
        led.append(e)

    assert [e.category for e in Ledger.load(path).events] == ["books", "books"]
