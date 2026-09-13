import pytest

from blueprints import earthquake_scheduler as scheduler_module
from blueprints.earthquake_scheduler import scheduled_earthquake_acquisition
from pipelines import IngestionResult


class FakeTimer:
    def __init__(self, past_due: bool = False) -> None:
        self.past_due = past_due


def result(*, succeeded: bool, reason: str) -> IngestionResult:
    return IngestionResult(
        succeeded=succeeded,
        reason=reason,
        normalized=0,
        skipped=0,
        published=0,
        failed=0,
        metadata=[],
        events=[],
    )


def test_timer_runs_pipeline_with_publishing_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run_ingestion(*, dry_run: bool) -> IngestionResult:
        assert not dry_run
        return result(succeeded=True, reason="OK")

    monkeypatch.setattr(scheduler_module, "run_earthquake_ingestion", run_ingestion)

    scheduled_earthquake_acquisition(FakeTimer())


def test_timer_marks_failed_pipeline_run_as_failed_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        scheduler_module,
        "run_earthquake_ingestion",
        lambda *, dry_run: result(succeeded=False, reason="USGS download failed"),
    )

    with pytest.raises(RuntimeError, match="USGS download failed"):
        scheduled_earthquake_acquisition(FakeTimer())
