# app/engine/scheduler.py
"""
Cron scheduling: compute next run times and poll for workflows whose
schedule has fired, creating WorkflowRun rows.

Design decision made here, not yet confirmed with anyone: the polling loop
runs as a background asyncio task inside the FastAPI process (consistent
with the "engine runs in-process" architecture decision), started via the
app's lifespan hook. If this should instead be a separate process or
APScheduler, this file needs to change before it's relied on.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Workflow, WorkflowRun
from app.models.enums import TriggerType, WorkflowRunStatus

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 30


def next_run_time(cron_expression: str, base_time: datetime) -> datetime:
    """
    Given a cron expression and a reference time, return the next time
    the schedule fires after base_time.

    Does not itself check whether that time has passed — that's the
    caller's job (see get_due_workflows). This function is pure and easy
    to unit test against hand-verified expected times.
    """
    itr = croniter(cron_expression, base_time)
    return itr.get_next(datetime)


def get_due_workflows(db: Session, now: datetime) -> list[Workflow]:
    """
    Return active workflows whose cron schedule has fired as of `now`.

    Queries real Workflow rows from Postgres — not a hardcoded list, per
    the Week 2 requirement ("integrate with real DB"). Included here now
    rather than deferred, since the polling loop is meaningless without it.

    Note: this recomputes next_run_time from cron_expression and last
    triggered_at on every poll rather than persisting "next_run_at" as a
    column. Simpler, but means a workflow with no prior runs needs a
    sensible base_time — using created_at as that base_time, since a
    workflow with no runs yet has never had its schedule evaluated.
    """
    due: list[Workflow] = []

    active_workflows = db.execute(
        select(Workflow).where(Workflow.is_active.is_(True))
    ).scalars().all()

    for workflow in active_workflows:
        if not workflow.cron_expression:
            continue

        last_run = db.execute(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == workflow.id)
            .order_by(WorkflowRun.triggered_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        base_time = last_run.triggered_at if last_run else workflow.created_at
        scheduled_time = next_run_time(workflow.cron_expression, base_time)

        if scheduled_time <= now:
            due.append(workflow)

    return due


def trigger_workflow_run(db: Session, workflow: Workflow) -> WorkflowRun:
    """
    Create a new WorkflowRun row for a workflow whose schedule fired.

    status=PENDING, trigger_type=CRON per spec — the worker pool (Week 4,
    not this file) is what actually picks it up and moves it to RUNNING.
    """
    run = WorkflowRun(
        workflow_id=workflow.id,
        status=WorkflowRunStatus.PENDING,
        trigger_type=TriggerType.CRON,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _active_run_count(db: Session, workflow_id: int) -> int:
    """
    Count WorkflowRuns for this workflow that are still in flight
    (PENDING or RUNNING) — used to enforce max_active_runs.
    """
    return db.execute(
        select(WorkflowRun).where(
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.status.in_(
                [WorkflowRunStatus.PENDING, WorkflowRunStatus.RUNNING]
            ),
        )
    ).scalars().all().__len__()


async def poll_and_trigger(db: Session) -> list[WorkflowRun]:
    """
    One polling cycle: find due workflows, create a WorkflowRun for each —
    unless doing so would exceed that workflow's max_active_runs.

    Enforced here, not deferred: if a workflow's previous run is still
    PENDING or RUNNING and its count already meets max_active_runs, this
    cycle skips triggering it and logs why, rather than silently piling up
    runs behind a slow or stuck workflow.
    """
    now = datetime.utcnow()
    due_workflows = get_due_workflows(db, now)

    triggered_runs = []
    for workflow in due_workflows:
        active_count = _active_run_count(db, workflow.id)
        if active_count >= workflow.max_active_runs:
            logger.info(
                "Skipping workflow %s: %d active run(s) already meets max_active_runs=%d",
                workflow.id, active_count, workflow.max_active_runs,
            )
            continue

        run = trigger_workflow_run(db, workflow)
        logger.info("Triggered WorkflowRun %s for workflow %s", run.id, workflow.id)
        triggered_runs.append(run)

    return triggered_runs


async def run_polling_loop(
    session_factory,
    interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    stop_event: asyncio.Event | None = None,
) -> None:
    """
    Long-running loop, intended to be launched as a background asyncio
    task from the FastAPI lifespan hook:

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            stop_event = asyncio.Event()
            task = asyncio.create_task(run_polling_loop(SessionLocal, stop_event=stop_event))
            yield
            stop_event.set()
            await task

    session_factory is passed in (e.g. SessionLocal from app.core.database)
    rather than imported directly, so this loop is testable without a real
    running app — pass a fake session factory in tests.

    stop_event lets tests and app shutdown terminate the loop cleanly
    instead of it running forever inside a test process.
    """
    while stop_event is None or not stop_event.is_set():
        db = session_factory()
        try:
            await poll_and_trigger(db)
        except Exception:
            logger.exception("Polling cycle failed")
        finally:
            db.close()

        try:
            await asyncio.wait_for(
                stop_event.wait() if stop_event else asyncio.sleep(interval_seconds),
                timeout=interval_seconds,
            )
        except asyncio.TimeoutError:
            pass
