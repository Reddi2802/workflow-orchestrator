# tests/test_worker_pool.py
"""
Integration tests for the worker pool. Real Postgres, no mocking of the DB
layer -- same standard as test_scheduler_integration.py.

Commands use sys.executable rather than shell built-ins like `true`/`false`:
this project runs on Windows/PowerShell as well as Linux, and `true`/`false`
aren't real commands under cmd.exe. `python -c "exit(N)"` is portable.

Redis: these tests call the real enqueue_task/dequeue_task against the
Redis container, consistent with "verify at the database/queue level, not
a mock." Run docker compose up -d before running this file.
"""
from __future__ import annotations

import sys

from app.engine.worker_pool import (
    dispatch_pending_runs,
    process_task_run,
    start_workflow_run,
)
from app.models.entities import Task, TaskDependency, Workflow, WorkflowRun
from app.models.enums import TaskStatus, TriggerType, WorkflowRunStatus
from app.queue.redis_queue import dequeue_task


def _ok_command() -> str:
    return f'"{sys.executable}" -c "exit(0)"'


def _fail_command() -> str:
    return f'"{sys.executable}" -c "exit(1)"'


def _make_workflow(db, max_active_runs=1) -> Workflow:
    workflow = Workflow(name="wp-test", max_active_runs=max_active_runs)
    db.add(workflow)
    db.commit()
    db.refresh(workflow)
    return workflow


def _make_run(db, workflow) -> WorkflowRun:
    run = WorkflowRun(
        workflow_id=workflow.id,
        status=WorkflowRunStatus.PENDING,
        trigger_type=TriggerType.MANUAL,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _make_task(db, workflow, name, command=None, max_retries=0, retry_backoff_seconds=0):
    task = Task(
        workflow_id=workflow.id,
        name=name,
        command=command or _ok_command(),
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _drain_and_process(db, expected_count, timeout=5):
    """Pull exactly `expected_count` task_run_ids off the queue and process
    each one, in the order the queue delivers them. process_task_run may
    itself enqueue more (downstream tasks, retries) -- callers that need
    to walk multiple DAG levels call this again for the next wave."""
    processed = []
    for _ in range(expected_count):
        task_run_id = dequeue_task(timeout)
        assert task_run_id is not None, "expected a task_run_id, queue was empty"
        process_task_run(db, task_run_id)
        processed.append(task_run_id)
    return processed


def test_start_workflow_run_enqueues_only_root_tasks(db_session):
    """A -> B chain: starting the run should create+enqueue A only, not B."""
    workflow = _make_workflow(db_session)
    task_a = _make_task(db_session, workflow, "A")
    task_b = _make_task(db_session, workflow, "B")
    db_session.add(TaskDependency(upstream_task_id=task_a.id, downstream_task_id=task_b.id))
    db_session.commit()

    run = _make_run(db_session, workflow)
    created = start_workflow_run(db_session, run)

    assert [tr.task_id for tr in created] == [task_a.id]
    assert run.status == WorkflowRunStatus.RUNNING

    task_run_id = dequeue_task(5)
    assert task_run_id == created[0].id


def test_downstream_task_starts_only_after_upstream_success(db_session):
    workflow = _make_workflow(db_session)
    task_a = _make_task(db_session, workflow, "A")
    task_b = _make_task(db_session, workflow, "B")
    db_session.add(TaskDependency(upstream_task_id=task_a.id, downstream_task_id=task_b.id))
    db_session.commit()

    run = _make_run(db_session, workflow)
    start_workflow_run(db_session, run)

    # Process A (succeeds) -- this should be the trigger that enqueues B.
    _drain_and_process(db_session, expected_count=1)

    b_run_id = dequeue_task(5)
    assert b_run_id is not None, "B should have been enqueued after A succeeded"

    from app.models.entities import TaskRun
    b_task_run = db_session.get(TaskRun, b_run_id)
    assert b_task_run.task_id == task_b.id
    assert b_task_run.attempt_number == 1


def test_diamond_dag_executes_in_correct_order_and_run_succeeds(db_session):
    """A -> B, A -> C, B -> D, C -> D. D should only start once BOTH B and C
    have succeeded, and the WorkflowRun should finalize to SUCCESS."""
    workflow = _make_workflow(db_session)
    a = _make_task(db_session, workflow, "A")
    b = _make_task(db_session, workflow, "B")
    c = _make_task(db_session, workflow, "C")
    d = _make_task(db_session, workflow, "D")
    db_session.add_all([
        TaskDependency(upstream_task_id=a.id, downstream_task_id=b.id),
        TaskDependency(upstream_task_id=a.id, downstream_task_id=c.id),
        TaskDependency(upstream_task_id=b.id, downstream_task_id=d.id),
        TaskDependency(upstream_task_id=c.id, downstream_task_id=d.id),
    ])
    db_session.commit()

    run = _make_run(db_session, workflow)
    start_workflow_run(db_session, run)

    _drain_and_process(db_session, expected_count=1)  # A
    _drain_and_process(db_session, expected_count=2)  # B and C, order not guaranteed

    d_run_id = dequeue_task(5)
    assert d_run_id is not None, "D should start once both B and C have succeeded"
    process_task_run(db_session, d_run_id)

    db_session.refresh(run)
    assert run.status == WorkflowRunStatus.SUCCESS
    assert run.completed_at is not None


def test_retry_sequence_matches_max_retries_then_fails(db_session):
    """max_retries=2 -> 3 total attempts (Airflow convention, per the Week 3
    design decision). Attempt 1 fails -> RETRYING + new row. Attempt 2
    fails -> RETRYING + new row. Attempt 3 fails -> FAILED, stop."""
    workflow = _make_workflow(db_session)
    task = _make_task(
        db_session, workflow, "flaky",
        command=_fail_command(), max_retries=2, retry_backoff_seconds=0,
    )
    run = _make_run(db_session, workflow)
    start_workflow_run(db_session, run)

    from app.models.entities import TaskRun

    # Attempt 1
    run_id_1 = dequeue_task(5)
    process_task_run(db_session, run_id_1)
    attempt_1 = db_session.get(TaskRun, run_id_1)
    assert attempt_1.status == TaskStatus.RETRYING

    # Attempt 2 (new row, enqueued by attempt 1's failure)
    run_id_2 = dequeue_task(5)
    assert run_id_2 != run_id_1
    process_task_run(db_session, run_id_2)
    attempt_2 = db_session.get(TaskRun, run_id_2)
    assert attempt_2.attempt_number == 2
    assert attempt_2.status == TaskStatus.RETRYING

    # Attempt 3 -- max_retries=2 means attempt_number<=2 retries, so this
    # is the last one: attempt_number=3 > max_retries=2 -> should_retry False.
    run_id_3 = dequeue_task(5)
    process_task_run(db_session, run_id_3)
    attempt_3 = db_session.get(TaskRun, run_id_3)
    assert attempt_3.attempt_number == 3
    assert attempt_3.status == TaskStatus.FAILED

    # No 4th attempt should ever be enqueued.
    assert dequeue_task(2) is None

    db_session.refresh(run)
    assert run.status == WorkflowRunStatus.FAILED


def test_permanent_failure_skips_downstream_tasks(db_session):
    """A fails permanently (max_retries=0) -> B (downstream of A) should be
    recorded SKIPPED, never enqueued, and the run should finalize FAILED."""
    workflow = _make_workflow(db_session)
    task_a = _make_task(db_session, workflow, "A", command=_fail_command(), max_retries=0)
    task_b = _make_task(db_session, workflow, "B")
    db_session.add(TaskDependency(upstream_task_id=task_a.id, downstream_task_id=task_b.id))
    db_session.commit()

    run = _make_run(db_session, workflow)
    start_workflow_run(db_session, run)

    a_run_id = dequeue_task(5)
    process_task_run(db_session, a_run_id)

    assert dequeue_task(2) is None, "B must never be enqueued after A's permanent failure"

    from app.models.entities import TaskRun
    b_task_run = db_session.query(TaskRun).filter_by(
        workflow_run_id=run.id, task_id=task_b.id
    ).one()
    assert b_task_run.status == TaskStatus.SKIPPED

    db_session.refresh(run)
    assert run.status == WorkflowRunStatus.FAILED


def test_max_active_runs_allows_new_run_after_completion(db_session):
    """The gap flagged in known_limitations.md #1: once a run actually
    completes (via the worker pool, not a manually-inserted row), a second
    run for the same workflow should no longer be blocked by max_active_runs.
    """
    from app.engine.scheduler import _active_run_count

    workflow = _make_workflow(db_session, max_active_runs=1)
    _make_task(db_session, workflow, "solo")

    run_1 = _make_run(db_session, workflow)
    start_workflow_run(db_session, run_1)
    assert _active_run_count(db_session, workflow.id) == 1

    run_id = dequeue_task(5)
    process_task_run(db_session, run_id)

    db_session.refresh(run_1)
    assert run_1.status == WorkflowRunStatus.SUCCESS

    # Now that run_1 is no longer PENDING/RUNNING, a new run shouldn't be blocked.
    assert _active_run_count(db_session, workflow.id) == 0


def test_dispatch_pending_runs_starts_a_run_created_by_the_scheduler(db_session):
    """End-to-end of the gap this file closes: a PENDING WorkflowRun (as if
    just created by trigger_workflow_run) gets picked up and started."""
    workflow = _make_workflow(db_session)
    _make_task(db_session, workflow, "solo")
    run = _make_run(db_session, workflow)
    assert run.status == WorkflowRunStatus.PENDING

    dispatch_pending_runs(db_session)

    db_session.refresh(run)
    assert run.status == WorkflowRunStatus.RUNNING
    assert dequeue_task(5) is not None
