"""Reading .env. A credential loader that surprises people is a liability."""

import os

from giveright.env import load


def write(tmp_path, text):
    p = tmp_path / ".env"
    p.write_text(text)
    return p


def test_it_sets_what_the_file_says(tmp_path, monkeypatch):
    monkeypatch.delenv("GR_PROBE", raising=False)
    load(write(tmp_path, "GR_PROBE=hello\n"))
    assert os.environ["GR_PROBE"] == "hello"


def test_the_real_environment_wins(tmp_path, monkeypatch):
    """Otherwise `KEY=x python ...` would be silently overridden by a file, and
    CI by whatever a developer happened to leave on disk."""
    monkeypatch.setenv("GR_PROBE", "from-the-shell")
    load(write(tmp_path, "GR_PROBE=from-the-file\n"))
    assert os.environ["GR_PROBE"] == "from-the-shell"


def test_comments_blank_lines_and_export_are_handled(tmp_path, monkeypatch):
    monkeypatch.delenv("GR_A", raising=False)
    monkeypatch.delenv("GR_B", raising=False)
    names = load(write(tmp_path, "# a comment\n\nexport GR_A=1\nGR_B=2\nnonsense\n"))
    assert set(names) == {"GR_A", "GR_B"}


def test_quotes_are_stripped_so_a_pasted_key_still_works(tmp_path, monkeypatch):
    monkeypatch.delenv("GR_PROBE", raising=False)
    load(write(tmp_path, 'GR_PROBE="abc123"\n'))
    assert os.environ["GR_PROBE"] == "abc123"


def test_a_value_containing_equals_survives(tmp_path, monkeypatch):
    """Bedrock API keys are base64 and end in padding."""
    monkeypatch.delenv("GR_PROBE", raising=False)
    load(write(tmp_path, "GR_PROBE=abc=def==\n"))
    assert os.environ["GR_PROBE"] == "abc=def=="


def test_a_missing_file_is_not_an_error(tmp_path):
    assert load(tmp_path / "nope.env") == []


def test_it_returns_names_never_values(tmp_path, monkeypatch):
    monkeypatch.delenv("GR_SECRET", raising=False)
    names = load(write(tmp_path, "GR_SECRET=hunter2\n"))
    assert names == ["GR_SECRET"]
    assert "hunter2" not in str(names)
