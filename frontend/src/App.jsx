import { useState } from "react";
import WorkflowList from "./components/WorkflowList.jsx";
import WorkflowDetail from "./components/WorkflowDetail.jsx";
import CreateWorkflowForm from "./components/CreateWorkflowForm.jsx";

// Basic view state: "list" | "detail" | "create"
// No router library — swap for react-router later if the app grows.
export default function App() {
  const [view, setView] = useState("list");
  const [selectedId, setSelectedId] = useState(null);

  return (
    <div className="app">
      <header className="topbar">
        <button className="brand" type="button" onClick={() => setView("list")} aria-label="Go to workflows"><span className="brand-mark">W</span><span>Workflow<span className="brand-accent">OS</span></span></button>
        <div className="topbar-status"><span className="status-dot" />System operational</div>
      </header>
      {view === "list" && (
        <WorkflowList
          onSelect={(id) => {
            setSelectedId(id);
            setView("detail");
          }}
          onCreate={() => setView("create")}
        />
      )}

      {view === "detail" && (
        <WorkflowDetail workflowId={selectedId} onBack={() => setView("list")} />
      )}

      {view === "create" && (
        <CreateWorkflowForm
          onCreated={(id) => {
            setSelectedId(id);
            setView("detail");
          }}
          onCancel={() => setView("list")}
        />
      )}
    </div>
  );
}
