"""The state one donation run carries between tool calls.

Strands tools are plain functions, so the pile, the radius and the corpus live
here and are closed over when the tools are built. That keeps every tool a pure
function of the workspace -- no globals, and a test can drive the whole toolset
without an agent or a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .corpus import categories, load_orgs
from .fallback import Pathways
from .geo import km
from .matching import Plan
from .models import Item, Org
from .observations import ObservationLog
from .watch import HoldLog
from .trends import Ledger

DEFAULT_RADIUS_MILES = 5.0


@dataclass
class Workspace:
    origin: tuple[float, float]
    orgs: list[Org]
    pathways: Pathways
    ledger: Ledger
    radius_km: float = km(DEFAULT_RADIUS_MILES)
    items: dict[str, Item] = field(default_factory=dict)
    plan: Plan | None = None
    today: date | None = None
    org_dir: Path | None = None
    observations: ObservationLog = field(default_factory=ObservationLog)
    holds: HoldLog = field(default_factory=HoldLog)

    @classmethod
    def open(
        cls,
        origin: tuple[float, float],
        *,
        radius_miles: float = DEFAULT_RADIUS_MILES,
        org_dir: Path | None = None,
        ledger_path: Path | None = None,
        observations_path: Path | None = None,
        holds_path: Path | None = None,
        today: date | None = None,
    ) -> "Workspace":
        # The curated corpus is read-only. Everything GiveRight itself observed
        # -- deliveries, replies -- lives in an append-only log and is folded on
        # top here, so a machine write can never overwrite a sourced fact.
        observations = ObservationLog.load(observations_path)
        return cls(
            origin=origin,
            orgs=observations.replay(load_orgs(org_dir)),
            pathways=Pathways.load(),
            ledger=Ledger.load(ledger_path),
            radius_km=km(radius_miles),
            today=today,
            org_dir=org_dir,
            observations=observations,
            holds=HoldLog.load(holds_path),
        )

    @property
    def vocabulary(self) -> list[str]:
        return categories(self.orgs)

    def org(self, org_id: str) -> Org | None:
        return next((o for o in self.orgs if o.id == org_id), None)

    def item(self, item_id: str) -> Item | None:
        return self.items.get(item_id)

    def add(self, items: list[Item]) -> None:
        for item in items:
            self.items[item.id] = item
