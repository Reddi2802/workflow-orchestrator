# tests/test_zero_task_run.py
"""
known_limitations.md item 12: a zero-task workflow triggered by cron used to
leave its WorkflowRun stuck at RUNNING (nothing ever finalized it), counting
against max_active_runs until the next restart.

Two layers are covered:
- start_workflow_run finalizes an empty run immediately (engine is safe no
  matter who created the run).
- poll_and_trigger_sync does not create runs for an empty workflow at all.

Real Postgres and Redis, no mocking.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

from app.engine.scheduler import _active_run_count, poll_and_trigger_sync
from app.engine.worker_pool import dispatch_pending_runs, start_workflow_run
from app.models.entities import Task, Workflow, WorkflowRun
from app.models.enums import TriggerType, WorkflowRunStatus
from app.queue.redis_queue import dequeue_task


def _ok_command() -> str:
    return f'"{sys.executable}" -c "exit(0)"'


def _make_workflow(db, **overrides) -> Workflow:
    defaults = dict(name="zero-task", is_active=True, max_active_runs=1)
    defaults.update(overrides)
    workflow = Workflow(**defaults)
    db.add(workflow)
    db.commit()
    db.refresh(workflow)
    return workflow


def _make_pending_run(db, workflow, trigger=TriggerType.CRON) -> WorkflowRun:
    run = WorkflowRun(
        workflow_id=workflow.id,
        status=WorkflowRunStatus.PENDING,
        trigger_type=trigger,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def test_start_workflow_run_finalizes_a_zero_task_run(db_session):
    workflow = _make_workflow(db_session)
    run = _make_pending_run(db_session, workflow)

    created = start_workflow_run(db_session, run)

    assert created == []
    db_session.refresh(run)
    assert run.status == WorkflowRunStatus.SUCCESS
    assert run.started_at is not None
    assert run.completed_at is not None
    assert dequeue_task(1) is None, "nothing should have been enqueued"


def test_zero_task_run_no_longer_counts_as_active(db_session):
    workflow = _make_workflow(db_session)
    _make_pending_run(db_session, workflow)
    assert _active_run_count(db_session, workflow.id) == 1

    dispatch_pending_runs(db_session)

    assert _active_run_count(db_session, workflow.id) == 0


def test_poll_skips_a_due_workflow_with_no_tasks(db_session):
    workflow = _make_workflow(
        db_session,
        cron_expression="* * * * *",
        created_at=datetime.utcnow() - timedelta(days=1),
    )

    triggered = poll_and_trigger_sync(db_session)

    assert triggered == []
    assert db_session.query(WorkflowRun).filter_by(workflow_id=workflow.id).count() == 0


def test_poll_still_triggers_a_due_workflow_that_has_tasks(db_session):
    """Control for the test above: the skip is about 'no tasks', not a blanket
    change to what counts as due."""
    workflow = _make_workflow(
        db_session,
        cron_expression="* * * * *",
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    db_session.add(Task(workflow_id=workflow.id, name="only", command=_ok_command()))
    db_session.commit()

    triggered = poll_and_trigger_sync(db_session)

    assert [r.workflow_id for r in triggered] == [workflow.id]
