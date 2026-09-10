# tests/test_scheduler_integration.py
"""
Integration tests for get_due_workflows / poll_and_trigger — these need a
real Postgres session because they query real Workflow/WorkflowRun rows.
Not unit tests: no mocking of the DB layer, since a mock here would only
prove the code calls session.execute(), not that the query is correct.

Requires the real Docker Postgres container running with migrations
applied (docker compose up -d && alembic upgrade head), same as the rest
of the project's "verify at the database level" standard.
"""

from datetime import datetime, timedelta

from app.engine.scheduler import get_due_workflows, poll_and_trigger
from app.models.entities import Workflow, WorkflowRun
from app.models.enums import WorkflowRunStatus, TriggerType


def _make_workflow(db, **overrides):
    defaults = dict(
        name="test-workflow",
        cron_expression="* * * * *",  # fires every minute — reliably "due"
        is_active=True,
        max_active_runs=1,
    )
    defaults.update(overrides)

    workflow = Workflow(**defaults)
    db.add(workflow)
    db.commit()
    db.refresh(workflow)
    return workflow


def test_get_due_workflows_includes_active_fired_workflow(db_session):
    workflow = _make_workflow(db_session)
    now = workflow.created_at + timedelta(minutes=2)

    due = get_due_workflows(db_session, now)

    assert workflow.id in [w.id for w in due]


def test_get_due_workflows_excludes_inactive_workflow(db_session):
    workflow = _make_workflow(db_session, is_active=False)
    now = workflow.created_at + timedelta(minutes=2)

    due = get_due_workflows(db_session, now)

    assert workflow.id not in [w.id for w in due]


def test_get_due_workflows_excludes_workflow_with_no_cron(db_session):
    workflow = _make_workflow(db_session, cron_expression=None)
    now = workflow.created_at + timedelta(minutes=2)

    due = get_due_workflows(db_session, now)

    assert workflow.id not in [w.id for w in due]


async def test_poll_and_trigger_creates_workflow_run(db_session):
    # Create the workflow slightly in the past so that its next
    # cron execution has already occurred by the time poll_and_trigger()
    # checks for due workflows.
    workflow = _make_workflow(
        db_session,
        created_at=datetime.utcnow() - timedelta(minutes=2),
    )

    triggered = await poll_and_trigger(db_session)

    assert any(run.workflow_id == workflow.id for run in triggered)

    run = db_session.query(WorkflowRun).filter_by(workflow_id=workflow.id).one()
    assert run.status == WorkflowRunStatus.PENDING
    assert run.trigger_type == TriggerType.CRON


async def test_poll_and_trigger_respects_max_active_runs(db_session):
    workflow = _make_workflow(db_session, max_active_runs=1)

    # Simulate a previous run that's still in flight.
    existing_run = WorkflowRun(
        workflow_id=workflow.id,
        status=WorkflowRunStatus.RUNNING,
        trigger_type=TriggerType.CRON,
    )

    db_session.add(existing_run)
    db_session.commit()

    triggered = await poll_and_trigger(db_session)

    # max_active_runs=1 already met by the RUNNING run above — no new
    # run should be created for this workflow this cycle.
    assert not any(run.workflow_id == workflow.id for run in triggered)