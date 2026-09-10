"""The audit log. What the agent did, recorded by the framework rather than by
the tools -- a log the tools wrote could only record what they chose to admit."""

import pytest

from giveright.agent import build_agent
from giveright.audit import AuditLog
from giveright.session import Workspace
from stubmodel import StubModel, say, use

from conftest import ORIGIN, TODAY


@pytest.fixture
def ws(tmp_path):
    w = Workspace.open(ORIGIN, radius_miles=8, ledger_path=tmp_path / "ledger.jsonl")
    w.today = TODAY
    return w


@pytest.fixture
def log(tmp_path):
    return AuditLog(path=tmp_path / "audit.jsonl", run="testrun")


def run(ws, log, script):
    build_agent(ws, model=StubModel(script), audit=log)("go")
    return log


def test_every_tool_call_is_recorded_with_its_arguments(ws, log):
    run(ws, log, [use("set_radius", radius_miles=12), say("done")])

    called = next(e for e in log.entries if e.event == "tool_called")
    assert called.tool == "set_radius"
    assert called.inputs == {"radius_miles": "12"}


def test_the_result_is_summarised_not_reproduced(ws, log):
    """A plan is kilobytes of organisation detail already in the corpus. The
    log should say a plan happened, not restate it."""
    run(ws, log, [
        use("identify_pile", image_path="data/fixtures/pile_01.jpg"),
        use("plan_dropoffs"),
        say("done"),
    ])

    returned = [e for e in log.entries if e.event == "tool_returned"]
    plan = next(e for e in returned if e.tool == "plan_dropoffs")
    assert plan.outcome == "ok"
    assert len(plan.detail) < 300
    assert "stops=" in plan.detail


def test_a_failing_tool_is_recorded_as_an_error(ws, log):
    run(ws, log, [use("identify_pile", image_path="nope.jpg"), say("done")])

    returned = next(e for e in log.entries if e.event == "tool_returned")
    assert returned.outcome == "ok"          # the tool returned {"error": ...}
    assert "error:" in returned.detail


def test_a_run_is_bracketed_so_the_boundaries_are_visible(ws, log):
    run(ws, log, [say("nothing to do")])

    kinds = [e.event for e in log.entries]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"


def test_timings_are_recorded(ws, log):
    run(ws, log, [use("set_radius", radius_miles=5), say("done")])
    returned = next(e for e in log.entries if e.event == "tool_returned")
    assert returned.ms is not None and returned.ms >= 0


def test_it_survives_the_process(ws, log, tmp_path):
    run(ws, log, [use("set_radius", radius_miles=5), say("done")])

    reloaded = AuditLog.load(tmp_path / "audit.jsonl")
    assert [e["event"] for e in reloaded][:2] == ["run_started", "tool_called"]


def test_the_timeline_reads_as_a_story(ws, log):
    run(ws, log, [
        use("identify_pile", image_path="data/fixtures/pile_01.jpg"),
        use("plan_dropoffs"),
        say("done"),
    ])

    text = "\n".join(log.timeline())
    assert "→ identify_pile" in text
    assert "→ plan_dropoffs" in text
    assert "✓" in text


def test_long_arguments_are_trimmed_rather_than_dropped(ws, log):
    """A truncated argument still shows what the model was trying to do."""
    run(ws, log, [use("identify_pile", image_path="x" * 900), say("done")])

    called = next(e for e in log.entries if e.event == "tool_called")
    assert called.inputs["image_path"].endswith("…")
    assert len(called.inputs["image_path"]) < 400
