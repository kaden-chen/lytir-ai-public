"""Application pipelines shared by Azure Function triggers."""

from pipelines.earthquake_ingestion import (
    EarthquakeIngestionPipeline,
    IngestionResult,
    run_earthquake_ingestion,
)

__all__ = [
    "EarthquakeIngestionPipeline",
    "IngestionResult",
    "run_earthquake_ingestion",
]
