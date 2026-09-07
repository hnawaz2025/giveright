"""Turning a photograph of a pile into a list of items.

This is the one place a model is allowed to tell us something we did not
already know, and even here its output is treated as a *claim about the photo*,
never as a fact about an organisation. It may say "there is a car seat"; it may
not say "the shelter needs car seats".

Two things keep the rest of the system cheap and testable:

  * identification is cached to `data/fixtures/<name>.json`, so the matcher,
    the ranker and the fallback chain are developed and tested against a real
    pile with zero Bedrock calls;
  * the model is asked to choose from the corpus vocabulary, and anything it
    invents is kept verbatim rather than coerced -- an unknown category is a
    real answer that the fallback chain handles, and silently renaming it to
    the nearest known one would route the item wrongly.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from .models import Condition, Item

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "data" / "fixtures"

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

_FORMATS = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp", ".gif": "gif"}

_CONDITION_WORDS = {
    "new": Condition.NEW,
    "good": Condition.GOOD,
    "fair": Condition.FAIR,
    "poor": Condition.POOR,
    "broken": Condition.BROKEN,
}

PROMPT = """\
List every distinct thing in this photograph that someone could donate.

Use these categories where one fits: {vocabulary}.
If nothing fits, write a short snake_case category of your own rather than
forcing it into one of the above -- a wrong category sends the item to the
wrong place.

For each item:
  * quantity: how many you can actually see. Do not estimate what might be
    underneath.
  * condition_hint: only what is visible -- stains, tears, missing parts. Leave
    it null if the photo does not show you. A guess here is worse than a gap,
    because the donor will be asked when it matters.
  * note: anything that changes where it can go (a size, a label, a date stamp
    on a car seat, visible damage).

Do not include rubbish, packaging, or the floor.
"""


class SeenItem(BaseModel):
    category: str = Field(description="snake_case category")
    description: str = Field(default="", description="what it looks like, briefly")
    quantity: int = Field(default=1, ge=1)
    condition_hint: str | None = Field(
        default=None, description="new, good, fair, poor, broken, or null if not visible"
    )
    note: str = Field(default="", description="anything that changes where it can go")


class Pile(BaseModel):
    items: list[SeenItem] = Field(default_factory=list)


def _condition(hint: str | None) -> Condition | None:
    """A hint becomes a condition only if it is unambiguous. Otherwise the donor
    is asked -- but only when the answer changes the routing."""
    if not hint:
        return None
    return _CONDITION_WORDS.get(hint.strip().lower())


def to_items(pile: Pile, *, prefix: str = "item") -> list[Item]:
    items = []
    for n, seen in enumerate(pile.items, start=1):
        attributes = {"note": seen.note} if seen.note else {}
        if seen.condition_hint:
            attributes["condition_hint"] = seen.condition_hint
        items.append(
            Item(
                id=f"{prefix}_{n:02d}",
                category=seen.category.strip().lower().replace(" ", "_"),
                description=seen.description,
                quantity=max(1, seen.quantity),
                condition=_condition(seen.condition_hint),
                attributes=attributes,
            )
        )
    return items


def cache_path(image_path: Path, cache_dir: Path | None = None) -> Path:
    return (cache_dir or FIXTURE_DIR) / f"{Path(image_path).stem}.json"


def load_cached(image_path: Path, cache_dir: Path | None = None) -> list[Item] | None:
    path = cache_path(image_path, cache_dir)
    if not path.exists():
        return None
    return to_items(Pile(**json.loads(path.read_text())), prefix=Path(image_path).stem)


def identify(
    image_path: str | Path,
    vocabulary: list[str],
    *,
    model=None,
    cache_dir: Path | None = None,
    use_cache: bool = True,
) -> list[Item]:
    """Identify a pile, reading the cached answer when there is one.

    Raises rather than inventing a pile if the image is missing and nothing is
    cached -- an empty list here would silently look like "nothing to donate".
    """
    image_path = Path(image_path)

    if use_cache:
        cached = load_cached(image_path, cache_dir)
        if cached is not None:
            return cached

    if not image_path.exists():
        raise FileNotFoundError(
            f"{image_path} is not on disk and no cached identification exists at "
            f"{cache_path(image_path, cache_dir)}"
        )

    pile = _identify_with_model(image_path, vocabulary, model=model)

    path = cache_path(image_path, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pile.model_dump(), indent=2) + "\n")

    return to_items(pile, prefix=image_path.stem)


def _identify_with_model(image_path: Path, vocabulary: list[str], *, model=None) -> Pile:
    """The only Bedrock call in the identification path."""
    from strands import Agent
    from strands.models import BedrockModel

    suffix = image_path.suffix.lower()
    if suffix not in _FORMATS:
        raise ValueError(f"unsupported image type {suffix!r}; use one of {sorted(_FORMATS)}")

    agent = Agent(
        model=model or BedrockModel(model_id=DEFAULT_MODEL_ID),
        system_prompt=(
            "You identify donatable items in photographs. You report only what "
            "is visible. You never speculate about condition you cannot see."
        ),
        structured_output_model=Pile,
    )

    result = agent(
        [
            {"text": PROMPT.format(vocabulary=", ".join(sorted(vocabulary)))},
            {"image": {"format": _FORMATS[suffix],
                       "source": {"bytes": image_path.read_bytes()}}},
        ]
    )

    if result.structured_output is None:
        raise RuntimeError(
            f"the model returned no structured identification for {image_path.name}"
        )
    return result.structured_output
