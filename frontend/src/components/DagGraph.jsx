import { colorsFor } from "../status.js";

const W = 150, H = 46, GAP_X = 80, GAP_Y = 26, PAD = 12;

// Left-to-right layout: a task's column = longest path from any root to it.
// (Longest path, not shortest, so a task always sits right of ALL its upstreams.)
function layout(tasks, dependencies) {
  const upstream = Object.fromEntries(tasks.map((t) => [t.id, []]));
  for (const d of dependencies) upstream[d.downstream_task_id]?.push(d.upstream_task_id);

  const depth = {};
  const visiting = new Set();
  const depthOf = (id) => {
    if (depth[id] !== undefined) return depth[id];
    if (visiting.has(id)) return 0; // cycle guard; the API rejects cycles, this is belt-and-braces
    visiting.add(id);
    const ups = upstream[id] ?? [];
    depth[id] = ups.length ? 1 + Math.max(...ups.map(depthOf)) : 0;
    visiting.delete(id);
    return depth[id];
  };
  tasks.forEach((t) => depthOf(t.id));

  const columns = [];
  tasks.forEach((t) => (columns[depth[t.id]] ??= []).push(t));
  const rows = Math.max(...columns.filter(Boolean).map((c) => c.length));
  const height = rows * H + (rows - 1) * GAP_Y;

  const pos = {};
  columns.forEach((col, c) => {
    if (!col) return;
    const colHeight = col.length * H + (col.length - 1) * GAP_Y;
    const offset = (height - colHeight) / 2; // centre each column vertically
    col.forEach((t, r) => (pos[t.id] = { x: c * (W + GAP_X), y: offset + r * (H + GAP_Y) }));
  });
  return { pos, width: columns.length * W + (columns.length - 1) * GAP_X, height };
}

// taskStatuses: optional { [task_id]: status }. Without it every node shows as "pending".
export default function DagGraph({ tasks = [], dependencies = [], taskStatuses = {} }) {
  if (tasks.length === 0) return <div className="subtle-empty">This workflow has no tasks.</div>;

  const { pos, width, height } = layout(tasks, dependencies);
  const total = (n) => n + PAD * 2;

  return (
    <div className="dag-scroll">
      <svg
        role="img"
        aria-label="Task dependency graph"
        viewBox={`${-PAD} ${-PAD} ${total(width)} ${total(height)}`}
        width={total(width)}
        height={total(height)}
      >
        <defs>
          <marker id="dag-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0 0L10 5L0 10z" fill="#8b97b3" />
          </marker>
        </defs>

        {dependencies.map((d) => {
          const a = pos[d.upstream_task_id];
          const b = pos[d.downstream_task_id];
          if (!a || !b) return null;
          const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2;
          const mid = (x1 + x2) / 2;
          return (
            <path
              key={d.id ?? `${d.upstream_task_id}-${d.downstream_task_id}`}
              d={`M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}`}
              fill="none" stroke="#8b97b3" strokeWidth="1.5" markerEnd="url(#dag-arrow)"
            />
          );
        })}

        {tasks.map((t) => {
          const status = taskStatuses[t.id] ?? "pending";
          const { bg, fg } = colorsFor(status);
          return (
            <g key={t.id} transform={`translate(${pos[t.id].x} ${pos[t.id].y})`}>
              <title>{`${t.name} (${status})`}</title>
              <rect width={W} height={H} rx="10" fill={bg} stroke={fg} strokeWidth="1.5"
                    strokeDasharray={status === "skipped" ? "5 4" : undefined} />
              <text x={W / 2} y={H / 2 - 3} textAnchor="middle" fontSize="13" fontWeight="700" fill={fg}>
                {t.name.length > 18 ? `${t.name.slice(0, 17)}…` : t.name}
              </text>
              <text x={W / 2} y={H / 2 + 13} textAnchor="middle" fontSize="10" fill={fg}>{status}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}