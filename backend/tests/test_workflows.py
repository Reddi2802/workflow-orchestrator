"""
Integration tests — these hit a REAL Postgres (docker compose must be
running: `docker compose up -d` from the repo root first).

Per the Week 1 self-check checklist: these confirm rows actually land in
the DB, not just that the HTTP response looks right.
"""


def _sample_payload():
    return {
        "name": "example-workflow",
        "cron_expression": "0 * * * *",
        "tasks": [
            {"name": "task_a", "command": "echo a"},
            {"name": "task_b", "command": "echo b"},
            {"name": "task_c", "command": "echo c"},
        ],
        "dependencies": [
            {"upstream_task_name": "task_a", "downstream_task_name": "task_b"},
            {"upstream_task_name": "task_a", "downstream_task_name": "task_c"},
        ],
    }


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200


def test_create_workflow(client):
    resp = client.post("/api/v1/workflows", json=_sample_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "example-workflow"
    assert len(body["tasks"]) == 3
    assert len(body["dependencies"]) == 2


def test_create_workflow_rejects_self_dependency(client):
    payload = {
        "name": "bad-workflow",
        "tasks": [{"name": "task_a", "command": "echo a"}],
        "dependencies": [
            {"upstream_task_name": "task_a", "downstream_task_name": "task_a"}
        ],
    }
    resp = client.post("/api/v1/workflows", json=payload)
    assert resp.status_code == 422


def test_list_workflows(client):
    client.post("/api/v1/workflows", json=_sample_payload())
    resp = client.get("/api/v1/workflows")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_get_workflow(client):
    created = client.post("/api/v1/workflows", json=_sample_payload()).json()
    resp = client.get(f"/api/v1/workflows/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["tasks"]) == 3
    assert len(body["dependencies"]) == 2


def test_get_workflow_404(client):
    resp = client.get("/api/v1/workflows/999999")
    assert resp.status_code == 404


def test_update_workflow(client):
    created = client.post("/api/v1/workflows", json=_sample_payload()).json()
    resp = client.put(f"/api/v1/workflows/{created['id']}", json={"name": "renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed"


def test_delete_workflow(client):
    created = client.post("/api/v1/workflows", json=_sample_payload()).json()
    resp = client.delete(f"/api/v1/workflows/{created['id']}")
    assert resp.status_code == 204
    assert client.get(f"/api/v1/workflows/{created['id']}").status_code == 404


def test_list_workflow_runs_empty(client):
    created = client.post("/api/v1/workflows", json=_sample_payload()).json()
    resp = client.get(f"/api/v1/workflows/{created['id']}/runs")
    assert resp.status_code == 200
    assert resp.json() == []
