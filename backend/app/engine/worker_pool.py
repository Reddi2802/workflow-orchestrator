# app/engine/worker_pool.py
"""
Async worker pool: pulls task_run_ids off Paramash's Redis queue and
actually executes them.

This file owns two separable jobs -- read both docstrings below before
changing either, they're not interchangeable:

1. STARTING a run (dispatch_pending_runs / start_workflow_run).
   Nothing before Week 4 creates the first TaskRun rows for a WorkflowRun.
   scheduler.trigger_workflow_run() only creates the WorkflowRun itself at
   status=PENDING -- its own docstring says "the worker pool ... is what
   actually picks it up and moves it to RUNNING." That hand-off was never
   actually implemented. dispatch_pending_runs() is that implementation.

   Deliberately NOT folded into trigger_workflow_run(): that function is
   already merged and covered by test_poll_and_trigger_creates_workflow_run,
   which asserts status == PENDING right after creation. Changing it to also
   start the run would break that assertion. Keeping this as a separate pass
   means scheduler.py doesn't change at all.

2. RUNNING each attempt (process_task_run and everything it calls).
   Dequeue -> execute -> decide success/retry/permanent-failure -> walk the
   DAG forward -> check whether the whole run is now finished.

The per-attempt logic (start_workflow_run, process_task_run, and everything
they call) is synchronous, same reasoning as scheduler.py's
poll_and_trigger_sync: every DB call is a blocking SQLAlchemy Session call,
and subprocess.run() itself blocks. The two background loops at the bottom
of this file (dispatch_loop, worker_loop) are `async def`, matching
scheduler.run_polling_loop's exact convention -- the loop itself is the
asyncio task, and only each cycle's blocking work goes through
asyncio.to_thread. See the loops' own docstrings for why.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import redis

from app.engine.retry_policy import next_backoff_seconds, should_retry, validate_transition
from app.models.entities import Task, TaskDependency, TaskRun, WorkflowRun
from app.models.enums import TaskStatus, WorkflowRunStatus
from app.queue.redis_queue import dequeue_task, enqueue_task

logger = logging.getLogger(__name__)

DEFAULT_COMMAND_TIMEOUT_SECONDS = 300


# --- 1. Starting a run -------------------------------------------------------

def _root_task_ids(tasks: list[Task], dependencies: list[TaskDependency]) -> list[int]:
    """Tasks with no upstream dependency -- these are what a run starts
    with. Everything else waits for start_workflow_run/_enqueue_ready_downstream
    to unblock it once its upstream(s) succeed."""
    all_ids = {t.id for t in tasks}
    has_upstream = {dep.downstream_task_id for dep in dependencies}
    return [tid for tid in all_ids if tid not in has_upstream]


def start_workflow_run(db: Session, workflow_run: WorkflowRun) -> list[TaskRun]:
    """
    Move a PENDING WorkflowRun to RUNNING; create + enqueue attempt-1
    TaskRuns for every root task in its workflow.

    Guard: if this WorkflowRun already has any TaskRun at all, treat it as
    already started and do nothing. This isn't a real concurrency fix (two
    dispatchers racing on the exact same row could still both pass this
    check before either commits) -- see known_limitations.md -- but it
    stops the everyday case of dispatch_pending_runs running twice on a
    run that's already past PENDING.
    """
    existing = db.execute(
        select(TaskRun).where(TaskRun.workflow_run_id == workflow_run.id).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return []

    tasks = workflow_run.workflow.tasks
    task_ids = [t.id for t in tasks]
    dependencies = (
        db.execute(
            select(TaskDependency).where(TaskDependency.upstream_task_id.in_(task_ids))
        ).scalars().all()
        if task_ids
        else []
    )
    root_ids = _root_task_ids(tasks, dependencies)

    workflow_run.status = WorkflowRunStatus.RUNNING
    workflow_run.started_at = datetime.utcnow()

    created: list[TaskRun] = []
    for task_id in root_ids:
        task_run = TaskRun(
            workflow_run_id=workflow_run.id,
            task_id=task_id,
            status=TaskStatus.PENDING,
            attempt_number=1,
        )
        db.add(task_run)
        created.append(task_run)

    db.commit()
    for task_run in created:
        db.refresh(task_run)
        enqueue_task(task_run.id)

    return created


def dispatch_pending_runs(db: Session) -> list[WorkflowRun]:
    """Find every WorkflowRun still in PENDING and start it. Call this once
    before the worker loop starts, and again on the same cadence as the
    scheduler's poll -- a run can land in PENDING at any time."""
    pending_runs = db.execute(
        select(WorkflowRun).where(WorkflowRun.status == WorkflowRunStatus.PENDING)
    ).scalars().all()

    for run in pending_runs:
        start_workflow_run(db, run)
    return pending_runs


# --- 2. Running an attempt ----------------------------------------------------

def _execute_command(command: str) -> tuple[bool, str | None]:
    """Real subprocess call -- student-scoped choice per Week_4_Plan.md.
    Flagged there as worth confirming with Paramash if 'execute the
    command' ever needs to mean more than shelling out; nothing in the
    spec pins it down further, so this is the interim answer, not a final
    one. Returns (succeeded, error_message)."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return False, f"Command timed out after {exc.timeout}s"

    if result.returncode == 0:
        return True, None
    error_text = (result.stderr or result.stdout or f"exit code {result.returncode}")
    return False, error_text[:2000]  # cap length -- error_message is unbounded String but no reason to store megabytes


def _latest_attempt(db: Session, workflow_run_id: int, task_id: int) -> TaskRun | None:
    return db.execute(
        select(TaskRun)
        .where(TaskRun.workflow_run_id == workflow_run_id, TaskRun.task_id == task_id)
        .order_by(TaskRun.attempt_number.desc())
        .limit(1)
    ).scalar_one_or_none()


def _enqueue_ready_downstream(db: Session, completed_task_run: TaskRun) -> None:
    """After a TaskRun reaches SUCCESS: for each downstream task, check
    whether *every* one of its upstream tasks now has a SUCCESS latest
    attempt in this same WorkflowRun. This is the runtime DAG walk --
    topological_sort only computes a valid order in the abstract, it never
    looks at an actual TaskRun row."""
    downstream_deps = db.execute(
        select(TaskDependency).where(
            TaskDependency.upstream_task_id == completed_task_run.task_id
        )
    ).scalars().all()

    for dep in downstream_deps:
        downstream_task_id = dep.downstream_task_id

        upstream_ids = [
            d.upstream_task_id
            for d in db.execute(
                select(TaskDependency).where(
                    TaskDependency.downstream_task_id == downstream_task_id
                )
            ).scalars().all()
        ]

        all_upstream_succeeded = all(
            (latest := _latest_attempt(db, completed_task_run.workflow_run_id, uid)) is not None
            and latest.status == TaskStatus.SUCCESS
            for uid in upstream_ids
        )
        if not all_upstream_succeeded:
            continue  # still waiting on a sibling upstream task

        if _latest_attempt(db, completed_task_run.workflow_run_id, downstream_task_id) is not None:
            continue  # already started (or already skipped) by another path

        new_run = TaskRun(
            workflow_run_id=completed_task_run.workflow_run_id,
            task_id=downstream_task_id,
            status=TaskStatus.PENDING,
            attempt_number=1,
        )
        db.add(new_run)
        try:
            db.commit()
        except IntegrityError:
            # Two sibling upstream tasks finished concurrently and both
            # reached this point before either committed -- the unique
            # constraint on (workflow_run_id, task_id, attempt_number)
            # catches the duplicate. The other worker's insert wins;
            # nothing further to do here.
            db.rollback()
            continue
        db.refresh(new_run)
        enqueue_task(new_run.id)


def _skip_downstream_tasks(db: Session, failed_task_run: TaskRun) -> None:
    """After a TaskRun reaches FAILED with no retries left: everything
    downstream of it (transitively, through the whole subgraph) can never
    run. Persist that as SKIPPED TaskRun rows instead of leaving a silent
    gap -- run-history views need something to render, and
    _maybe_finalize_workflow_run below needs every task accounted for to
    know the run is actually done."""
    workflow_run_id = failed_task_run.workflow_run_id
    frontier = [failed_task_run.task_id]
    visited = {failed_task_run.task_id}

    while frontier:
        task_id = frontier.pop()
        downstream_deps = db.execute(
            select(TaskDependency).where(TaskDependency.upstream_task_id == task_id)
        ).scalars().all()

        for dep in downstream_deps:
            downstream_id = dep.downstream_task_id
            if downstream_id in visited:
                continue
            visited.add(downstream_id)

            if _latest_attempt(db, workflow_run_id, downstream_id) is not None:
                continue  # already has a row (e.g. also reachable via a non-failed path)

            db.add(TaskRun(
                workflow_run_id=workflow_run_id,
                task_id=downstream_id,
                status=TaskStatus.SKIPPED,
                attempt_number=1,
            ))
            frontier.append(downstream_id)

    db.commit()


def _maybe_finalize_workflow_run(db: Session, workflow_run_id: int) -> None:
    """Check whether every task in this run's workflow has reached a
    terminal latest-attempt status (SUCCESS / FAILED / SKIPPED). If so,
    set WorkflowRun.status and completed_at -- this is the real-completion
    path known_limitations.md #1 flagged as untested until this module
    existed. If any task is still PENDING/RUNNING/RETRYING, or hasn't
    started at all, do nothing and return -- the run isn't done yet."""
    workflow_run = db.get(WorkflowRun, workflow_run_id)
    task_ids = [t.id for t in workflow_run.workflow.tasks]

    terminal_statuses: list[TaskStatus] = []
    for task_id in task_ids:
        latest = _latest_attempt(db, workflow_run_id, task_id)
        if latest is None:
            return  # a task hasn't even been reached yet
        if latest.status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRYING):
            return  # still in flight
        terminal_statuses.append(latest.status)

    any_failed = TaskStatus.FAILED in terminal_statuses
    any_succeeded = TaskStatus.SUCCESS in terminal_statuses

    if not any_failed:
        workflow_run.status = WorkflowRunStatus.SUCCESS
    elif any_succeeded:
        workflow_run.status = WorkflowRunStatus.PARTIAL
    else:
        workflow_run.status = WorkflowRunStatus.FAILED

    workflow_run.completed_at = datetime.utcnow()
    db.commit()


def process_task_run(db: Session, task_run_id: int) -> None:
    """Everything that happens to a single dequeued task_run_id: mark
    RUNNING, execute, then branch on success / retryable failure /
    permanent failure. Each branch ends by touching the two things that
    make the run move forward: the DAG walk and the finalize check."""
    task_run = db.get(TaskRun, task_run_id)
    if task_run is None:
        logger.error("Dequeued task_run_id %s does not exist -- dropping", task_run_id)
        return
    task = db.get(Task, task_run.task_id)

    validate_transition(task_run.status, TaskStatus.RUNNING)
    task_run.status = TaskStatus.RUNNING
    task_run.started_at = datetime.utcnow()
    db.commit()

    succeeded, error_message = _execute_command(task.command)

    if succeeded:
        validate_transition(task_run.status, TaskStatus.SUCCESS)
        task_run.status = TaskStatus.SUCCESS
        task_run.completed_at = datetime.utcnow()
        # Minimal recovery breadcrumb -- full checkpoint semantics are
        # Week 5's job, this just avoids checkpoint_data sitting NULL
        # forever for a row that clearly finished.
        task_run.checkpoint_data = {"completed_attempt": task_run.attempt_number}
        db.commit()

        _enqueue_ready_downstream(db, task_run)
        _maybe_finalize_workflow_run(db, task_run.workflow_run_id)
        return

    # should_retry's contract (see its docstring in retry_policy.py) is
    # "this TaskRun IS in FAILED" -- it decides whether a failed attempt
    # earns a follow-up, not whether this attempt failed. But the
    # transition graph forbids FAILED -> RETRYING (FAILED has no outgoing
    # transitions): a row that will be retried must go straight from
    # RUNNING to RETRYING and never pass through FAILED at all. Squaring
    # those two facts: set status to FAILED in memory only (not committed
    # yet) purely so should_retry can read it, then overwrite with
    # whichever status actually gets validated/committed below. Only one
    # status ever reaches the database for this row.
    original_status = task_run.status  # RUNNING
    task_run.status = TaskStatus.FAILED  # tentative -- for should_retry's read only
    will_retry = should_retry(task, task_run)

    if will_retry:
        validate_transition(original_status, TaskStatus.RETRYING)
        task_run.status = TaskStatus.RETRYING
        task_run.completed_at = datetime.utcnow()
        task_run.error_message = error_message
        db.commit()

        backoff = next_backoff_seconds(task_run.attempt_number, task.retry_backoff_seconds)
        # Blocking sleep is fine here -- this function runs inside a
        # worker thread dispatched via asyncio.to_thread (see
        # worker_loop_sync below), not on the event loop, so it only
        # stalls this one worker's next dequeue, not the whole app.
        time.sleep(backoff)

        next_attempt = TaskRun(
            workflow_run_id=task_run.workflow_run_id,
            task_id=task_run.task_id,
            status=TaskStatus.PENDING,
            attempt_number=task_run.attempt_number + 1,
        )
        db.add(next_attempt)
        db.commit()
        db.refresh(next_attempt)
        enqueue_task(next_attempt.id)
        return

    # Permanent failure: no more retries. task_run.status is already
    # FAILED from the tentative assignment above; validate against the
    # status the row actually held before this branch (RUNNING), not
    # against the tentative value we just set it to.
    validate_transition(original_status, TaskStatus.FAILED)
    task_run.completed_at = datetime.utcnow()
    task_run.error_message = error_message
    db.commit()

    _skip_downstream_tasks(db, task_run)
    _maybe_finalize_workflow_run(db, task_run.workflow_run_id)


# --- 3. Background loops (mirrors scheduler.run_polling_loop exactly) -------
#
# Convention taken directly from app/engine/scheduler.py: the outer loop
# stays `async def` and IS the asyncio task (created via
# asyncio.create_task in main.py's lifespan hook). Only each cycle's
# blocking work goes through asyncio.to_thread. This is what lets
# stop_event actually interrupt the loop promptly on shutdown, instead of
# the whole loop being stuck inside one giant synchronous call that
# can't be cancelled from outside until it happens to return on its own.

DEFAULT_DISPATCH_INTERVAL_SECONDS = 2  # deliberately shorter than the
# scheduler's cron-poll interval (typically ~60s) -- that interval exists
# to match cron granularity, which doesn't need to be fast. This one is
# pure startup latency for every run, cron-triggered or not, and the
# underlying query (find PENDING WorkflowRuns) is cheap, so there's no
# reason to inherit the scheduler's slower cadence here.

DEFAULT_WORKER_DEQUEUE_TIMEOUT_SECONDS = 5
DEFAULT_WORKER_POOL_SIZE = 2


async def dispatch_loop(
    session_factory,
    interval_seconds: int = DEFAULT_DISPATCH_INTERVAL_SECONDS,
    stop_event=None,
) -> None:
    """Background task: repeatedly calls dispatch_pending_runs so a
    WorkflowRun never sits at PENDING for longer than one interval."""
    while stop_event is None or not stop_event.is_set():
        db = session_factory()
        try:
            await asyncio.to_thread(dispatch_pending_runs, db)
        except Exception:
            logger.exception("Dispatch cycle failed")
        finally:
            db.close()

        try:
            await asyncio.wait_for(
                stop_event.wait() if stop_event else asyncio.sleep(interval_seconds),
                timeout=interval_seconds,
            )
        except asyncio.TimeoutError:
            pass


def _worker_cycle_sync(session_factory, dequeue_timeout_seconds: int) -> None:
    """One dequeue-and-process cycle -- the unit of work dispatched via
    asyncio.to_thread on each worker_loop iteration. dequeue_task itself
    is a blocking BRPOP with a timeout, so it already doubles as this
    loop's 'wait between cycles' -- no separate sleep needed the way
    dispatch_loop needs one.

    dequeue_task's own docstring claims it never raises on timeout, but
    that's not reliable in practice -- redis-py's blocking commands carry
    a client-side protective socket timeout that can trip under load,
    raising redis.exceptions.TimeoutError instead of returning None.
    Confirmed via live testing this fires on essentially every empty-queue
    cycle, not as a rare fluke -- so it's handled as two distinct cases,
    not one generic catch-all:

    - redis.exceptions.TimeoutError: functionally identical to a
      legitimate "nothing arrived" timeout from this worker's point of
      view. Swallowed silently, no log line -- logging a full stack trace
      every ~5s during ordinary idle time would bury every other log line
      that actually matters. The real fix belongs in redis_queue.py
      (Paramash's file, already merged) -- flagged separately, not
      patched here, since it's not this file's to change unilaterally.
    - anything else (e.g. a real ConnectionError because Redis is down):
      still logged loudly. Losing visibility into an actual outage to
      avoid noise from a known, benign, already-diagnosed timeout race
      would trade one problem for a worse one.
    """
    try:
        task_run_id = dequeue_task(dequeue_timeout_seconds)
    except redis.exceptions.TimeoutError:
        return
    except Exception:
        logger.exception("dequeue_task failed with an unexpected error")
        return

    if task_run_id is None:
        return

    db = session_factory()
    try:
        process_task_run(db, task_run_id)
    except Exception:
        logger.exception("Worker failed processing task_run_id %s", task_run_id)
    finally:
        db.close()


async def worker_loop(
    session_factory,
    dequeue_timeout_seconds: int = DEFAULT_WORKER_DEQUEUE_TIMEOUT_SECONDS,
    stop_event=None,
) -> None:
    """One worker's main loop -- same shape as run_polling_loop and
    dispatch_loop above. Launch several of these (see DEFAULT_WORKER_POOL_SIZE)
    as separate asyncio tasks from main.py's lifespan hook to get an actual
    pool rather than a single worker."""
    while stop_event is None or not stop_event.is_set():
        await asyncio.to_thread(_worker_cycle_sync, session_factory, dequeue_timeout_seconds)
