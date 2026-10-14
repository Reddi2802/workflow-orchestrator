# tests/test_recovery.py
"""
Integration tests for Week 5 crash recovery (app/engine/recovery.py). Real
Postgres + real Redis, no mocking -- same standard as test_worker_pool.py.

Each test constructs TaskRun/WorkflowRun rows DIRECTLY at whatever status a
crash would have left them in, rather than running the normal worker-pool
flow and killing it mid-way (there's no clean way to simulate a process
death mid-flight in a test process) -- this is the same approach the
original Week 5 spec's own self-test describes ("mark task 1 SUCCESS,
leave the WorkflowRun as RUNNING, as if crashed mid-way").

See app/engine/recovery.py's module docstring for the full A-F scenario
list this file's tests map onto.
"""
from __future__ import annotations

import sys

from app.engine.recovery import recover_incomplete_runs
from app.engine.worker_pool import start_workflow_run
from app.models.entities import Task, TaskDependency, TaskRun, Workflow, WorkflowRun
from app.models.enums import TaskStatus, TriggerType, WorkflowRunStatus
from app.queue.redis_queue import dequeue_task


def _ok_command() -> str:
    return f'"{sys.executable}" -c "exit(0)"'


def _fail_command() -> str:
    return f'"{sys.executable}" -c "exit(1)"'


def _make_workflow(db, max_active_runs=1) -> Workflow:
    workflow = Workflow(name="recovery-test", max_active_runs=max_active_runs)
    db.add(workflow)
    db.commit()
    db.refresh(workflow)
    return workflow


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


def _make_running_workflow_run(db, workflow) -> WorkflowRun:
    """A WorkflowRun already at RUNNING -- as if start_workflow_run ran
    successfully before the crash, which is the scenario recovery exists
    for. Built directly rather than via start_workflow_run so each test
    controls exactly which TaskRuns exist and in what state."""
    run = WorkflowRun(
        workflow_id=workflow.id,
        status=WorkflowRunStatus.RUNNING,
        trigger_type=TriggerType.MANUAL,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _make_task_run(db, workflow_run, task, status, attempt_number=1):
    task_run = TaskRun(
        workflow_run_id=workflow_run.id,
        task_id=task.id,
        status=status,
        attempt_number=attempt_number,
    )
    db.add(task_run)
    db.commit()
    db.refresh(task_run)
    return task_run


def test_self_test_from_original_spec_success_before_crash_enqueues_both_children(db_session):
    """The exact scenario the Week 5 spec describes: a 3-task run (A root,
    B and C both depend on A), A succeeded but the crash happened before
    _enqueue_ready_downstream ran -- so B and C have no TaskRun rows at
    all. Recovery should create + enqueue both. This is scenario B from
    recovery.py's docstring."""
    workflow = _make_workflow(db_session)
    task_a = _make_task(db_session, workflow, "A")
    task_b = _make_task(db_session, workflow, "B")
    task_c = _make_task(db_session, workflow, "C")
    db_session.add_all([
        TaskDependency(upstream_task_id=task_a.id, downstream_task_id=task_b.id),
        TaskDependency(upstream_task_id=task_a.id, downstream_task_id=task_c.id),
    ])
    db_session.commit()

    run = _make_running_workflow_run(db_session, workflow)
    # A already succeeded -- but downstream propagation never ran (the
    # simulated crash). No TaskRun exists yet for B or C.
    _make_task_run(db_session, run, task_a, TaskStatus.SUCCESS)

    recovered_ids = recover_incomplete_runs(db_session)
    assert run.id in recovered_ids

    b_run = db_session.query(TaskRun).filter_by(workflow_run_id=run.id, task_id=task_b.id).one()
    c_run = db_session.query(TaskRun).filter_by(workflow_run_id=run.id, task_id=task_c.id).one()
    assert b_run.status == TaskStatus.PENDING
    assert c_run.status == TaskStatus.PENDING

    queued_ids = {dequeue_task(3), dequeue_task(3)}
    assert queued_ids == {b_run.id, c_run.id}
    assert dequeue_task(2) is None, "exactly 2 should have been enqueued, not more"


def test_running_task_reset_to_pending_same_attempt_not_a_new_attempt(db_session):
    """Scenario A. A crash isn't the task's own failure -- recovering a
    RUNNING row must NOT consume a retry attempt."""
    workflow = _make_workflow(db_session)
    task = _make_task(db_session, workflow, "solo", max_retries=0)
    run = _make_running_workflow_run(db_session, workflow)
    task_run = _make_task_run(db_session, run, task, TaskStatus.RUNNING)

    recover_incomplete_runs(db_session)

    db_session.refresh(task_run)
    assert task_run.status == TaskStatus.PENDING
    assert task_run.attempt_number == 1, "must stay the SAME attempt, not a new one"

    assert dequeue_task(5) == task_run.id
    # Confirm no second row was created for a "new attempt".
    all_runs = db_session.query(TaskRun).filter_by(workflow_run_id=run.id, task_id=task.id).all()
    assert len(all_runs) == 1


def test_retrying_task_missing_next_attempt_row_gets_created(db_session):
    """Scenario D. Crash happened during the backoff sleep, before the
    next attempt row was ever created."""
    workflow = _make_workflow(db_session)
    task = _make_task(db_session, workflow, "flaky", max_retries=2)
    run = _make_running_workflow_run(db_session, workflow)
    attempt_1 = _make_task_run(db_session, run, task, TaskStatus.RETRYING, attempt_number=1)

    recover_incomplete_runs(db_session)

    attempt_2 = db_session.query(TaskRun).filter_by(
        workflow_run_id=run.id, task_id=task.id, attempt_number=2
    ).one()
    assert attempt_2.status == TaskStatus.PENDING
    assert dequeue_task(5) == attempt_2.id

    # The original RETRYING row is untouched -- RETRYING is still
    # terminal-per-row, per the Week 3 design decision.
    db_session.refresh(attempt_1)
    assert attempt_1.status == TaskStatus.RETRYING


def test_pending_task_gets_reenqueued(db_session):
    """Scenario E. The row exists correctly; recovery can't know whether
    Redis still has it, so it re-enqueues unconditionally."""
    workflow = _make_workflow(db_session)
    task = _make_task(db_session, workflow, "solo")
    run = _make_running_workflow_run(db_session, workflow)
    task_run = _make_task_run(db_session, run, task, TaskStatus.PENDING)
    # Deliberately NOT enqueued here -- simulating the crash happening
    # between the row's commit and the enqueue_task call.

    recover_incomplete_runs(db_session)

    assert dequeue_task(5) == task_run.id


def test_permanently_failed_task_propagates_skip_to_downstream(db_session):
    """Scenario C. A failed permanently, crash happened before
    _skip_downstream_tasks ran -- B (downstream of A) has no row yet."""
    workflow = _make_workflow(db_session)
    task_a = _make_task(db_session, workflow, "A", command=_fail_command(), max_retries=0)
    task_b = _make_task(db_session, workflow, "B")
    db_session.add(TaskDependency(upstream_task_id=task_a.id, downstream_task_id=task_b.id))
    db_session.commit()

    run = _make_running_workflow_run(db_session, workflow)
    _make_task_run(db_session, run, task_a, TaskStatus.FAILED)

    recover_incomplete_runs(db_session)

    b_run = db_session.query(TaskRun).filter_by(workflow_run_id=run.id, task_id=task_b.id).one()
    assert b_run.status == TaskStatus.SKIPPED
    assert dequeue_task(2) is None, "a SKIPPED task must never be enqueued"


def test_everything_already_terminal_only_finalizes_workflow_run(db_session):
    """Not a 'something was lost' case -- every task genuinely finished
    before the crash, and only the final WorkflowRun.status commit was
    lost. Recovery should finalize the run without enqueueing anything."""
    workflow = _make_workflow(db_session)
    task = _make_task(db_session, workflow, "solo")
    run = _make_running_workflow_run(db_session, workflow)
    _make_task_run(db_session, run, task, TaskStatus.SUCCESS)

    recover_incomplete_runs(db_session)

    db_session.refresh(run)
    assert run.status == WorkflowRunStatus.SUCCESS
    assert run.completed_at is not None
    assert dequeue_task(2) is None


def test_pending_workflow_runs_are_not_touched_by_recovery(db_session):
    """Scenario F, implicitly: a WorkflowRun that never left PENDING is
    dispatch_loop's job, not recovery's. Confirms recovery only looks at
    RUNNING runs, not PENDING ones."""
    workflow = _make_workflow(db_session)
    _make_task(db_session, workflow, "solo")
    run = WorkflowRun(
        workflow_id=workflow.id,
        status=WorkflowRunStatus.PENDING,
        trigger_type=TriggerType.MANUAL,
    )
    db_session.add(run)
    db_session.commit()

    recovered_ids = recover_incomplete_runs(db_session)

    assert run.id not in recovered_ids
    db_session.refresh(run)
    assert run.status == WorkflowRunStatus.PENDING


def test_recovery_is_idempotent_running_twice_does_not_duplicate(db_session):
    """Calling recover_incomplete_runs twice in a row (e.g. two quick
    restarts) must not create duplicate rows or double-enqueue the same
    task_run_id beyond what's expected."""
    workflow = _make_workflow(db_session)
    task_a = _make_task(db_session, workflow, "A")
    task_b = _make_task(db_session, workflow, "B")
    db_session.add(TaskDependency(upstream_task_id=task_a.id, downstream_task_id=task_b.id))
    db_session.commit()

    run = _make_running_workflow_run(db_session, workflow)
    _make_task_run(db_session, run, task_a, TaskStatus.SUCCESS)

    recover_incomplete_runs(db_session)
    first_b = db_session.query(TaskRun).filter_by(workflow_run_id=run.id, task_id=task_b.id).one()
    dequeue_task(3)  # drain the one enqueue from the first call

    recover_incomplete_runs(db_session)
    all_b_runs = db_session.query(TaskRun).filter_by(workflow_run_id=run.id, task_id=task_b.id).all()
    assert len(all_b_runs) == 1, "second recovery pass must not create a duplicate row for B"
    assert all_b_runs[0].id == first_b.id

    # B is still PENDING (first recovery's enqueue was drained above, not
    # processed) -- scenario E applies on the second pass, so it gets
    # legitimately re-enqueued again. That's expected, not a bug: this is
    # the same "can't know what Redis still has" trade-off documented in
    # recovery.py for scenario E.
    assert dequeue_task(3) == first_b.id
