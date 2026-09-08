"""Identification caching.

The bug these exist for: phone cameras name every capture `image.jpg`, so a
filename-keyed cache served the second photo the first photo's answer.
"""

import json

from giveright.vision import (
    Pile,
    identify,
    runtime_cache_path,
)


class FakeAgent:
    """Stands in for the model. Returns a scripted pile and counts calls."""

    calls = 0

    def __init__(self, pile):
        self.pile = pile

    def __call__(self, _content):
        FakeAgent.calls += 1
        return type("R", (), {"structured_output": self.pile})()


def write(tmp_path, name, content: bytes):
    p = tmp_path / name
    p.write_bytes(content)
    return p


def patch_model(monkeypatch, pile):
    monkeypatch.setattr(
        "giveright.vision._identify_with_model", lambda p, v, model=None: pile
    )


def test_two_photos_with_the_same_filename_do_not_collide(tmp_path, monkeypatch):
    """The actual reported bug: photographing shoes returned the shirt."""
    monkeypatch.setattr("giveright.vision.RUNTIME_CACHE_DIR", tmp_path / "cache")

    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    first = write(tmp_path / "a", "image.jpg", b"\xff\xd8shirt-bytes")
    second = write(tmp_path / "b", "image.jpg", b"\xff\xd8shoe-bytes")

    patch_model(monkeypatch, Pile(items=[{"category": "shirts", "quantity": 1}]))
    a = identify(first, ["shirts"], use_fixture=False)

    patch_model(monkeypatch, Pile(items=[{"category": "shoes", "quantity": 2}]))
    b = identify(second, ["shoes"], use_fixture=False)

    assert a[0].category == "shirts"
    assert b[0].category == "shoes", "same filename must not serve a stale answer"


def test_the_same_photo_twice_costs_one_model_call(tmp_path, monkeypatch):
    monkeypatch.setattr("giveright.vision.RUNTIME_CACHE_DIR", tmp_path / "cache")
    photo = write(tmp_path, "pile.jpg", b"\xff\xd8identical")

    calls = []

    def once(p, v, model=None):
        calls.append(p)
        return Pile(items=[{"category": "books", "quantity": 3}])

    monkeypatch.setattr("giveright.vision._identify_with_model", once)

    identify(photo, ["books"], use_fixture=False)
    again = identify(photo, ["books"], use_fixture=False)

    assert len(calls) == 1, "re-uploading the same photo must not pay Bedrock twice"
    assert again[0].quantity == 3


def test_uploads_never_write_into_the_committed_fixtures(tmp_path, monkeypatch):
    """data/fixtures is hand-committed development data, like data/orgs."""
    from giveright.vision import FIXTURE_DIR

    monkeypatch.setattr("giveright.vision.RUNTIME_CACHE_DIR", tmp_path / "cache")
    before = {p.name: p.read_text() for p in FIXTURE_DIR.glob("*.json")}

    photo = write(tmp_path, "image.jpg", b"\xff\xd8something-new")
    patch_model(monkeypatch, Pile(items=[{"category": "lamps", "quantity": 1}]))
    identify(photo, ["lamps"], use_fixture=False)

    assert {p.name: p.read_text() for p in FIXTURE_DIR.glob("*.json")} == before
    assert runtime_cache_path(photo).exists()


def test_a_committed_fixture_still_works_without_the_image(tmp_path):
    """pile_01.jpg is gitignored and does not exist; pile_01.json is committed."""
    items = identify("data/fixtures/pile_01.jpg", ["winter_coats"])
    assert [i.category for i in items][:2] == ["winter_coats", "towels"]


def test_the_api_never_reads_the_name_keyed_fixtures(client_photo_named_pile_01=None):
    """A judge uploading a photo they happened to name pile_01.jpg must not be
    shown the demo pile."""
    import inspect

    from giveright import api

    src = inspect.getsource(api.start_run)
    assert "use_fixture=False" in src
