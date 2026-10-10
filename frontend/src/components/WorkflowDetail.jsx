import { useCallback, useEffect, useRef, useState } from "react";
import { getWorkflow, getWorkflowRuns, getRun, triggerRun } from "../api.js";
import { statusClass, isActive, latestStatusByTask } from "../status.js";
import DagGraph from "./DagGraph.jsx";
import "./WorkflowDetail.css";

// Poll every 2s while anything is still moving. The dispatch loop on the backend
// ticks every 2s, so polling faster than that can't show anything new. There is
// no push channel in this architecture (in-process engine, Section 2), so polling
// is the intended approach. It stops as soon as everything is terminal.
const POLL_MS = 2000;

const fmt = (ts) =>
  ts ? new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(ts) ? ts : `${ts}Z`).toLocaleString() : "—";

export default function WorkflowDetail({ workflowId, onBack }) {
  const [workflow, setWorkflow] = useState(null);
  const [runs, setRuns] = useState([]);
  const [selectedRunId, setSelectedRunId] = useState(null);
  const [runDetail, setRunDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [triggering, setTriggering] = useState(false);

  // Lets an in-flight response for run #3 be discarded if the user already clicked run #4.
  const selectedRef = useRef(null);
  selectedRef.current = selectedRunId;

  // Initial load.
  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      setSelectedRunId(null);
      setRunDetail(null);
      try {
        const [wf, runData] = await Promise.all([getWorkflow(workflowId), getWorkflowRuns(workflowId)]);
        if (cancelled) return;
        setWorkflow(wf);
        setRuns(runData);
        if (runData.length > 0) setSelectedRunId(runData[0].id); // newest first
      } catch (err) {
        if (!cancelled) setError(err.message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, [workflowId]);

  const refreshRuns = useCallback(async () => {
    try {
      setRuns(await getWorkflowRuns(workflowId));
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }, [workflowId]);

  const refreshDetail = useCallback(async (id) => {
    if (id == null) return;
    try {
      const data = await getRun(id);
      if (selectedRef.current === id) setRunDetail(data);
    } catch (err) {
      if (selectedRef.current === id) setError(err.message);
    }
  }, []);

  // Fetch the selected run's task-level detail whenever the selection changes.
  useEffect(() => {
    setRunDetail(null);
    refreshDetail(selectedRunId);
  }, [selectedRunId, refreshDetail]);

  const needsPolling =
    runs.some((r) => isActive(r.status)) ||
    (runDetail != null && (isActive(runDetail.status) || runDetail.task_runs.some((t) => isActive(t.status))));

  useEffect(() => {
    if (!needsPolling) return undefined;
    const timer = setInterval(() => {
      refreshRuns();
      refreshDetail(selectedRef.current);
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [needsPolling, refreshRuns, refreshDetail]);

  async function handleTrigger() {
    setTriggering(true);
    setError(null);
    try {
      const run = await triggerRun(workflowId); // 409 if max_active_runs is already reached
      await refreshRuns();
      setSelectedRunId(run.id);
    } catch (err) {
      setError(err.message);
    } finally {
      setTriggering(false);
    }
  }

  const tasks = workflow?.tasks ?? [];
  const taskName = Object.fromEntries(tasks.map((t) => [t.id, t.name]));
  const taskStatuses = runDetail ? latestStatusByTask(runDetail.task_runs) : {};
  const attempts = runDetail
    ? [...runDetail.task_runs].sort((a, b) => a.task_id - b.task_id || a.attempt_number - b.attempt_number)
    : [];

  return (
    <div>
      <button className="back-link" onClick={onBack}>← All workflows</button>
      {loading && <div className="loading-state"><span className="spinner" />Loading workflow</div>}
      {error && <p className="error-text" role="alert">{error}</p>}

      {workflow && (
        <>
          <div className="detail-hero">
            <div>
              <p className="eyebrow">Workflow overview</p>
              <h1>{workflow.name}</h1>
              <p className="page-description">A coordinated sequence of tasks and dependencies.</p>
            </div>
            <div className="run-actions">
              <span className={`status-pill ${statusClass(runs[0]?.status ?? workflow.latest_run_status)}`}>
                {runs[0]?.status ?? workflow.latest_run_status ?? "No runs yet"}
              </span>
              <button className="primary" onClick={handleTrigger} disabled={triggering}>
                {triggering ? "Triggering…" : "Trigger run"}
              </button>
            </div>
          </div>

          <section className="content-section">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Execution plan</p>
                <h2>Task graph</h2>
              </div>
              <span className="count-badge">{tasks.length}</span>
            </div>
            <DagGraph tasks={tasks} dependencies={workflow.dependencies ?? []} taskStatuses={taskStatuses} />
            <p className="legend-note">
              {runDetail
                ? `Node colours show the status of run #${runDetail.id}.`
                : "Select a run below to colour the graph with its task statuses."}
            </p>
          </section>

          <section className="content-section">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Activity</p>
                <h2>Run history</h2>
              </div>
            </div>
            {runs.length === 0 ? (
              <div className="subtle-empty">No runs yet. Use “Trigger run” to start one.</div>
            ) : (
              <div className="run-list">
                {runs.map((run) => (
                  <div
                    key={run.id}
                    className={`card workflow-row run-row${run.id === selectedRunId ? " selected" : ""}`}
                    onClick={() => setSelectedRunId(run.id)}
                  >
                    <div>
                      <div className="workflow-name">Run #{run.id}</div>
                      <div className="muted">
                        {run.trigger_type} · triggered {fmt(run.triggered_at)}
                        {run.completed_at ? ` · finished ${fmt(run.completed_at)}` : ""}
                      </div>
                    </div>
                    <span className={`status-pill ${statusClass(run.status)}`}>{run.status}</span>
                  </div>
                ))}
              </div>
            )}

            {runDetail && (
              <div className="card run-detail">
                <h3>Run #{runDetail.id} — task attempts</h3>
                {attempts.length === 0 ? (
                  <div className="subtle-empty">No tasks have started yet.</div>
                ) : (
                  attempts.map((tr) => (
                    <div key={tr.id} className="attempt-row">
                      <div>
                        <strong>{taskName[tr.task_id] ?? `task ${tr.task_id}`}</strong>
                        <span className="muted"> · attempt {tr.attempt_number}</span>
                        {tr.error_message && <div className="attempt-error">{tr.error_message}</div>}
                      </div>
                      <span className={`status-pill ${statusClass(tr.status)}`}>{tr.status}</span>
                    </div>
                  ))
                )}
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
