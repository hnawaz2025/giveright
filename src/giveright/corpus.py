"""Loading the organization corpus.

The corpus is hand-curated and every fact carries a source and a date, so the
UI can show the difference between "the org told us on Tuesday" and "this was
on their website in August". That distinction is the product, not a caveat.

There is deliberately no writer here. Machine-observed facts -- deliveries,
replies -- go to `observations.py` and are replayed on top at load time. An
earlier version wrote back to these files and a single test run replaced a
sourced, commented YAML file with a machine dump.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from .models import Condition, Need, Org

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "orgs"

_CONDITIONS = {c.name.lower(): c for c in Condition}


def _condition(value: str | int) -> Condition:
    if isinstance(value, int):
        return Condition(value)
    key = str(value).strip().lower()
    if key not in _CONDITIONS:
        raise ValueError(f"unknown condition {value!r}; use one of {sorted(_CONDITIONS)}")
    return _CONDITIONS[key]


def _need(raw: dict) -> Need:
    verified = raw.get("last_verified")
    if isinstance(verified, str):
        verified = date.fromisoformat(verified)
    return Need(
        category=raw["category"],
        target_qty=int(raw["target_qty"]),
        # Absent means nobody has said, which is not the same fact as zero.
        on_hand_estimate=(
            None if raw.get("on_hand_estimate") is None
            else int(raw["on_hand_estimate"])
        ),
        delivered_since_verified=int(raw.get("delivered_since_verified", 0)),
        source_url=raw.get("source_url"),
        last_verified=verified,
        confirmed_by_org=bool(raw.get("confirmed_by_org", False)),
        shelf_life_days=int(raw.get("shelf_life_days", 14)),
    )


def parse_org(raw: dict) -> Org:
    return Org(
        id=raw["id"],
        name=raw["name"],
        lat=float(raw["lat"]),
        lng=float(raw["lng"]),
        address=raw.get("address", ""),
        hours=raw.get("hours", ""),
        phone=raw.get("phone", ""),
        email=raw.get("email", ""),
        preferred_channel=raw.get("preferred_channel", "email"),
        verification_source=raw.get("verification_source", ""),
        accepts={k: _condition(v) for k, v in (raw.get("accepts") or {}).items()},
        prohibited=list(raw.get("prohibited") or []),
        needs=[_need(n) for n in (raw.get("needs") or [])],
        at_capacity=bool(raw.get("at_capacity", False)),
    )


def load_orgs(directory: Path | None = None) -> list[Org]:
    """Load every *.yaml in the corpus directory, sorted by id for stable output."""
    directory = directory or DATA_DIR
    orgs = [parse_org(yaml.safe_load(p.read_text())) for p in directory.glob("*.yaml")]

    seen: set[str] = set()
    for org in orgs:
        if org.id in seen:
            raise ValueError(f"duplicate org id in corpus: {org.id}")
        seen.add(org.id)

    return sorted(orgs, key=lambda o: o.id)


def categories(orgs: list[Org]) -> list[str]:
    """Every category any org has an opinion about -- the vision vocabulary."""
    seen: set[str] = set()
    for org in orgs:
        seen.update(org.accepts)
        seen.update(org.prohibited)
        seen.update(n.category for n in org.needs)
    return sorted(seen)
