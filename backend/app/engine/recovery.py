# app/engine/recovery.py
"""
Week 5: crash recovery. If the whole process dies mid-run (laptop sleeps,
uvicorn killed, power loss), a WorkflowRun can be left at RUNNING forever,
with some TaskRuns genuinely finished and others abandoned mid-flight, or
never even written/enqueued. recover_incomplete_runs() is called once at
app startup (see main.py's lifespan, BEFORE dispatch_loop/worker_loop
start) to detect that state and resume correctly -- re-enqueueing only
what's actually unfinished, not restarting the whole run from scratch and
not leaving it stuck forever.

Design note -- "crashed mid-run" is not one scenario, it's several, because
the DB writes and the Redis enqueue_task calls in worker_pool.py are two
separate operations, not one atomic transaction. A crash can land in the
gap between them. This module handles every scenario identified during
planning:

  A. TaskRun still RUNNING when the process died.
     -> Reset to PENDING at the SAME attempt_number and re-enqueue. NOT a
        new attempt row -- a crash is not the task's own failure, and
        charging it against max_retries would be wrong. This requires
        going around retry_policy.validate_transition (RUNNING -> PENDING
        is not a legal transition in normal operation, deliberately, per
        the Week 3 design note in worker_pool.py). That's intentional:
        the validator's job is to catch application bugs during NORMAL
        operation; crash recovery is reconciling state after an ABNORMAL
        process death, a fundamentally different kind of transition with
        its own justification. Going around it silently would be a bug;
        going around it with this comment is a deliberate, documented
        exception -- the one and only place in the codebase this happens.

  B. TaskRun SUCCESS committed, crash before _enqueue_ready_downstream ran
     -> downstream task has no row at all.
  C. TaskRun FAILED (permanent) committed, crash before
     _skip_downstream_tasks ran -> downstream task has no row at all.
     B and C are both handled the same way: for every task whose latest
     attempt is a terminal status (SUCCESS or FAILED), re-run the exact
     same downstream-propagation call the live path would have run
     (_enqueue_ready_downstream / _skip_downstream_tasks from
     worker_pool.py, reused directly rather than reimplemented, so any
     future fix to that logic automatically applies here too). Both of
     those functions are already idempotent -- re-running them against a
     downstream task that was already properly created is a safe no-op
     (they check _latest_attempt is None before creating anything, and
     catch the unique-constraint IntegrityError for the concurrent case).

  D. TaskRun RETRYING, crash during the backoff time.sleep() -- before the
     next-attempt row was created.
     -> Create the missing next-attempt row (PENDING, attempt_number + 1)
        and enqueue it. Safe to assume it's missing: _latest_attempt
        returns the row with the highest attempt_number, so if a newer
        row already existed, THAT row (not this RETRYING one) would have
        been returned in the first place.

  E. TaskRun PENDING, crash before enqueue_task was called (or Redis
     itself is uncertain -- recovery has no way to distinguish "never
     enqueued" from "enqueued, and still safely sitting in Redis since
     Redis didn't crash too").
     -> Re-enqueue unconditionally. This can occasionally cause a genuine
        duplicate (if the row actually was already in the queue and
        survived). That's an accepted, documented trade-off, not
        silently ignored: a duplicate dequeue of the same task_run_id
        hits process_task_run's own validate_transition call on whatever
        status that row is ALREADY in by the time the second dequeue is
        processed (e.g. validate_transition(RUNNING, RUNNING) or
        validate_transition(SUCCESS, RUNNING) are both invalid) --
        process_task_run raises ValueError, the worker's own try/except
        catches and logs it, nothing is corrupted. A fail-safe outcome
        (one wasted queue slot, one log line), not a fail-silent one.
        Building real queue-level idempotency (dedup keys, etc.) to avoid
        this entirely is beyond this project's scope -- see
        known_limitations.md.

  F. WorkflowRun never left PENDING at all (crash before
     dispatch_pending_runs/start_workflow_run ran).
     -> Not handled here, and doesn't need to be: dispatch_loop already
        picks up any PENDING WorkflowRun on its own normal 2s cadence,
        crash or no crash. recover_incomplete_runs() only looks at
        WorkflowRuns already in RUNNING.

Explicitly NOT handled -- see known_limitations.md:
  - A RUNNING TaskRun's underlying subprocess could, in principle, still
    be alive as an orphaned process on the machine even though the worker
    that launched it died (subprocess.run() is synchronous, but if the OS
    process hosting the whole app is killed outright -- not just the
    Python thread -- a child process's exact fate depends on the OS and
    how it was launched). Recovery has no PID tracking and can't tell the
    difference between "truly dead" and "orphaned but still running."
    Scenario A above re-runs the command regardless. For a non-idempotent
    command this could cause double-execution. Treated as an accepted
    student-capstone-scope limitation, not a bug to silently paper over --
    real process-group/PID tracking (what Airflow actually does) is out
    of scope here.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.dag import topological_sort
from app.engine.worker_pool import (
    _enqueue_ready_downstream,
    _latest_attempt,
    _maybe_finalize_workflow_run,
    _skip_downstream_tasks,
)
from app.models.entities import Task, TaskDependency, TaskRun, WorkflowRun
from app.models.enums import TaskStatus, WorkflowRunStatus
from app.queue.redis_queue import enqueue_task

logger = logging.getLogger(__name__)


def _dependencies_for_tasks(db: Session, tasks: list[Task]) -> list[TaskDependency]:
    """Same join-through-Task pattern used everywhere else in this codebase
    since the Section 3 schema correction removed TaskDependency.workflow_id."""
    task_ids = [t.id for t in tasks]
    if not task_ids:
        return []
    return db.execute(
        select(TaskDependency).where(TaskDependency.upstream_task_id.in_(task_ids))
    ).scalars().all()


def _recover_workflow_run(db: Session, workflow_run: WorkflowRun) -> int:
    """Walk one stuck-RUNNING WorkflowRun's tasks in topological order,
    reconciling each one per the A-E scenarios in this module's docstring.
    Returns the number of TaskRuns actually re-enqueued (0 if the run
    turns out to have genuinely nothing left to do -- see the finalize
    call at the end, which covers the case where everything had already
    finished and only the WorkflowRun's own status update was lost).

    Topological order matters here, not just for tidiness: because we
    process every task's terminal-status propagation (via
    _enqueue_ready_downstream / _skip_downstream_tasks) as we pass
    through it, by the time we reach a downstream task in this same pass,
    any row it needed created by an upstream task earlier in the SAME
    pass has already been created. One pass is enough -- no fixed-point
    iteration needed.

    Bug found via live testing, fixed here: a row _enqueue_ready_downstream
    creates for a downstream task (because its upstream just turned out to
    be SUCCESS, above) is PENDING and already correctly enqueued at the
    moment it's created. But this SAME loop still visits that downstream
    task on its own later iteration (topological order puts it after its
    upstream) -- and without the snapshot below, finding a PENDING row
    there looks indistinguishable from scenario E (a genuine pre-crash
    orphan), so the loop's own PENDING branch would enqueue it AGAIN.
    Confirmed via the self-test from the original Week 5 spec: a 3-task
    A->B, A->C run doubled B and C's enqueues from 2 to 4 before this fix.
    The snapshot (pre_pass_latest_id) records each task's latest-attempt
    id BEFORE this pass touches anything; a task whose current latest id
    doesn't match its snapshot value was created DURING this pass and is
    skipped -- it was already fully handled (enqueued or SKIPPED) at the
    moment _enqueue_ready_downstream/_skip_downstream_tasks created it.
    """
    tasks = workflow_run.workflow.tasks
    dependencies = _dependencies_for_tasks(db, tasks)

    try:
        ordered_tasks = topological_sort(tasks, dependencies)
    except Exception:
        # Shouldn't happen -- has_cycle() already rejects a cyclic graph
        # at workflow-creation time (POST /workflows), so a WorkflowRun
        # should never exist for a cyclic workflow in the first place.
        # If this ever fires, something upstream of recovery is already
        # badly wrong -- log loudly and skip this run rather than crash
        # the whole startup sequence over one corrupted workflow.
        logger.exception(
            "Workflow %s (run %s) failed topological_sort during recovery "
            "-- skipping this run, investigate the workflow's dependency graph",
            workflow_run.workflow_id, workflow_run.id,
        )
        return 0

    # downstream_task_id -> [upstream_task_id, ...], built once rather than
    # re-querying per task.
    upstream_of: dict[int, list[int]] = {}
    for dep in dependencies:
        upstream_of.setdefault(dep.downstream_task_id, []).append(dep.upstream_task_id)

    # Snapshot each task's latest-attempt id BEFORE this pass changes
    # anything -- see this function's docstring for why this is required,
    # not optional.
    pre_pass_latest_id: dict[int, int | None] = {}
    for task in ordered_tasks:
        snapshot_latest = _latest_attempt(db, workflow_run.id, task.id)
        pre_pass_latest_id[task.id] = snapshot_latest.id if snapshot_latest else None

    re_enqueued_count = 0

    for task in ordered_tasks:
        latest = _latest_attempt(db, workflow_run.id, task.id)

        if latest is not None and latest.id != pre_pass_latest_id[task.id]:
            # This row did not exist when the pass started -- an earlier
            # task's turn in THIS SAME loop created it (via
            # _enqueue_ready_downstream or _skip_downstream_tasks) and
            # already enqueued or SKIPPED it correctly at creation time.
            # Processing it again here would double-enqueue it.
            continue

        if latest is None:
            # No row at all. In every case EXCEPT a root task, this is
            # expected and fine -- if this task's upstream situation
            # warranted a row existing, an earlier iteration of THIS SAME
            # loop (processing that upstream task, since topological order
            # guarantees upstream-before-downstream) already created it via
            # _enqueue_ready_downstream or _skip_downstream_tasks below.
            # Reaching here with no row and no upstream means this is a
            # root task that was never started at all -- defensive-only
            # path: start_workflow_run creates all root TaskRuns in the
            # same DB commit that sets WorkflowRun.status = RUNNING, so a
            # RUNNING WorkflowRun should never actually have a rootless
            # root task. Logged loudly specifically because it means that
            # invariant broke somewhere.
            if not upstream_of.get(task.id):
                logger.warning(
                    "Recovery: root task %s (workflow_run %s) has no TaskRun "
                    "at all despite the run being RUNNING -- this should be "
                    "impossible per start_workflow_run's atomicity. Creating "
                    "attempt 1 now.",
                    task.id, workflow_run.id,
                )
                new_run = TaskRun(
                    workflow_run_id=workflow_run.id,
                    task_id=task.id,
                    status=TaskStatus.PENDING,
                    attempt_number=1,
                )
                db.add(new_run)
                db.commit()
                db.refresh(new_run)
                enqueue_task(new_run.id)
                re_enqueued_count += 1
            continue

        if latest.status == TaskStatus.RUNNING:
            # Scenario A. Deliberately bypasses validate_transition -- see
            # this module's docstring for why. Same attempt_number, not a
            # new attempt: a crash isn't this task's own failure.
            latest.status = TaskStatus.PENDING
            db.commit()
            enqueue_task(latest.id)
            re_enqueued_count += 1

        elif latest.status == TaskStatus.RETRYING:
            # Scenario D. By definition no newer row exists yet (see this
            # function's docstring on why _latest_attempt guarantees that).
            next_attempt = TaskRun(
                workflow_run_id=workflow_run.id,
                task_id=task.id,
                status=TaskStatus.PENDING,
                attempt_number=latest.attempt_number + 1,
            )
            db.add(next_attempt)
            db.commit()
            db.refresh(next_attempt)
            enqueue_task(next_attempt.id)
            re_enqueued_count += 1

        elif latest.status == TaskStatus.PENDING:
            # Scenario E. Unconditional re-enqueue -- see docstring for the
            # accepted possible-duplicate trade-off.
            enqueue_task(latest.id)
            re_enqueued_count += 1

        elif latest.status == TaskStatus.SUCCESS:
            # Scenario B. Idempotent -- safe even if downstream already
            # exists and was already enqueued normally.
            _enqueue_ready_downstream(db, latest)

        elif latest.status == TaskStatus.FAILED:
            # Scenario C. Idempotent -- safe even if downstream was
            # already marked SKIPPED normally.
            _skip_downstream_tasks(db, latest)

        # SKIPPED: nothing further needed. The transitive walk from
        # _skip_downstream_tasks (called from whichever upstream task
        # actually failed, earlier in this same topological pass) already
        # covers this task's own downstream subgraph.

    # Whether or not anything above needed fixing, the WorkflowRun's own
    # status might just be stale -- e.g. every task actually finished
    # before the crash and only the final status-and-completed_at commit
    # in _maybe_finalize_workflow_run was lost. Always check.
    _maybe_finalize_workflow_run(db, workflow_run.id)

    return re_enqueued_count


def recover_incomplete_runs(db: Session) -> list[int]:
    """Entry point: call once at app startup, BEFORE dispatch_loop/worker_loop
    start pulling from the queue (see main.py's lifespan) -- recovery
    creating/enqueueing rows at the same time a worker is already dequeuing
    is a real race (Gap 3 from planning); running recovery to completion
    first, synchronously, avoids it entirely rather than relying on the
    dedup guards inside _enqueue_ready_downstream/_skip_downstream_tasks to
    absorb it (they can absorb a CONCURRENT race, but there's no reason to
    rely on that when a simple ordering guarantee removes the race
    outright).

    Returns the list of WorkflowRun ids that were actually examined (i.e.
    were found stuck in RUNNING), for startup logging -- not every id in
    this list necessarily had something wrong; some may have turned out to
    just need the finalize check.
    """
    stuck_runs = db.execute(
        select(WorkflowRun).where(WorkflowRun.status == WorkflowRunStatus.RUNNING)
    ).scalars().all()

    for workflow_run in stuck_runs:
        re_enqueued = _recover_workflow_run(db, workflow_run)
        logger.info(
            "Recovery: WorkflowRun %s -- %d TaskRun(s) re-enqueued",
            workflow_run.id, re_enqueued,
        )

    return [run.id for run in stuck_runs]
