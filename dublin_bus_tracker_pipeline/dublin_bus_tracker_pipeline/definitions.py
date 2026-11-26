"""
Dagster definitions: what exists (assets), what runs (a job) and when (a schedule).

The pipeline runs every minute. If a run is still going when the next minute starts,
that minute is skipped so runs never pile up.
"""
from dagster import (
    DagsterRunStatus,
    DefaultScheduleStatus,
    Definitions,
    RunRequest,
    RunsFilter,
    ScheduleEvaluationContext,
    SkipReason,
    define_asset_job,
    in_process_executor,
    load_asset_checks_from_modules,
    load_assets_from_modules,
    schedule,
)

from . import assets, checks  # noqa: TID252

all_assets = load_assets_from_modules([assets])
all_checks = load_asset_checks_from_modules([checks])   # data quality checks

# All steps run in one Python process, which keeps memory use low.
my_job = define_asset_job(name="my_asset_job", selection="*", executor_def=in_process_executor)


@schedule(
    job=my_job,
    cron_schedule="* * * * *",          # every minute
    execution_timezone="Europe/Dublin",
    default_status=DefaultScheduleStatus.RUNNING,   # starts with `dagster dev`
)
def every_minute_schedule(context: ScheduleEvaluationContext):
    still_running = context.instance.get_runs(
        filters=RunsFilter(
            job_name=my_job.name,
            statuses=[DagsterRunStatus.QUEUED, DagsterRunStatus.NOT_STARTED,
                      DagsterRunStatus.STARTING, DagsterRunStatus.STARTED],
        ),
        limit=1,
    )
    if still_running:
        return SkipReason("Previous run still in progress; skipping this minute.")
    return RunRequest(run_key=None)


defs = Definitions(
    assets=all_assets,
    asset_checks=all_checks,
    jobs=[my_job],
    schedules=[every_minute_schedule],
)
