import json
from datetime import datetime

import azure.functions as func
import pytest

from blueprints import earthquake_acquisition as acquisition_module
from blueprints.earthquake_acquisition import acquire_earthquakes
from pipelines import IngestionResult


def request(body: bytes = b"") -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url="http://localhost:7071/api/earthquakes/acquire",
        headers={},
        params={},
        route_params={},
        body=body,
    )


def test_acquisition_endpoint_returns_ok_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = IngestionResult(
        succeeded=True,
        reason="OK",
        normalized=2,
        skipped=0,
        published=2,
        failed=0,
        metadata=[
            {
                "id": "us7000abcd",
                "event_type": "earthquake",
                "magnitude": 2.4,
                "place": "Example",
                "occurred_at_utc": "2026-09-11T12:30:00Z",
                "updated_at_utc": None,
                "longitude": -122.4,
                "latitude": 38.7,
                "depth_km": 4.2,
                "source_url": None,
            }
        ],
        events=[],
    )

    def run_ingestion(*, dry_run: bool) -> IngestionResult:
        assert not dry_run
        return result

    monkeypatch.setattr(acquisition_module, "run_earthquake_ingestion", run_ingestion)

    response = acquire_earthquakes(request())
    payload = json.loads(response.get_body())

    assert response.status_code == 200
    assert payload["reason"] == "OK"
    assert payload["counts"] == {
        "retrieved": 2,
        "normalized": 2,
        "skipped": 0,
        "published": 2,
        "failed": 0,
    }
    assert payload["metadata"][0]["id"] == "us7000abcd"
    assert "event" not in payload["metadata"][0]
    assert "events" not in payload
    assert datetime.fromisoformat(payload["utc_now"]).tzinfo is not None


def test_acquisition_endpoint_reports_skipped_source_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = IngestionResult(
        succeeded=False,
        reason="Skipped 1 malformed USGS earthquake events",
        normalized=2,
        skipped=1,
        published=2,
        failed=0,
        metadata=[],
        events=[],
    )
    monkeypatch.setattr(
        acquisition_module,
        "run_earthquake_ingestion",
        lambda *, dry_run: result,
    )

    response = acquire_earthquakes(request())
    payload = json.loads(response.get_body())

    assert response.status_code == 500
    assert payload["reason"] == "Skipped 1 malformed USGS earthquake events"
    assert payload["counts"] == {
        "retrieved": 3,
        "normalized": 2,
        "skipped": 1,
        "published": 2,
        "failed": 0,
    }


def test_acquisition_endpoint_reports_partial_publish_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = IngestionResult(
        succeeded=False,
        reason="IoT Hub failed to publish 1 of 2 earthquake events",
        normalized=2,
        skipped=0,
        published=1,
        failed=1,
        metadata=[],
        events=[],
    )
    monkeypatch.setattr(
        acquisition_module,
        "run_earthquake_ingestion",
        lambda *, dry_run: result,
    )

    response = acquire_earthquakes(request())
    payload = json.loads(response.get_body())

    assert response.status_code == 500
    assert payload["reason"] == ("IoT Hub failed to publish 1 of 2 earthquake events")
    assert payload["counts"] == {
        "retrieved": 2,
        "normalized": 2,
        "skipped": 0,
        "published": 1,
        "failed": 1,
    }


def test_acquisition_endpoint_reports_pipeline_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = IngestionResult(
        succeeded=False,
        reason="USGS download failed: RuntimeError: USGS is unavailable",
        normalized=0,
        skipped=0,
        published=0,
        failed=0,
        metadata=[],
        events=[],
    )
    monkeypatch.setattr(
        acquisition_module,
        "run_earthquake_ingestion",
        lambda *, dry_run: result,
    )

    response = acquire_earthquakes(request())
    payload = json.loads(response.get_body())

    assert response.status_code == 500
    assert payload["reason"] == (
        "USGS download failed: RuntimeError: USGS is unavailable"
    )
    assert payload["counts"] == {
        "retrieved": 0,
        "normalized": 0,
        "skipped": 0,
        "published": 0,
        "failed": 0,
    }


def test_acquisition_endpoint_returns_complete_events_for_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    complete_event = {
        "id": "us7000abcd",
        "schema_version": 1,
        "source": "usgs",
        "metadata": {
            "event_type": "earthquake",
            "magnitude": 2.4,
            "place": "Example",
            "occurred_at_utc": "2026-09-11T12:30:00Z",
            "updated_at_utc": None,
            "longitude": -122.4,
            "latitude": 38.7,
            "depth_km": 4.2,
            "source_url": None,
        },
        "event": {"type": "Feature", "id": "us7000abcd"},
    }
    result = IngestionResult(
        succeeded=True,
        reason="OK",
        normalized=1,
        skipped=0,
        published=0,
        failed=0,
        metadata=[{"id": "us7000abcd", **complete_event["metadata"]}],  # type: ignore[typeddict-item]
        events=[complete_event],  # type: ignore[list-item]
    )

    def run_ingestion(*, dry_run: bool) -> IngestionResult:
        assert dry_run
        return result

    monkeypatch.setattr(acquisition_module, "run_earthquake_ingestion", run_ingestion)

    response = acquire_earthquakes(request(b'{"dry_run": "yes"}'))
    payload = json.loads(response.get_body())

    assert response.status_code == 200
    assert payload["events"] == [complete_event]
    assert payload["counts"] == {
        "retrieved": 1,
        "normalized": 1,
        "skipped": 0,
        "published": 0,
        "failed": 0,
    }


def test_acquisition_endpoint_treats_unrecognized_dry_run_as_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = IngestionResult(
        succeeded=True,
        reason="OK",
        normalized=0,
        skipped=0,
        published=0,
        failed=0,
        metadata=[],
        events=[],
    )

    def run_ingestion(*, dry_run: bool) -> IngestionResult:
        assert not dry_run
        return result

    monkeypatch.setattr(
        acquisition_module,
        "run_earthquake_ingestion",
        run_ingestion,
    )

    response = acquire_earthquakes(request(b'{"dry_run": "sometimes"}'))
    payload = json.loads(response.get_body())

    assert response.status_code == 200
    assert payload["metadata"] == []
    assert payload["reason"] == "OK"


def test_acquisition_endpoint_rejects_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def should_not_run(*, dry_run: bool) -> IngestionResult:
        pytest.fail(f"pipeline unexpectedly ran with dry_run={dry_run}")

    monkeypatch.setattr(
        acquisition_module,
        "run_earthquake_ingestion",
        should_not_run,
    )

    response = acquire_earthquakes(request(b"not-json"))
    payload = json.loads(response.get_body())

    assert response.status_code == 400
    assert payload["metadata"] == []
    assert payload["counts"] == {
        "retrieved": 0,
        "normalized": 0,
        "skipped": 0,
        "published": 0,
        "failed": 0,
    }
    assert payload["reason"] == "Invalid request: request body must be valid JSON"
