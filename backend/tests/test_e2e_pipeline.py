# tests/test_e2e_pipeline.py
"""
Week 7: the whole pipeline through the public HTTP API, with the REAL engine
running (recovery at startup, dispatch loop, 2 workers) against real Postgres,
real Redis and real subprocesses. No mocking.

Every other test file exercises one layer (API, scheduler, worker pool,
recovery). This file is the one that would catch the layers disagreeing with
each other, which is what the joint integration week is for. It is the
automated, repeatable version of the core scenarios in scripts/e2e_smoke.py
(that script additionally covers cron and a real crash, which need a live
server and, for the crash, a human to kill it).

How it works: `with TestClient(app)` runs the app's lifespan, so the engine
loops really start. The module-scoped fixture starts them once for the whole
file; stopping them waits for the workers' 5 second queue timeout, so it costs
about 5 seconds once, not per test.

Do not run this while a live uvicorn is using the same database and Redis:
conftest.py wipes both between tests.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app

TERMINAL = {"success", "failed", "partial"}


def _cmd(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def _sleep(seconds: float) -> str:
    return _cmd(f"import time; time.sleep({seconds})")


FAIL = _cmd("import sys; sys.exit(1)")


@pytest.fixture(scope="module")
def live():
    """A client whose app has its lifespan running, i.e. a working engine."""
    with TestClient(app) as client:
        yield client


def _task(name, command, max_retries=0, backoff=1):
    return {"name": name, "command": command, "max_retries": max_retries,
            "retry_backoff_seconds": backoff}


def _create(client, name, tasks, deps=(), max_active_runs=1):
    resp = client.post("/api/v1/workflows", json={
        "name": f"e2e-{name}",
        "max_active_runs": max_active_runs,
        "tasks": tasks,
        "dependencies": [{"upstream_task_name": u, "downstream_task_name": d} for u, d in deps],
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def _wait(client, run_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = client.get(f"/api/v1/runs/{run_id}").json()
        if detail["status"] in TERMINAL:
            return detail
        time.sleep(0.3)
    pytest.fail(f"run {run_id} did not finish within {timeout}s")


def _run(client, name, tasks, deps=(), **kw):
    wf = _create(client, name, tasks, deps, **kw)
    trig = client.post(f"/api/v1/workflows/{wf['id']}/runs")
    assert trig.status_code == 201, trig.text
    detail = _wait(client, trig.json()["id"])
    names = {t["id"]: t["name"] for t in wf["tasks"]}
    rows: dict[str, list] = {}
    for tr in detail["task_runs"]:
        rows.setdefault(names[tr["task_id"]], []).append(tr)
    for r in rows.values():
        r.sort(key=lambda x: x["attempt_number"])
    return wf, detail, rows


def _t(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_diamond_runs_in_dependency_order_and_succeeds(live):
    tasks = [_task(n, _sleep(0.4)) for n in "abcd"]
    _, detail, rows = _run(live, "diamond", tasks, [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")])

    assert detail["status"] == "success"
    assert detail["completed_at"] is not None
    for n in "abcd":
        assert [(r["attempt_number"], r["status"]) for r in rows[n]] == [(1, "success")]

    a, b, c, d = (rows[n][0] for n in "abcd")
    assert _t(b["started_at"]) >= _t(a["completed_at"])
    assert _t(c["started_at"]) >= _t(a["completed_at"])
    assert _t(d["started_at"]) >= max(_t(b["completed_at"]), _t(c["completed_at"]))


def test_failed_task_skips_its_downstream_and_run_is_partial(live):
    tasks = [_task("a", _sleep(0.2)), _task("b", FAIL), _task("c", _sleep(0.2))]
    _, detail, rows = _run(live, "failure-skip", tasks, [("a", "b"), ("b", "c")])

    assert rows["a"][0]["status"] == "success"
    assert rows["b"][0]["status"] == "failed"
    assert rows["b"][0]["error_message"] is not None
    assert rows["c"][0]["status"] == "skipped"
    assert detail["status"] == "partial"


def test_retry_creates_a_second_attempt_after_the_backoff(live, tmp_path):
    marker = tmp_path / "ran-once"
    # fails the first time (no marker yet), succeeds the second time
    flaky = _cmd(
        f"import os,sys; f=r'{marker}'; ok=os.path.exists(f); open(f,'w').close(); sys.exit(0 if ok else 1)"
    )
    _, detail, rows = _run(live, "retry", [_task("flaky", flaky, max_retries=1, backoff=1)])

    assert [(r["attempt_number"], r["status"]) for r in rows["flaky"]] == [(1, "retrying"), (2, "success")]
    gap = (_t(rows["flaky"][1]["started_at"]) - _t(rows["flaky"][0]["completed_at"])).total_seconds()
    assert gap >= 0.9
    assert detail["status"] == "success"


def test_second_trigger_while_a_run_is_active_gets_409(live):
    wf = _create(live, "max-active", [_task("slow", _sleep(2))], max_active_runs=1)
    first = live.post(f"/api/v1/workflows/{wf['id']}/runs")
    assert first.status_code == 201

    second = live.post(f"/api/v1/workflows/{wf['id']}/runs")
    assert second.status_code == 409

    _wait(live, first.json()["id"])
    third = live.post(f"/api/v1/workflows/{wf['id']}/runs")
    assert third.status_code == 201
    _wait(live, third.json()["id"])


def test_empty_workflow_cannot_be_triggered(live):
    wf = _create(live, "empty", [])

    resp = live.post(f"/api/v1/workflows/{wf['id']}/runs")

    assert resp.status_code == 400
