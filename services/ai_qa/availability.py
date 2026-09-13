"""Short-lived earthquake availability metadata for Q&A routing."""

import asyncio
import threading
from dataclasses import dataclass
from datetime import datetime
from time import monotonic

from services.earthquake_query_service import EarthquakeQueryService

AVAILABILITY_CACHE_SECONDS = 300.0


@dataclass(frozen=True)
class EarthquakeAvailability:
    """Latest observed occurrence and whether it came from the process cache."""

    latest_observed_at_utc: datetime | None
    cache_hit: bool


class EarthquakeAvailabilityService:
    """Cache the latest valid occurrence timestamp for five minutes."""

    def __init__(self, earthquake_service: EarthquakeQueryService) -> None:
        self._earthquake_service = earthquake_service
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached_value: datetime | None = None
        self._has_cached_value = False

    async def get(self) -> EarthquakeAvailability:
        now = monotonic()
        with self._lock:
            if (
                self._has_cached_value
                and now - self._cached_at < AVAILABILITY_CACHE_SECONDS
            ):
                return EarthquakeAvailability(self._cached_value, cache_hit=True)

        latest = await asyncio.to_thread(self._earthquake_service.latest_observed_at)
        with self._lock:
            self._cached_at = monotonic()
            self._cached_value = latest
            self._has_cached_value = True
        return EarthquakeAvailability(latest, cache_hit=False)
