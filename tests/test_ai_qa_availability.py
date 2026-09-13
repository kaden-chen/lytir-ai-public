import asyncio
from datetime import UTC, datetime
from typing import cast

from services.ai_qa.availability import EarthquakeAvailabilityService
from services.earthquake_query_service import EarthquakeQueryService


class FakeEarthquakeService:
    def __init__(self) -> None:
        self.calls = 0

    def latest_observed_at(self) -> datetime:
        self.calls += 1
        return datetime(2026, 9, 12, 1, 55, tzinfo=UTC)


def test_availability_reuses_recent_process_cache() -> None:
    earthquake = FakeEarthquakeService()
    service = EarthquakeAvailabilityService(cast(EarthquakeQueryService, earthquake))

    first = asyncio.run(service.get())
    second = asyncio.run(service.get())

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.latest_observed_at_utc == first.latest_observed_at_utc
    assert earthquake.calls == 1
