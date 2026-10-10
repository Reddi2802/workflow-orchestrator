"""POST /api/v1/workflows/{id}/runs -- manual trigger.

Needs the real Postgres (docker compose up -d), like the other API tests.
`client` is a plain TestClient(app) without a context manager, so the lifespan
hook (scheduler / dispatch loop / workers) does NOT run here: a triggered run
stays PENDING instead of being picked up mid-test.
"""
from app.core.database import SessionLocal
from app.models.entities import WorkflowRun
from app.models.enums import TriggerType, WorkflowRunStatus


def _create_workflow(client, **overrides):
    payload = {"name": "trigger-wf", "tasks": [{"name": "a", "command": "echo a"}], **overrides}
    resp = client.post("/api/v1/workflows", json=payload)
    assert resp.status_code == 201
    return resp.json()["id"]


def test_trigger_creates_pending_manual_run(client):
    wf_id = _create_workflow(client)

    resp = client.post(f"/api/v1/workflows/{wf_id}/runs")

    assert resp.status_code == 201
    body = resp.json()
    assert body["workflow_id"] == wf_id
    assert body["status"] == "pending"
    assert body["trigger_type"] == "manual"

    # Verify at the DB level, not just the HTTP response.
    db = SessionLocal()
    try:
        row = db.get(WorkflowRun, body["id"])
        assert row is not None
        assert row.status == WorkflowRunStatus.PENDING
        assert row.trigger_type == TriggerType.MANUAL
    finally:
        db.close()


def test_triggered_run_appears_in_history(client):
    wf_id = _create_workflow(client)
    run_id = client.post(f"/api/v1/workflows/{wf_id}/runs").json()["id"]

    history = client.get(f"/api/v1/workflows/{wf_id}/runs").json()

    assert [r["id"] for r in history] == [run_id]


def test_trigger_unknown_workflow_returns_404(client):
    resp = client.post("/api/v1/workflows/999999/runs")
    assert resp.status_code == 404


def test_trigger_blocked_at_max_active_runs(client):
    wf_id = _create_workflow(client)  # max_active_runs defaults to 1

    assert client.post(f"/api/v1/workflows/{wf_id}/runs").status_code == 201
    second = client.post(f"/api/v1/workflows/{wf_id}/runs")

    assert second.status_code == 409
    assert len(client.get(f"/api/v1/workflows/{wf_id}/runs").json()) == 1


def test_trigger_allowed_up_to_max_active_runs(client):
    wf_id = _create_workflow(client, max_active_runs=2)

    assert client.post(f"/api/v1/workflows/{wf_id}/runs").status_code == 201
    assert client.post(f"/api/v1/workflows/{wf_id}/runs").status_code == 201
    assert client.post(f"/api/v1/workflows/{wf_id}/runs").status_code == 409