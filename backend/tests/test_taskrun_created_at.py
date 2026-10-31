# tests/test_taskrun_created_at.py
"""
TaskRun.created_at: stamped by the database at insert time, in UTC, so that
started_at - created_at is a real queue-wait measurement.

Needs the migration applied (`uv run alembic upgrade head`) -- the shared
conftest only create_all()s tables that do not exist yet, it never adds a
column to an existing table.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

from app.engine.worker_pool import process_task_run, start_workflow_run
from app.models.entities import Task, TaskRun, Workflow, WorkflowRun
from app.models.enums import TaskStatus, TriggerType, WorkflowRunStatus
from app.queue.redis_queue import dequeue_task


def _fail_command() -> str:
    return f'"{sys.executable}" -c "exit(1)"'


def test_new_task_run_gets_a_utc_created_at(db_session):
    workflow = Workflow(name="created-at")
    db_session.add(workflow)
    db_session.flush()
    task = Task(workflow_id=workflow.id, name="t", command="exit 0")
    run = WorkflowRun(
        workflow_id=workflow.id,
        status=WorkflowRunStatus.RUNNING,
        trigger_type=TriggerType.MANUAL,
    )
    db_session.add_all([task, run])
    db_session.flush()

    before = datetime.utcnow()
    task_run = TaskRun(
        workflow_run_id=run.id, task_id=task.id,
        status=TaskStatus.PENDING, attempt_number=1,
    )
    db_session.add(task_run)
    db_session.commit()
    db_session.refresh(task_run)
    after = datetime.utcnow()

    assert task_run.created_at is not None
    # Same clock, same timezone as started_at/completed_at. A generous window
    # absorbs clock jitter but would catch a timezone offset (hours).
    assert before - timedelta(seconds=5) <= task_run.created_at <= after + timedelta(seconds=5)


def test_retry_row_is_stamped_after_the_backoff_sleep(db_session):
    """The retry row is inserted after time.sleep(backoff), while the session
    still has a transaction open from before the sleep. Its created_at must be
    the real insert time (after the sleep), not the transaction start, or queue
    wait would include the backoff."""
    workflow = Workflow(name="created-at-retry")
    db_session.add(workflow)
    db_session.flush()
    task = Task(
        workflow_id=workflow.id, name="flaky", command=_fail_command(),
        max_retries=1, retry_backoff_seconds=1,
    )
    run = WorkflowRun(
        workflow_id=workflow.id,
        status=WorkflowRunStatus.PENDING,
        trigger_type=TriggerType.MANUAL,
    )
    db_session.add_all([task, run])
    db_session.commit()

    start_workflow_run(db_session, run)
    process_task_run(db_session, dequeue_task(5))   # attempt 1 fails -> RETRYING, sleeps 1s

    attempts = (
        db_session.query(TaskRun)
        .filter_by(workflow_run_id=run.id, task_id=task.id)
        .order_by(TaskRun.attempt_number)
        .all()
    )
    assert [a.attempt_number for a in attempts] == [1, 2]
    first, second = attempts
    assert first.status == TaskStatus.RETRYING

    gap = (second.created_at - first.completed_at).total_seconds()
    assert gap >= 0.9, f"retry row stamped {gap:.2f}s after attempt 1 failed; expected >= the 1s backoff"
