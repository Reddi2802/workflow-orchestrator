"""
Week 9 coverage push: the engine/ and api/ branches that no earlier test
reached. Each test asserts behaviour (rows, status codes, log-free state), not
just that a line ran.
"""
from __future__ import annotations

import asyncio
import sys

from app.core.database import SessionLocal
from app.engine import recovery
from app.engine.recovery import recover_incomplete_runs
from app.engine.scheduler import run_polling_loop
from app.engine.worker_pool import dispatch_loop, worker_loop
from app.models.entities import Task, TaskRun, Workflow, WorkflowRun
from app.models.enums import TaskStatus, TriggerType, WorkflowRunStatus
from app.queue.redis_queue import dequeue_task


def _payload(name="gap-wf"):
    return {
        "name": name,
        "tasks": [
            {"name": "a", "command": f'"{sys.executable}" -c "exit(0)"'},
            {"name": "b", "command": f'"{sys.executable}" -c "exit(0)"'},
        ],
        "dependencies": [{"upstream_task_name": "a", "downstream_task_name": "b"}],
    }


# --- API ---------------------------------------------------------------------

def test_get_run_returns_run_with_task_runs(client):
    wf = client.post("/api/v1/workflows", json=_payload()).json()
    run = client.post(f"/api/v1/workflows/{wf['id']}/runs").json()
    resp = client.get(f"/api/v1/runs/{run['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == run["id"]
    assert body["workflow_id"] == wf["id"]
    assert isinstance(body["task_runs"], list)


def test_get_run_unknown_is_404(client):
    assert client.get("/api/v1/runs/99999999").status_code == 404


def test_update_workflow_changes_only_sent_fields(client):
    wf = client.post("/api/v1/workflows", json=_payload()).json()
    resp = client.put(f"/api/v1/workflows/{wf['id']}", json={"description": "new text"})
    assert resp.status_code == 200
    assert resp.json()["description"] == "new text"
    assert resp.json()["name"] == "gap-wf"


def test_update_workflow_unknown_is_404(client):
    assert client.put("/api/v1/workflows/99999999", json={"description": "x"}).status_code == 404


def test_delete_workflow_removes_rows_and_second_delete_is_404(client):
    wf = client.post("/api/v1/workflows", json=_payload()).json()
    assert client.delete(f"/api/v1/workflows/{wf['id']}").status_code == 204
    assert client.get(f"/api/v1/workflows/{wf['id']}").status_code == 404
    db = SessionLocal()
    try:
        assert db.query(Task).filter(Task.workflow_id == wf["id"]).count() == 0
    finally:
        db.close()
    assert client.delete(f"/api/v1/workflows/{wf['id']}").status_code == 404


def test_list_runs_unknown_workflow_is_404(client):
    assert client.get("/api/v1/workflows/99999999/runs").status_code == 404


# --- recovery defensive paths -------------------------------------------------

def _running_run_with_one_task(db):
    wf = Workflow(name="gap-recovery")
    db.add(wf)
    db.commit()
    task = Task(workflow_id=wf.id, name="only", command=f'"{sys.executable}" -c "exit(0)"')
    db.add(task)
    run = WorkflowRun(workflow_id=wf.id, status=WorkflowRunStatus.RUNNING,
                      trigger_type=TriggerType.MANUAL)
    db.add(run)
    db.commit()
    return wf, task, run


def test_recovery_creates_missing_root_task_run(db_session):
    """RUNNING run whose root task has no TaskRun at all (should be impossible,
    but recovery repairs it): attempt 1 is created and enqueued."""
    _, task, run = _running_run_with_one_task(db_session)
    recover_incomplete_runs(db_session)
    rows = db_session.query(TaskRun).filter(TaskRun.workflow_run_id == run.id).all()
    assert [(r.task_id, r.attempt_number, r.status) for r in rows] == [
        (task.id, 1, TaskStatus.PENDING)
    ]
    assert dequeue_task(1) == rows[0].id


def test_recovery_skips_run_when_topological_sort_fails(db_session, monkeypatch):
    _, _, run = _running_run_with_one_task(db_session)

    def boom(*a, **k):
        raise RuntimeError("corrupted graph")

    monkeypatch.setattr(recovery, "topological_sort", boom)
    assert recover_incomplete_runs(db_session) == [run.id]  # examined...
    assert db_session.query(TaskRun).filter(TaskRun.workflow_run_id == run.id).count() == 0  # ...not touched
    assert dequeue_task(1) is None


# --- background loops stop cleanly --------------------------------------------

def _run_loop_briefly(loop_fn, **kw):
    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(loop_fn(SessionLocal, stop_event=stop, **kw))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, timeout=10)

    asyncio.run(main())


def test_polling_loop_stops_on_event():
    _run_loop_briefly(run_polling_loop, interval_seconds=1)


def test_dispatch_loop_stops_on_event():
    _run_loop_briefly(dispatch_loop, interval_seconds=1)


def test_worker_loop_stops_on_event():
    _run_loop_briefly(worker_loop, dequeue_timeout_seconds=1)


def test_loops_survive_a_failing_cycle(monkeypatch):
    """A cycle that raises is logged and the loop keeps going until stopped."""
    from app.engine import scheduler

    calls = {"n": 0}

    def flaky(db):
        calls["n"] += 1
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(scheduler, "poll_and_trigger_sync", flaky)
    _run_loop_briefly(run_polling_loop, interval_seconds=1)
    assert calls["n"] >= 1
