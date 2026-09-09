"""Turning a photograph of a pile into a list of items.

This is the one place a model is allowed to tell us something we did not
already know, and even here its output is treated as a *claim about the photo*,
never as a fact about an organisation. It may say "there is a car seat"; it may
not say "the shelter needs car seats".

Two things keep the rest of the system cheap and testable:

  * identification is cached, so the matcher, the ranker and the fallback chain
    are developed and tested against a real pile with zero Bedrock calls. There
    are two caches and the difference matters: `data/fixtures/<name>.json` is
    committed and keyed by filename, for development; the runtime cache is
    keyed by a hash of the image bytes and is gitignored. Keying a live upload
    by filename was a real bug -- phone cameras name every capture `image.jpg`,
    so a second photo was served the first photo's answer;
  * the model is asked to choose from the corpus vocabulary, and anything it
    invents is kept verbatim rather than coerced -- an unknown category is a
    real answer that the fallback chain handles, and silently renaming it to
    the nearest known one would route the item wrongly.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, Field

from .llm import VISION_MODEL_ID, bedrock
from .models import Condition, Item

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "data" / "fixtures"

# Gitignored. Answers about real uploads, keyed by the image's own bytes.
RUNTIME_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"

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

**Never infer who something belongs to.** A photograph does not show you the
age, size or gender of the person a garment is for. Do not call a shirt
children's clothing because it looks small, or menswear because it looks plain.
Choose a narrow category like that only when the photograph contains actual
evidence -- a readable size label, a print or cut that is unambiguously for a
small child, a recognisable school uniform. Otherwise use the plain category
for the thing itself: `shirts`, `trousers`, `shoes`, `coats`. A broader category
that is true beats a narrower one that is a guess, because the donor is asked
about anything that turns out to matter.

The same rule applies to everything you cannot see. Report the object, not the
story around it.

For each item:
  * category: what the thing is, at the narrowest level the photograph actually
    supports.
  * quantity: how many you can actually see. Do not estimate what might be
    underneath.
  * condition_hint: only what is visible -- stains, tears, missing parts, or
    obvious newness like an attached tag. Leave it null if the photo does not
    show you, which is the usual case. A guess here is worse than a gap,
    because the donor is asked whenever the answer would change the plan.
  * alternatives: when the photograph would equally support two of the
    categories above and you cannot tell which -- a shirt that might be for a
    child or an adult -- put the plain object in `category` and list those
    corpus categories here. The donor is asked, but only if the answer would
    send the item somewhere different. Leave it empty when the photo does tell
    you.
  * note: anything visible that changes where it can go -- a size label you can
    actually read, a date stamp on a car seat, visible damage. Leave it empty
    rather than filling it with an impression.

Do not include rubbish, packaging, or the floor.
"""


class SeenItem(BaseModel):
    category: str = Field(description="snake_case category")
    alternatives: list[str] = Field(
        default_factory=list,
        description=(
            "corpus categories this could equally be, when the photograph "
            "cannot settle it; empty when it can"
        ),
    )
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
                alternatives=[
                    a.strip().lower().replace(" ", "_")
                    for a in seen.alternatives
                    if a.strip()
                ],
                description=seen.description,
                quantity=max(1, seen.quantity),
                condition=_condition(seen.condition_hint),
                attributes=attributes,
            )
        )
    return items


def cache_path(image_path: Path, cache_dir: Path | None = None) -> Path:
    """Where a committed development fixture lives, keyed by filename."""
    return (cache_dir or FIXTURE_DIR) / f"{Path(image_path).stem}.json"


def _digest(image_path: Path) -> str:
    return hashlib.sha256(image_path.read_bytes()).hexdigest()[:16]


def runtime_cache_path(image_path: Path) -> Path:
    """Where a real upload's answer lives, keyed by the image's own bytes.

    Filenames cannot be trusted: every iPhone camera capture arrives as
    `image.jpg`. Two different photos must never collide, and re-uploading the
    same photo should not cost a second Bedrock call.
    """
    return RUNTIME_CACHE_DIR / f"{_digest(image_path)}.json"


def load_cached(image_path: Path, cache_dir: Path | None = None) -> list[Item] | None:
    """A committed fixture, by name. The image itself need not exist on disk."""
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
    use_fixture: bool = True,
) -> list[Item]:
    """Identify a pile, reading the cached answer when there is one.

    Raises rather than inventing a pile if the image is missing and nothing is
    cached -- an empty list here would silently look like "nothing to donate".
    """
    image_path = Path(image_path)

    # Committed development fixtures, by filename. Never consulted for a real
    # upload: a photo that happened to be called pile_01.jpg would otherwise be
    # answered with the demo pile.
    if use_fixture:
        cached = load_cached(image_path, cache_dir)
        if cached is not None:
            return cached

    if not image_path.exists():
        raise FileNotFoundError(
            f"{image_path} is not on disk and no cached identification exists at "
            f"{cache_path(image_path, cache_dir)}"
        )

    # From here the answer is about *these bytes*, so everything is keyed on
    # them. Nothing is written into the committed fixtures directory.
    runtime = runtime_cache_path(image_path)
    prefix = _digest(image_path)

    if use_cache and runtime.exists():
        return to_items(Pile(**json.loads(runtime.read_text())), prefix=prefix)

    pile = _identify_with_model(image_path, vocabulary, model=model)

    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text(json.dumps(pile.model_dump(), indent=2) + "\n")

    return to_items(pile, prefix=prefix)


def _identify_with_model(image_path: Path, vocabulary: list[str], *, model=None) -> Pile:
    """The only Bedrock call in the identification path."""
    from strands import Agent

    suffix = image_path.suffix.lower()
    if suffix not in _FORMATS:
        raise ValueError(f"unsupported image type {suffix!r}; use one of {sorted(_FORMATS)}")

    agent = Agent(
        model=model or bedrock(VISION_MODEL_ID),
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
