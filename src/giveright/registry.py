"""Every tax-exempt organization in the country, and what that does not tell us.

`data/orgs/*.yaml` is hand-curated and will never cover the United States. This
is the other half: a bulk registry built from the IRS Exempt Organizations
Business Master File, so a donor anywhere gets real organizations rather than
an empty map.

The two layers are kept strictly apart, because they are different kinds of
claim:

  * **Who and where** comes from the IRS. It is a dated public record, so it
    satisfies the same provenance rule everything else here does -- the source
    is the BMF extract and the date is the extract's date.

  * **What they accept** is inferred from the NTEE code, which is a
    classification the organization was assigned, not a sentence it wrote.
    A registry entry says "an organization of this kind normally takes these",
    never "this organization needs these".

  * **What they need** is not here at all, and is never guessed. A registry
    organization has no needs until it tells us one, which is what
    `verify_with_org` is for. It ranks as somewhere that would accept the item,
    below anywhere that has actually asked for it.

Storage is SQLite because the filtered national set runs to hundreds of
thousands of rows and a donor only ever wants the few dozen inside their
radius. Stdlib, one file, no service to run.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .geo import haversine_km
from .models import Condition, Org

REGISTRY_FILE = Path(__file__).resolve().parents[2] / "data" / "registry.sqlite"

# NTEE is the IRS's own taxonomy, so this maps an official classification onto
# the categories a donor's photograph produces. It is deliberately narrow: an
# organization only appears for goods its classification actually implies it
# handles. Everything here is "would plausibly accept", never "needs".
NTEE_ACCEPTS: dict[str, tuple[str, ...]] = {
    "K30": ("canned_food",),                    # food service, free distribution
    "K31": ("canned_food",),                    # food banks, food pantries
    "K34": ("canned_food",),                    # congregate meals
    "K35": ("canned_food",),                    # soup kitchens
    "L41": ("winter_coats", "towels", "childrens_clothing", "adult_clothing"),
    # P20 is the IRS's multipurpose human-services bucket and by far the
    # largest -- around 60% of a typical import. It holds real shelters and
    # policy foundations alike, so it is mapped narrowly: the goods almost any
    # human-service organisation can use, and nothing more. Anything wider sends
    # donors to advocacy offices. A `dont_take` reply removes it permanently.
    "P20": ("winter_coats", "adult_clothing"),
    "P24": ("winter_coats", "towels", "kitchenware", "childrens_clothing",
            "adult_clothing", "books", "lamps"),  # salvage & redistribution
    "P26": ("winter_coats", "adult_clothing"),  # emergency assistance
    "P30": ("childrens_clothing", "books"),     # children & youth services
    "P40": ("childrens_clothing", "kitchenware"),
    "P43": ("winter_coats", "childrens_clothing", "adult_clothing", "towels"),
    "P45": ("childrens_clothing", "books"),
    "P60": ("winter_coats", "canned_food", "adult_clothing", "towels"),
    "P73": ("towels", "kitchenware"),
    "P80": ("winter_coats", "adult_clothing", "books"),
    "P82": ("kitchenware", "adult_clothing"),
    "P85": ("kitchenware", "towels"),
}

# The condition floor a registry organization is assumed to hold. Deliberately
# forgiving: we do not know their rules, so we must not invent a strict one and
# silently refuse a donation they would have taken. The floor only rises when
# they tell us.
ASSUMED_FLOOR = Condition.FAIR

# IRS names are stored shouted. Title-casing them is an improvement, but it
# turns DC into Dc, so known initialisms are preserved. This is deliberately an
# allowlist rather than a rule: no heuristic tells UPO from Upo reliably, and a
# guess that mangles an organisation's own name is worse than a plain one.
ACRONYMS = {
    "DC", "USA", "US", "UK", "YMCA", "YWCA", "NAACP", "AARP", "PTA", "PTO",
    "HIV", "AIDS", "LGBT", "LGBTQ", "ASPCA", "VFW", "AMVETS", "CASA", "STEM",
    "II", "III", "IV",
} | {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY",
}

# Lower-cased inside a name, never at the start of one.
JOINERS = {"of", "the", "and", "for", "a", "an", "in", "on", "at", "to", "de"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
    ein         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    street      TEXT,
    city        TEXT,
    state       TEXT,
    zip         TEXT,
    ntee        TEXT,
    lat         REAL,
    lng         REAL,
    source      TEXT NOT NULL,
    imported_on TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orgs_lat ON orgs(lat);
CREATE INDEX IF NOT EXISTS orgs_lng ON orgs(lng);
"""


@dataclass(frozen=True)
class Entry:
    ein: str
    name: str
    street: str
    city: str
    state: str
    zip: str
    ntee: str
    lat: float
    lng: float
    source: str
    imported_on: str

    @property
    def address(self) -> str:
        return ", ".join(p for p in (self.street, self.city, self.state, self.zip) if p)

    def accepts(self) -> dict[str, Condition]:
        categories = NTEE_ACCEPTS.get((self.ntee or "")[:3].upper(), ())
        return {c: ASSUMED_FLOOR for c in categories}

    @property
    def display_name(self) -> str:
        """The IRS stores names shouted. Make them readable without mangling."""
        words = []
        for n, word in enumerate(self.name.split()):
            bare = word.strip(".,'").upper()
            if bare in ACRONYMS:
                words.append(word.upper())
            elif n and word.lower() in JOINERS:
                words.append(word.lower())
            else:
                words.append(word.title())
        return " ".join(words)

    def as_org(self) -> Org:
        """A registry entry as the matcher sees it: somewhere that would accept
        the item, with no needs, because it has never told us one."""
        return Org(
            id=f"irs_{self.ein}",
            name=self.display_name,
            lat=self.lat,
            lng=self.lng,
            address=self.address,
            hours="",                     # not in the BMF; ask, never invent
            verification_source=(
                f"IRS Exempt Organizations BMF, NTEE {self.ntee or 'unclassified'}, "
                f"extract imported {self.imported_on}"
            ),
            accepts=self.accepts(),
            needs=[],                     # Layer 2 is never guessed
        )


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or REGISTRY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def save(conn: sqlite3.Connection, entries: list[Entry]) -> int:
    conn.executemany(
        "INSERT OR REPLACE INTO orgs "
        "(ein,name,street,city,state,zip,ntee,lat,lng,source,imported_on) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (e.ein, e.name, e.street, e.city, e.state, e.zip, e.ntee,
             e.lat, e.lng, e.source, e.imported_on)
            for e in entries
        ],
    )
    conn.commit()
    return len(entries)


def near(
    origin: tuple[float, float],
    radius_km: float,
    *,
    path: Path | None = None,
    conn: sqlite3.Connection | None = None,
    limit: int = 200,
) -> list[Org]:
    """Registry organizations inside the radius, nearest first.

    A bounding box narrows it in SQL -- the index makes that cheap across the
    whole country -- and the exact distance is then checked properly, because a
    box is not a circle and the radius is a promise.
    """
    path = path or REGISTRY_FILE
    if conn is None:
        if not path.exists():
            return []
        conn = connect(path)
        close = True
    else:
        close = False

    lat, lng = origin
    dlat = radius_km / 111.0
    # Longitude degrees shrink towards the poles; guard the cosine near them.
    dlng = radius_km / max(1.0, 111.0 * math.cos(math.radians(lat)))

    try:
        rows = conn.execute(
            "SELECT * FROM orgs WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
            (lat - dlat, lat + dlat, lng - dlng, lng + dlng),
        ).fetchall()
    finally:
        if close:
            conn.close()

    inside = []
    for row in rows:
        distance = haversine_km(lat, lng, row["lat"], row["lng"])
        if distance <= radius_km:
            inside.append((distance, Entry(**dict(row))))

    inside.sort(key=lambda pair: pair[0])
    if len(inside) > limit:
        # Nearest-first, so what is dropped is what the donor was least likely
        # to drive to -- but say so rather than pretending the radius was empty
        # beyond this point.
        truncated_at = inside[limit][0]
        globals()["LAST_TRUNCATED_KM"] = truncated_at
    return [entry.as_org() for _d, entry in inside[:limit]]


LAST_TRUNCATED_KM: float | None = None


def count(path: Path | None = None) -> int:
    path = path or REGISTRY_FILE
    if not path.exists():
        return 0
    conn = connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM orgs").fetchone()[0]
    finally:
        conn.close()


def today_iso() -> str:
    return date.today().isoformat()
