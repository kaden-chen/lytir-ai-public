"""Orchestrate the earthquake download, normalization, and publishing stages."""

import logging
from dataclasses import dataclass

from config import ServiceSettings
from models import EarthquakeEnvelope, EarthquakeMetadataRecord
from services import EarthquakeNormalizer, IoTHubPublisher, USGSClient


@dataclass(frozen=True)
class IngestionResult:
    """Outcome and counts produced by one pipeline run."""

    succeeded: bool
    reason: str
    normalized: int
    skipped: int
    published: int
    failed: int
    metadata: list[EarthquakeMetadataRecord]
    events: list[EarthquakeEnvelope]


class EarthquakeIngestionPipeline:
    """Run the earthquake pipeline stages in sequence."""

    def __init__(
        self,
        client: USGSClient,
        normalizer: EarthquakeNormalizer,
        publisher: IoTHubPublisher,
    ) -> None:
        self._client = client
        self._normalizer = normalizer
        self._publisher = publisher

    def run(self, dry_run: bool = False) -> IngestionResult:
        """Download, normalize, and publish one USGS feed."""
        stage = "USGS download"
        normalized = 0
        skipped = 0
        published = 0
        failed = 0
        metadata: list[EarthquakeMetadataRecord] = []
        events: list[EarthquakeEnvelope] = []

        try:
            feed = self._client.download()

            stage = "earthquake normalization"
            normalization = self._normalizer.normalize_feed(feed)
            normalized = len(normalization.events)
            skipped = normalization.skipped
            metadata = [
                EarthquakeMetadataRecord(id=event["id"], **event["metadata"])
                for event in normalization.events
            ]

            if dry_run:
                events = normalization.events
            else:
                stage = "IoT Hub publishing"
                publication = self._publisher.publish(normalization.events)
                published = publication.published
                failed = publication.failed
        except Exception as exc:
            logging.exception("Earthquake ingestion failed during %s", stage)
            return IngestionResult(
                succeeded=False,
                reason=f"{stage} failed: {type(exc).__name__}: {exc}",
                normalized=normalized,
                skipped=skipped,
                published=published,
                failed=failed,
                metadata=metadata,
                events=events,
            )

        problems: list[str] = []
        if skipped:
            problems.append(f"Skipped {skipped} malformed USGS earthquake events")
        if failed:
            problems.append(
                f"IoT Hub failed to publish {failed} of "
                f"{published + failed} earthquake events"
            )

        result = IngestionResult(
            succeeded=not problems,
            reason="; ".join(problems) if problems else "OK",
            normalized=normalized,
            skipped=skipped,
            published=published,
            failed=failed,
            metadata=metadata,
            events=events,
        )
        log = logging.info if result.succeeded else logging.warning
        log(
            "Earthquake ingestion completed: reason=%s normalized=%d skipped=%d "
            "published=%d failed=%d dry_run=%s",
            result.reason,
            result.normalized,
            result.skipped,
            result.published,
            result.failed,
            dry_run,
        )
        return result


def run_earthquake_ingestion(dry_run: bool = False) -> IngestionResult:
    """Build and run the pipeline using the Function App environment."""
    try:
        settings = ServiceSettings()
        pipeline = EarthquakeIngestionPipeline(
            client=USGSClient(settings.usgs_earthquake_api_url),
            normalizer=EarthquakeNormalizer(),
            publisher=IoTHubPublisher(settings.iot_hub_device_connection_string),
        )
    except Exception as exc:
        logging.exception("Earthquake ingestion initialization failed")
        return IngestionResult(
            succeeded=False,
            reason=f"Pipeline initialization failed: {type(exc).__name__}: {exc}",
            normalized=0,
            skipped=0,
            published=0,
            failed=0,
            metadata=[],
            events=[],
        )

    return pipeline.run(dry_run=dry_run)
