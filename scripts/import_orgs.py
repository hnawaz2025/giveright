"""Build the national organization registry from the IRS Business Master File.

    python scripts/import_orgs.py --states all          # the whole country
    python scripts/import_orgs.py --states DC MD VA     # one metro area
    python scripts/import_orgs.py --states CA --limit 2000

Three stages, each of which can be re-run without redoing the others:

  download  the IRS Exempt Organizations BMF extracts (~500 MB, cached on disk)
  filter    to live 501(c)(3)s in the requested states whose NTEE code implies
            they handle donated goods
  geocode   through the US Census Bureau's free batch geocoder, cached by
            address so a re-run costs nothing

What this does NOT do is decide what any organization needs. The BMF says who
exists and where; it says nothing about whether a shelter is short of coats
this week. Registry entries therefore carry no needs at all -- that is Layer 2,
and it only ever comes from the organization itself.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from giveright.registry import (  # noqa: E402
    NTEE_ACCEPTS,
    Entry,
    connect,
    count,
    save,
    today_iso,
)

BMF_FILES = [f"https://www.irs.gov/pub/irs-soi/eo{n}.csv" for n in (1, 2, 3, 4)]
CENSUS_BATCH = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
CENSUS_LIMIT = 10_000                       # per the Census batch API

ALL_STATES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
    "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA "
    "WV WI WY PR VI GU AS MP"
).split()

CACHE = Path(__file__).resolve().parents[1] / "data" / "cache"
GEOCACHE = CACHE / "geocoded.jsonl"

WANTED_NTEE = tuple(sorted(NTEE_ACCEPTS))
LIVE_STATUS = {"01"}                        # unconditionally exempt
CHARITABLE = {"03"}                         # 501(c)(3)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def download(url: str) -> Path:
    """Cached. The extracts are large and change monthly, not hourly."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / Path(url).name
    if path.exists() and path.stat().st_size > 0:
        return path
    log(f"  downloading {url} …")
    tmp = path.with_suffix(".part")
    try:
        urllib.request.urlretrieve(url, tmp)
    except urllib.error.URLError as exc:
        raise SystemExit(f"could not download {url}: {exc}") from exc
    tmp.rename(path)
    return path


def wanted(row: dict) -> bool:
    return (
        row.get("SUBSECTION") in CHARITABLE
        and row.get("STATUS") in LIVE_STATUS
        and (row.get("NTEE_CD") or "")[:3].upper() in NTEE_ACCEPTS
        and bool(row.get("STREET"))
    )


def harvest(states: set[str], limit: int | None) -> list[dict]:
    found = []
    for url in BMF_FILES:
        path = download(url)
        with path.open(newline="", encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh):
                if row.get("STATE") in states and wanted(row):
                    found.append(row)
                    if limit and len(found) >= limit:
                        return found
        log(f"  {path.name}: {len(found)} matching so far")
    return found


# --------------------------------------------------------------------------
# Geocoding
# --------------------------------------------------------------------------


def load_geocache() -> dict[str, tuple[float, float]]:
    if not GEOCACHE.exists():
        return {}
    out = {}
    for line in GEOCACHE.read_text().splitlines():
        if line.strip():
            rec = json.loads(line)
            out[rec["key"]] = (rec["lat"], rec["lng"])
    return out


def remember(found: dict[str, tuple[float, float]]) -> None:
    GEOCACHE.parent.mkdir(parents=True, exist_ok=True)
    with GEOCACHE.open("a") as fh:
        for key, (lat, lng) in found.items():
            fh.write(json.dumps({"key": key, "lat": lat, "lng": lng}) + "\n")


def key_for(row: dict) -> str:
    return "|".join(
        (row["STREET"], row["CITY"], row["STATE"], (row["ZIP"] or "")[:5])
    ).upper()


def geocode_chunk(chunk: list[dict]) -> dict[str, tuple[float, float]]:
    """One batch through the Census geocoder, or nothing.

    The network boundary catches everything, deliberately. A run that has spent
    an hour geocoding must not be lost because urllib raised a class that was
    not on a list -- RemoteDisconnected is not a URLError, and finding that out
    after 47,000 addresses is an expensive way to learn it.
    """
    payload = io.StringIO()
    writer = csv.writer(payload)
    for n, row in enumerate(chunk):
        writer.writerow(
            [n, row["STREET"], row["CITY"], row["STATE"], (row["ZIP"] or "")[:5]]
        )

    for attempt in (1, 2):
        try:
            body, headers = _multipart(payload.getvalue().encode())
            request = urllib.request.Request(CENSUS_BATCH, data=body, headers=headers)
            with urllib.request.urlopen(request, timeout=900) as response:
                result = response.read().decode("utf-8", "replace")
            break
        except Exception as exc:  # noqa: BLE001 -- a network boundary
            log(f"    ! attempt {attempt} failed ({type(exc).__name__}: {exc})")
            if attempt == 2:
                return {}
            time.sleep(20)

    found = {}
    for line in csv.reader(io.StringIO(result)):
        # id, input, match, exact, matched address, "lon,lat", tiger, side
        if len(line) >= 6 and line[2] == "Match" and "," in line[5]:
            lng, lat = line[5].split(",")[:2]
            found[key_for(chunk[int(line[0])])] = (float(lat), float(lng))
    return found


def _multipart(csv_bytes: bytes) -> tuple[bytes, dict]:
    boundary = "----giveright-geocode"
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="benchmark"\r\n\r\n'
        f"Public_AR_Current\r\n".encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="addressFile"; '
        f'filename="addresses.csv"\r\nContent-Type: text/csv\r\n\r\n'.encode(),
        csv_bytes,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    return body, {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--states", nargs="+", required=True,
                        help='two-letter codes, e.g. DC MD VA, or "all"')
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after this many matching organizations")
    parser.add_argument("--out", default=None, help="registry path")
    args = parser.parse_args(argv)

    states = (
        set(ALL_STATES)
        if any(s.lower() == "all" for s in args.states)
        else {s.strip().upper() for s in args.states}
    )
    log(f"NTEE codes in scope: {', '.join(WANTED_NTEE)}")
    log(f"states: {'all 50 + DC and territories' if len(states) > 20 else ', '.join(sorted(states))}")

    rows = harvest(states, args.limit)
    log(f"{len(rows)} live 501(c)(3)s whose classification implies donated goods")
    if not rows:
        log("nothing to import")
        return 1

    cache = load_geocache()
    todo = [r for r in rows if key_for(r) not in cache]
    log(f"  {len(rows) - len(todo)} already geocoded, {len(todo)} to do")

    path = Path(args.out) if args.out else None
    conn = connect(path)
    written, failed = 0, 0

    # Save as we go. An import that only writes at the end throws away an
    # hour's work the first time a chunk fails, which is exactly what happened.
    def flush(subset: list[dict]) -> int:
        entries = [
            Entry(
                ein=row["EIN"], name=row["NAME"], street=row["STREET"],
                city=row["CITY"], state=row["STATE"], zip=(row["ZIP"] or "")[:5],
                ntee=(row["NTEE_CD"] or "").upper(),
                lat=cache[key_for(row)][0], lng=cache[key_for(row)][1],
                source="IRS Exempt Organizations Business Master File",
                imported_on=today_iso(),
            )
            for row in subset
            if key_for(row) in cache
        ]
        save(conn, entries)
        return len(entries)

    written += flush([r for r in rows if key_for(r) in cache])   # anything cached already

    for start in range(0, len(todo), CENSUS_LIMIT):
        chunk = todo[start : start + CENSUS_LIMIT]
        log(f"  geocoding {start + 1}–{start + len(chunk)} of {len(todo)} …")
        found = geocode_chunk(chunk)
        if not found:
            failed += len(chunk)
            log("    chunk produced nothing; moving on")
            continue
        cache.update(found)
        remember(found)
        written += flush(chunk)
        log(f"    matched {len(found)}; registry now holds {count(path)}")

    conn.close()
    log(f"imported {written} organizations")
    if failed:
        log(f"{failed} addresses were in chunks the geocoder refused -- "
            f"re-run to retry just those, the rest are cached")
    log(f"registry holds {count(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
