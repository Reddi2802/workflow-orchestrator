# tests/test_duplicate_dequeue.py
"""
known_limitations.md item 9: the same task_run_id can sit in the Redis queue
twice (recovery re-enqueues every PENDING row unconditionally), and two
workers can dequeue the two copies at nearly the same moment.

Old behaviour: process_task_run read the row with an unlocked db.get() and
checked "is it PENDING?" in Python, then committed RUNNING as a separate
step. Two workers could both pass the check while the row was still PENDING,
and the command ran twice.

Why these tests are NOT a simple "dequeue the same id twice in a row": two
back-to-back dequeues already behaved correctly before the fix, because the
second one saw RUNNING/SUCCESS and validate_transition raised. A test like
that passes on the buggy code and proves nothing. The tests below reproduce
the actual interleaving:

- test_stale_pending_read_does_not_execute_twice makes the interleaving
  deterministic: worker B reads the row while it is still PENDING, worker A
  then claims and finishes it, and only then does B proceed. On the old code
  B acts on its stale PENDING read and the command runs twice.
- test_concurrent_duplicate_dequeues_execute_once is the same thing with two
  real threads and two real sessions racing through process_task_run.

Real Postgres and Redis, no mocking of the DB layer. The only thing patched
is worker_pool._execute_command, so the test can count executions without
spawning subprocesses.

These tests use committed rows via SessionLocal (not the rolled-back
db_session fixture): two independent connections cannot see each other's
uncommitted rows, and two connections are the whole point here. The autouse
_clean_db fixture in conftest.py wipes workflows before each test.
"""
from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.database import SessionLocal
from app.engine import worker_pool
from app.engine.worker_pool import process_task_run
from app.models.entities import Task, TaskRun, Workflow, WorkflowRun
from app.models.enums import TaskStatus, TriggerType, WorkflowRunStatus


def _ok_command() -> str:
    return f'"{sys.executable}" -c "exit(0)"'


@pytest.fixture
def pending_task_run_id() -> int:
    """One committed workflow -> one task -> one RUNNING WorkflowRun -> one
    PENDING TaskRun (attempt 1). Returns the TaskRun id."""
    db = SessionLocal()
    try:
        workflow = Workflow(name="dup-dequeue")
        db.add(workflow)
        db.flush()

        task = Task(workflow_id=workflow.id, name="only", command=_ok_command())
        run = WorkflowRun(
            workflow_id=workflow.id,
            status=WorkflowRunStatus.RUNNING,
            trigger_type=TriggerType.MANUAL,
        )
        db.add_all([task, run])
        db.flush()

        task_run = TaskRun(
            workflow_run_id=run.id,
            task_id=task.id,
            status=TaskStatus.PENDING,
            attempt_number=1,
        )
        db.add(task_run)
        db.commit()
        return task_run.id
    finally:
        db.close()


@pytest.fixture
def execute_calls(monkeypatch) -> list[str]:
    """Replace the real subprocess call with a counter. The short sleep keeps
    the 'command' running long enough that a racing duplicate would overlap
    with it."""
    calls: list[str] = []
    lock = threading.Lock()

    def fake_execute(command: str):
        with lock:
            calls.append(command)
        time.sleep(0.2)
        return True, None

    monkeypatch.setattr(worker_pool, "_execute_command", fake_execute)
    return calls


def _reload(task_run_id: int) -> TaskRun:
    db = SessionLocal()
    try:
        row = db.get(TaskRun, task_run_id)
        db.expunge(row)
        return row
    finally:
        db.close()


def test_stale_pending_read_does_not_execute_twice(pending_task_run_id, execute_calls):
    db_a = SessionLocal()
    db_b = SessionLocal()
    try:
        # Worker B's "read": the row is PENDING at this moment. In the old
        # code this was process_task_run's own db.get(), and B kept acting
        # on this value even after A changed the row.
        stale = db_b.get(TaskRun, pending_task_run_id)
        assert stale.status == TaskStatus.PENDING

        process_task_run(db_a, pending_task_run_id)   # worker A claims and finishes
        process_task_run(db_b, pending_task_run_id)   # worker B arrives with a stale PENDING read
    finally:
        db_a.close()
        db_b.close()

    assert len(execute_calls) == 1, "the command ran more than once for one TaskRun"

    row = _reload(pending_task_run_id)
    assert row.status == TaskStatus.SUCCESS
    assert row.attempt_number == 1


def test_concurrent_duplicate_dequeues_execute_once(pending_task_run_id, execute_calls):
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker() -> None:
        db = SessionLocal()
        try:
            barrier.wait(timeout=5)
            process_task_run(db, pending_task_run_id)
        except BaseException as exc:  # noqa: BLE001 -- surface anything, including ValueError
            errors.append(exc)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker), pool.submit(worker)]
        for f in futures:
            f.result(timeout=15)

    assert errors == [], f"a duplicate dequeue should be skipped quietly, got: {errors!r}"
    assert len(execute_calls) == 1, "the command ran more than once for one TaskRun"
    assert _reload(pending_task_run_id).status == TaskStatus.SUCCESS


def test_duplicate_of_finished_task_run_is_a_quiet_noop(pending_task_run_id, execute_calls):
    db = SessionLocal()
    try:
        process_task_run(db, pending_task_run_id)
    finally:
        db.close()

    finished = _reload(pending_task_run_id)
    assert finished.status == TaskStatus.SUCCESS

    # A late duplicate id for a row that already finished: must not raise
    # (it used to raise ValueError from validate_transition and rely on the
    # worker's catch-all), must not execute, must not touch the row.
    db = SessionLocal()
    try:
        process_task_run(db, pending_task_run_id)
    finally:
        db.close()

    assert len(execute_calls) == 1
    after = _reload(pending_task_run_id)
    assert after.status == TaskStatus.SUCCESS
    assert after.started_at == finished.started_at
    assert after.completed_at == finished.completed_at


def test_unknown_task_run_id_is_dropped_without_raising(execute_calls):
    db = SessionLocal()
    try:
        process_task_run(db, 987654321)
    finally:
        db.close()
    assert execute_calls == []
