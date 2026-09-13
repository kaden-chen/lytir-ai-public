from collections.abc import Iterable
from typing import Any

import pytest

from models import EarthquakeEnvelope, EarthquakeMetadata
from pipelines import EarthquakeIngestionPipeline, run_earthquake_ingestion
from services import (
    NormalizationResult,
    PublishResult,
)


class FakeClient:
    def __init__(self, feed: dict[str, Any]) -> None:
        self.feed = feed

    def download(self) -> dict[str, Any]:
        return self.feed


class FailingClient:
    def download(self) -> dict[str, Any]:
        raise RuntimeError("USGS is unavailable")


class FakeNormalizer:
    def __init__(self, result: NormalizationResult) -> None:
        self.result = result

    def normalize_feed(self, feed: dict[str, Any]) -> NormalizationResult:
        assert feed == {"type": "FeatureCollection"}
        return self.result


class FakePublisher:
    def __init__(self, result: PublishResult) -> None:
        self.result = result
        self.events: list[EarthquakeEnvelope] = []

    def publish(self, events: Iterable[EarthquakeEnvelope]) -> PublishResult:
        self.events = list(events)
        return self.result


def envelope() -> EarthquakeEnvelope:
    metadata: EarthquakeMetadata = {
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
    return {
        "id": "us7000abcd",
        "schema_version": 1,
        "source": "usgs",
        "metadata": metadata,
        "event": {"type": "Feature", "id": "us7000abcd"},
    }


def test_pipeline_runs_each_stage_and_returns_counts() -> None:
    event = envelope()
    publisher = FakePublisher(PublishResult(published=1, failed=0))
    pipeline = EarthquakeIngestionPipeline(
        client=FakeClient({"type": "FeatureCollection"}),  # type: ignore[arg-type]
        normalizer=FakeNormalizer(  # type: ignore[arg-type]
            NormalizationResult(events=[event], skipped=0)
        ),
        publisher=publisher,  # type: ignore[arg-type]
    )

    result = pipeline.run()

    assert result.succeeded
    assert result.reason == "OK"
    assert result.normalized == 1
    assert result.skipped == 0
    assert result.published == 1
    assert result.failed == 0
    assert publisher.events == [event]
    assert result.metadata == [{"id": "us7000abcd", **event["metadata"]}]
    assert result.events == []


def test_pipeline_dry_run_returns_events_without_publishing() -> None:
    event = envelope()
    publisher = FakePublisher(PublishResult(published=0, failed=0))
    pipeline = EarthquakeIngestionPipeline(
        client=FakeClient({"type": "FeatureCollection"}),  # type: ignore[arg-type]
        normalizer=FakeNormalizer(  # type: ignore[arg-type]
            NormalizationResult(events=[event], skipped=0)
        ),
        publisher=publisher,  # type: ignore[arg-type]
    )

    result = pipeline.run(dry_run=True)

    assert result.succeeded
    assert result.reason == "OK"
    assert result.published == 0
    assert result.events == [event]
    assert publisher.events == []


def test_pipeline_reports_exception_with_stage_context() -> None:
    pipeline = EarthquakeIngestionPipeline(
        client=FailingClient(),  # type: ignore[arg-type]
        normalizer=FakeNormalizer(  # type: ignore[arg-type]
            NormalizationResult(events=[], skipped=0)
        ),
        publisher=FakePublisher(PublishResult(published=0, failed=0)),  # type: ignore[arg-type]
    )

    result = pipeline.run()

    assert not result.succeeded
    assert result.reason == "USGS download failed: RuntimeError: USGS is unavailable"
    assert result.metadata == []
    assert result.events == []


def test_pipeline_entry_point_reports_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("USGS_EARTHQUAKE_API_URL", raising=False)
    monkeypatch.delenv("IOT_HUB_DEVICE_CONNECTION_STRING", raising=False)

    result = run_earthquake_ingestion()

    assert not result.succeeded
    assert result.reason == (
        "Pipeline initialization failed: ValueError: "
        "USGS_EARTHQUAKE_API_URL is required"
    )
    assert result.metadata == []
    assert result.events == []
