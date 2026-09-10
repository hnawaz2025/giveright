"""Limits on the one endpoint that spends money.

Most of this API is cheap: reading organisations is a bounded SQLite query and
the dashboard is a file. `POST /runs` is different -- it sends a photograph to a
model, and a public demo URL is open to anyone who finds it.

Two limits, because they protect against different things.

**Per client**, so one person cannot exhaust the service for everyone. A token
bucket rather than a fixed window: a donor who uploads three photographs in a
minute is behaving normally and should not be punished for a burst, while
someone uploading continuously should be slowed.

**Per hour, in total**, because the first limit does nothing about a hundred
clients. This is the one that stands between a shared demo link and a bill,
and it is deliberately a hard ceiling rather than a rate.

Both live in memory. That is honest for a single instance and wrong for
several -- with more than one process this becomes per-process, and the real
answer is a shared store. Said here rather than discovered later.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class Bucket:
    tokens: float
    updated: float


@dataclass
class RateLimiter:
    """A token bucket per client, plus a hard hourly ceiling for everyone."""

    per_minute: float = 6.0          # sustained rate for one client
    burst: int = 3                   # how many can arrive at once
    hourly_ceiling: int = 200        # total model calls per hour, all clients
    _buckets: dict[str, Bucket] = field(default_factory=dict)
    _hour: list[float] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self, client: str, *, now: float | None = None) -> str | None:
        """None if allowed, otherwise why not. Consumes on success only."""
        now = time.monotonic() if now is None else now

        with self._lock:
            self._hour = [t for t in self._hour if now - t < 3600]
            if len(self._hour) >= self.hourly_ceiling:
                return (
                    "This demo has reached its hourly limit on photo "
                    "identification. Try again later, or use the sample pile."
                )

            bucket = self._buckets.get(client)
            if bucket is None:
                bucket = Bucket(tokens=float(self.burst), updated=now)
                self._buckets[client] = bucket

            refill = (now - bucket.updated) * (self.per_minute / 60.0)
            bucket.tokens = min(float(self.burst), bucket.tokens + refill)
            bucket.updated = now

            if bucket.tokens < 1.0:
                wait = (1.0 - bucket.tokens) / (self.per_minute / 60.0)
                return f"Too many photos too quickly. Try again in {wait:.0f}s."

            bucket.tokens -= 1.0
            self._hour.append(now)
            return None

    def remaining(self, client: str) -> int:
        with self._lock:
            bucket = self._buckets.get(client)
            return self.burst if bucket is None else int(bucket.tokens)

    def used_this_hour(self, *, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        with self._lock:
            return len([t for t in self._hour if now - t < 3600])
