// Real enum values from backend/app/models/enums.py.
//   TaskStatus:        pending | running | success | failed | retrying | skipped
//   WorkflowRunStatus: pending | running | success | failed | partial
// Exact matching on purpose: the older substring matching sent "retrying" and
// "skipped" to the "pending" style, which made them indistinguishable.
const KNOWN = new Set([
  "pending", "running", "success", "failed", "retrying", "skipped", "partial",
]);

export function statusClass(status) {
  if (!status) return "status-none";
  const key = String(status).toLowerCase();
  return KNOWN.has(key) ? `status-${key}` : "status-pending"; // unknown value: never crash
}

// Same palette as the .status-* pill classes, for SVG fills (CSS classes can't set SVG attrs).
const COLORS = {
  pending:  { bg: "#f0f2f6", fg: "#667085" },
  running:  { bg: "#eaf3ff", fg: "#3177cf" },
  success:  { bg: "#e4f8ef", fg: "#168759" },
  failed:   { bg: "#fff0f0", fg: "#d64e54" },
  retrying: { bg: "#fff8db", fg: "#9a6700" },
  skipped:  { bg: "#f7f8fa", fg: "#98a2b3" },
  partial:  { bg: "#fff6e5", fg: "#b9780a" },
};
export const colorsFor = (status) => COLORS[String(status).toLowerCase()] ?? COLORS.pending;

// Statuses that mean "something is still going to change" -> keep polling.
const ACTIVE = new Set(["pending", "running", "retrying"]);
export const isActive = (status) => ACTIVE.has(String(status).toLowerCase());

// A retry inserts a NEW TaskRun row (unique on run + task + attempt_number), so one
// task can have several rows. The task's real state is its highest attempt_number.
export function latestStatusByTask(taskRuns = []) {
  const best = {};
  for (const tr of taskRuns) {
    if (!best[tr.task_id] || tr.attempt_number > best[tr.task_id].attempt_number) {
      best[tr.task_id] = tr;
    }
  }
  return Object.fromEntries(Object.entries(best).map(([id, tr]) => [id, tr.status]));
}