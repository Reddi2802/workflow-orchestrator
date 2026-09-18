import { useEffect, useState } from "react";
import { listWorkflows, deleteWorkflow } from "../api.js";

function statusClass(status) { const key = (status || "pending").toLowerCase(); if (key.includes("success")) return "status-success"; if (key.includes("fail")) return "status-failed"; if (key.includes("run")) return "status-running"; return "status-pending"; }

export default function WorkflowList({ onSelect, onCreate }) {
  const [workflows, setWorkflows] = useState([]); const [loading, setLoading] = useState(true); const [error, setError] = useState(null);
  async function load() { setLoading(true); setError(null); try { setWorkflows(await listWorkflows()); } catch (err) { setError(err.message); } finally { setLoading(false); } }
  useEffect(() => { load(); }, []);
  async function handleDelete(e, id) { e.stopPropagation(); if (!confirm("Delete this workflow?")) return; try { await deleteWorkflow(id); load(); } catch (err) { setError(err.message); } }
  return <div>
    <div className="app-header"><div><p className="eyebrow">Orchestration workspace</p><h1>Workflows</h1><p className="page-description">Build, monitor, and manage your automated processes.</p></div><button className="primary" onClick={onCreate}><span>+</span> New workflow</button></div>
    {loading && <div className="loading-state"><span className="spinner" />Loading workflows</div>}
    {error && <p className="error-text">{error}</p>}
    {!loading && !error && workflows.length === 0 && <div className="empty-state"><div className="empty-icon">⌘</div><h2>Your workspace is ready</h2><p>Create a workflow to start orchestrating your tasks.</p><button className="primary" onClick={onCreate}>Create your first workflow</button></div>}
    {!loading && !error && workflows.length > 0 && <><div className="section-label">All workflows <span>{workflows.length}</span></div><div className="workflow-grid">{workflows.map((wf) => <div key={wf.id} className="card workflow-row" onClick={() => onSelect(wf.id)}><div><div className="workflow-icon">↗</div><div className="workflow-name">{wf.name}</div><div className="muted">{wf.task_count} task{wf.task_count === 1 ? "" : "s"}</div></div><div className="workflow-actions"><span className={`status-pill ${statusClass(wf.status)}`}>{wf.status || "pending"}</span><button className="icon-button danger-button" title="Delete workflow" onClick={(e) => handleDelete(e, wf.id)}>×</button></div></div>)}</div></>}
  </div>;
}
