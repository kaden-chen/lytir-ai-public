from datetime import UTC, datetime
from typing import Any

import pytest
from azure.core.exceptions import AzureError

from models import EarthquakeQuery
from services import EarthquakeQueryService, EarthquakeQueryUnavailableError


class FakeContainer:
    def __init__(
        self,
        items: list[dict[str, Any]] | None = None,
        error: AzureError | None = None,
    ) -> None:
        self.items = items or []
        self.error = error
        self.calls: list[tuple[str, list[dict[str, object]], bool]] = []

    def query_items(
        self,
        *,
        query: str,
        parameters: list[dict[str, object]],
        enable_cross_partition_query: bool,
    ) -> list[dict[str, Any]]:
        self.calls.append((query, parameters, enable_cross_partition_query))
        if self.error is not None:
            raise self.error
        return self.items


def item(
    cosmos_id: str,
    *,
    source_id: str = "us-event",
    cosmos_ts: int = 1,
    occurred_at: str = "2026-09-11T12:30:00Z",
    updated_at: str | None = "2026-09-11T12:35:00Z",
    magnitude: float | None = 2.4,
    place: str | None = "Example",
    longitude: float = -122.4,
    latitude: float = 38.7,
) -> dict[str, Any]:
    return {
        "cosmos_id": cosmos_id,
        "cosmos_ts": cosmos_ts,
        "source_id": source_id,
        "schema_version": 1,
        "event_type": "earthquake",
        "magnitude": magnitude,
        "place": place,
        "occurred_at_utc": occurred_at,
        "updated_at_utc": updated_at,
        "longitude": longitude,
        "latitude": latitude,
        "depth_km": 4.2,
        "source_url": "https://should-not-be-returned.invalid",
        "event": {"secret": "raw"},
    }


def filters(**overrides: object) -> EarthquakeQuery:
    values: dict[str, object] = {
        "start_time": datetime(2026, 9, 11, 12, tzinfo=UTC),
        "end_time": datetime(2026, 9, 11, 13, tzinfo=UTC),
    }
    values.update(overrides)
    return EarthquakeQuery(**values)  # type: ignore[arg-type]


def test_query_deduplicates_before_magnitude_filter_and_hides_internal_fields() -> None:
    older_id = "11111111-1111-4111-8111-111111111111"
    newer_id = "22222222-2222-4222-8222-222222222222"
    container = FakeContainer(
        [
            item(older_id, cosmos_ts=20, magnitude=5.0),
            item(
                newer_id,
                cosmos_ts=10,
                updated_at="2026-09-11T12:40:00Z",
                magnitude=2.0,
            ),
        ]
    )

    result = EarthquakeQueryService(container).query(filters(min_magnitude=4.0))

    assert result.items == []
    assert result.candidates == 2
    assert result.duplicates == 1
    assert result.selected == 1

    unfiltered = EarthquakeQueryService(container).query(filters())
    assert unfiltered.items[0]["id"] == newer_id
    assert set(unfiltered.items[0]) == {
        "id",
        "event_type",
        "magnitude",
        "place",
        "occurred_at_utc",
        "updated_at_utc",
        "longitude",
        "latitude",
        "depth_km",
    }


def test_query_uses_cosmos_timestamp_and_id_as_deterministic_tie_breakers() -> None:
    lowest_id = "11111111-1111-4111-8111-111111111111"
    middle_id = "22222222-2222-4222-8222-222222222222"
    highest_id = "33333333-3333-4333-8333-333333333333"
    shared_update = "2026-09-11T12:35:00Z"
    container = FakeContainer(
        [
            item(lowest_id, cosmos_ts=1, updated_at=shared_update),
            item(middle_id, cosmos_ts=2, updated_at=shared_update),
            item(highest_id, cosmos_ts=2, updated_at=shared_update),
        ]
    )

    result = EarthquakeQueryService(container).query(filters())

    assert [record["id"] for record in result.items] == [highest_id]
    assert result.duplicates == 2


def test_query_skips_malformed_records_and_sorts_newest_first() -> None:
    older_id = "11111111-1111-4111-8111-111111111111"
    newer_id = "22222222-2222-4222-8222-222222222222"
    malformed = item("not-a-uuid", source_id="bad")
    container = FakeContainer(
        [
            item(older_id, source_id="older", occurred_at="2026-09-11T12:10:00Z"),
            malformed,
            item(newer_id, source_id="newer", occurred_at="2026-09-11T12:50:00Z"),
        ]
    )

    result = EarthquakeQueryService(container).query(filters())

    assert [record["id"] for record in result.items] == [newer_id, older_id]
    assert result.malformed == 1


def test_query_applies_radius_across_antimeridian() -> None:
    near_id = "11111111-1111-4111-8111-111111111111"
    far_id = "22222222-2222-4222-8222-222222222222"
    container = FakeContainer(
        [
            item(near_id, source_id="near", latitude=0, longitude=-179.9),
            item(far_id, source_id="far", latitude=0, longitude=-170),
        ]
    )

    result = EarthquakeQueryService(container).query(
        filters(latitude=0.0, longitude=179.9, radius_km=30.0)
    )

    assert [record["id"] for record in result.items] == [near_id]


def test_query_parameterizes_bounded_cosmos_query() -> None:
    container = FakeContainer()

    EarthquakeQueryService(container).query(filters())

    query, parameters, cross_partition = container.calls[0]
    assert "@start_time" in query
    assert "@end_time" in query
    assert parameters == [
        {"name": "@start_time", "value": "2026-09-11T12:00:00.000000Z"},
        {"name": "@end_time", "value": "2026-09-11T13:00:00.000000Z"},
    ]
    assert cross_partition


def test_exact_lookup_returns_allowlisted_record() -> None:
    cosmos_id = "11111111-1111-4111-8111-111111111111"
    container = FakeContainer([item(cosmos_id)])

    record = EarthquakeQueryService(container).get_by_id(cosmos_id)

    assert record is not None
    assert record["id"] == cosmos_id
    assert container.calls[0][1] == [{"name": "@id", "value": cosmos_id}]


def test_service_maps_azure_failure_to_unavailable_error() -> None:
    container = FakeContainer(error=AzureError("unavailable"))

    with pytest.raises(EarthquakeQueryUnavailableError):
        EarthquakeQueryService(container).query(filters())


def test_latest_observed_at_returns_latest_valid_timestamp() -> None:
    container = FakeContainer(
        [
            {"occurred_at_utc": "invalid"},
            {"occurred_at_utc": "2026-09-11T12:30:00Z"},
            {"occurred_at_utc": "2026-09-11T12:45:00Z"},
        ]
    )

    latest = EarthquakeQueryService(container).latest_observed_at()

    assert latest == datetime(2026, 9, 11, 12, 45, tzinfo=UTC)
    assert "ORDER BY" in container.calls[0][0]
    assert container.calls[0][1] == []
