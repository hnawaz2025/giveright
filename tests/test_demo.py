"""The demo must keep working without a model, a key, or a network."""

from giveright.demo import main


def test_the_deterministic_walkthrough_runs_end_to_end(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr("giveright.trends.LEDGER_FILE", tmp_path / "ledger.jsonl")
    assert main(["--today", "2026-09-07"]) == 0

    out = capsys.readouterr().out
    assert "The plan" in out
    assert "No dead ends" in out
    assert "not counted as recovery" in out


def test_a_missing_photo_fails_loudly(capsys):
    assert main(["--photo", "data/fixtures/nope.jpg"]) == 1
