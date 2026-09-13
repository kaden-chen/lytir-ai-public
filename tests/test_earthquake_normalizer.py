from typing import Any

import pytest

from services import EarthquakeNormalizer


def usgs_feature() -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": "us7000abcd",
        "properties": {
            "type": "earthquake",
            "mag": 2.4,
            "place": "10 km NW of Example",
            "time": 1_757_592_600_000,
            "updated": 1_757_592_900_000,
            "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000abcd",
        },
        "geometry": {
            "type": "Point",
            "coordinates": [-122.4, 38.7, 4.2],
        },
    }


def test_normalizer_preserves_event_and_builds_metadata() -> None:
    feature = usgs_feature()

    envelope = EarthquakeNormalizer().normalize_feature(feature)

    assert envelope["id"] == "us7000abcd"
    assert envelope["schema_version"] == 1
    assert envelope["source"] == "usgs"
    assert envelope["event"] is feature
    assert envelope["metadata"] == {
        "event_type": "earthquake",
        "magnitude": 2.4,
        "place": "10 km NW of Example",
        "occurred_at_utc": "2025-09-11T12:10:00Z",
        "updated_at_utc": "2025-09-11T12:15:00Z",
        "longitude": -122.4,
        "latitude": 38.7,
        "depth_km": 4.2,
        "source_url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000abcd",
    }


def test_normalizer_skips_only_malformed_features() -> None:
    feature = usgs_feature()
    feed = {
        "type": "FeatureCollection",
        "features": [feature, {"type": "Feature", "id": "missing-data"}, None],
    }

    result = EarthquakeNormalizer().normalize_feed(feed)

    assert [event["id"] for event in result.events] == ["us7000abcd"]
    assert result.skipped == 2


def test_normalizer_rejects_invalid_feed() -> None:
    with pytest.raises(ValueError, match="FeatureCollection"):
        EarthquakeNormalizer().normalize_feed({"type": "Feature"})
