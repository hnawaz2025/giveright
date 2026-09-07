"""Distance, and the radius the donor chose before taking the photo.

The radius is a promise: an org outside it is never offered, however badly it
needs the item. Everything here is pure arithmetic so the matcher stays
testable without a maps API.
"""

from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0088

# Distance at which an org's score has decayed to 1/e. Tuned so that a large
# shortfall difference can justify a longer drive, but a small one cannot.
DECAY_KM = 8.0

KM_PER_MILE = 1.609344


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def miles(km: float) -> float:
    return km / KM_PER_MILE


def km(mi: float) -> float:
    return mi * KM_PER_MILE


def proximity(distance_km: float) -> float:
    """1.0 at the doorstep, decaying with distance. Never zero, never negative."""
    return math.exp(-max(0.0, distance_km) / DECAY_KM)


def within(distance_km: float, radius_km: float) -> bool:
    return distance_km <= radius_km
