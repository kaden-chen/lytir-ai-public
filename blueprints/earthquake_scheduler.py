"""Timer trigger for scheduled earthquake acquisition."""

import logging

import azure.functions as func

from pipelines import run_earthquake_ingestion

blueprint = func.Blueprint()  # type: ignore[no-untyped-call]


@blueprint.timer_trigger(
    schedule="%USGS_CRON_JOB_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def scheduled_earthquake_acquisition(timer: func.TimerRequest) -> None:
    """Run the earthquake ingestion pipeline on the configured schedule."""
    if timer.past_due:
        logging.warning("Earthquake acquisition timer invocation is past due")

    result = run_earthquake_ingestion(dry_run=False)
    if not result.succeeded:
        raise RuntimeError(result.reason)
