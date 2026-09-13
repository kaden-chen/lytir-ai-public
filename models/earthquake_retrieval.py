"""Domain shapes for earthquake metadata retrieval."""

from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict


class EarthquakeRetrievalRecord(TypedDict):
    """Public metadata allowlist returned for one stored earthquake."""

    id: str
    event_type: str
    magnitude: float | None
    place: str | None
    occurred_at_utc: str
    updated_at_utc: str | None
    longitude: float
    latitude: float
    depth_km: float


@dataclass(frozen=True)
class EarthquakeQuery:
    """Validated filters for one bounded collection query."""

    start_time: datetime
    end_time: datetime
    latitude: float | None = None
    longitude: float | None = None
    radius_km: float | None = None
    min_magnitude: float | None = None
    max_magnitude: float | None = None
