// Points at your local FastAPI server (uv run uvicorn app.main:app --reload).
// Override with VITE_API_URL in frontend/.env.local (e.g. to use another port);
// .env.local is git-ignored by Vite's default .gitignore, so it won't be committed.
const API_BASE = `${import.meta.env.VITE_API_URL ?? "http://localhost:8000"}/api/v1`;

async function request(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const message = body.detail
      ? typeof body.detail === "string"
        ? body.detail
        : JSON.stringify(body.detail)
      : `Request failed with status ${res.status}`;
    throw new Error(message);
  }

  // 204 No Content (e.g. DELETE) has no body to parse
  if (res.status === 204) return null;
  return res.json();
}

export function listWorkflows() {
  return request("/workflows");
}

export function getWorkflow(id) {
  return request(`/workflows/${id}`);
}

export function createWorkflow(payload) {
  return request("/workflows", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function deleteWorkflow(id) {
  return request(`/workflows/${id}`, { method: "DELETE" });
}

export function getWorkflowRuns(id) {
  return request(`/workflows/${id}/runs`);
}

// Week 5: one run with its task_runs nested (GET /runs/{id}).
export function getRun(id) {
  return request(`/runs/${id}`);
}

// Week 5: manual trigger (POST /workflows/{id}/runs). Returns the new PENDING run.
export function triggerRun(workflowId) {
  return request(`/workflows/${workflowId}/runs`, { method: "POST" });
}
