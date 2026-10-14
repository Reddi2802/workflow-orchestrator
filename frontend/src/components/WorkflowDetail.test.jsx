import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import WorkflowDetail from "./WorkflowDetail.jsx";
import { getWorkflow, getWorkflowRuns, getRun, triggerRun } from "../api.js";

vi.mock("../api.js", () => ({
  getWorkflow: vi.fn(),
  getWorkflowRuns: vi.fn(),
  getRun: vi.fn(),
  triggerRun: vi.fn(),
}));

const workflow = {
  id: 1,
  name: "pipeline",
  latest_run_status: null,
  tasks: [
    { id: 1, name: "step_one" },
    { id: 2, name: "boom" },
    { id: 3, name: "never_runs" },
  ],
  dependencies: [
    { id: 1, upstream_task_id: 1, downstream_task_id: 2 },
    { id: 2, upstream_task_id: 2, downstream_task_id: 3 },
  ],
};
const run = (over = {}) => ({
  id: 10, workflow_id: 1, status: "success", trigger_type: "manual",
  triggered_at: "2026-10-10T07:16:42", started_at: null, completed_at: null, ...over,
});
const taskRun = (over = {}) => ({
  id: 1, workflow_run_id: 10, task_id: 1, status: "success", attempt_number: 1,
  error_message: null, ...over,
});

beforeEach(() => {
  vi.resetAllMocks();
  getWorkflow.mockResolvedValue(workflow);
  getWorkflowRuns.mockResolvedValue([]);
  getRun.mockResolvedValue({ ...run(), task_runs: [] });
});
afterEach(() => vi.useRealTimers());

const renderDetail = () => render(<WorkflowDetail workflowId={1} onBack={() => {}} />);

describe("WorkflowDetail", () => {
  it("handles a workflow with no runs yet", async () => {
    renderDetail();
    expect(await screen.findByText(/No runs yet\. Use/)).toBeInTheDocument(); // history message
    expect(screen.getByText("No runs yet")).toBeInTheDocument(); // header pill
    expect(screen.getByText("step_one")).toBeInTheDocument(); // graph still renders
    expect(getRun).not.toHaveBeenCalled();
  });

  it("shows the API error instead of a blank page when the workflow is gone", async () => {
    getWorkflow.mockRejectedValue(new Error("Workflow not found"));
    renderDetail();
    expect(await screen.findByRole("alert")).toHaveTextContent("Workflow not found");
  });

  it("shows a PARTIAL run as its own state, not as failed", async () => {
    getWorkflowRuns.mockResolvedValue([run({ status: "partial" })]);
    getRun.mockResolvedValue({ ...run({ status: "partial" }), task_runs: [] });
    renderDetail();

    const pills = await screen.findAllByText("partial");
    expect(pills.length).toBeGreaterThan(0);
    for (const p of pills) {
      expect(p).toHaveClass("status-partial");
      expect(p).not.toHaveClass("status-failed");
    }
  });

  it("renders a SKIPPED task without crashing, styled and dashed in the graph", async () => {
    getWorkflowRuns.mockResolvedValue([run({ status: "partial" })]);
    getRun.mockResolvedValue({
      ...run({ status: "partial" }),
      task_runs: [
        taskRun({ id: 1, task_id: 1, status: "success" }),
        taskRun({ id: 2, task_id: 2, status: "failed", error_message: "exit code 1" }),
        taskRun({ id: 3, task_id: 3, status: "skipped" }),
      ],
    });
    const { container } = renderDetail();

    const list = await screen.findByText(/task attempts/);
    const panel = list.closest(".run-detail");
    expect(within(panel).getByText("skipped")).toHaveClass("status-skipped");
    expect(within(panel).getByText("exit code 1")).toBeInTheDocument();
    expect(container.querySelectorAll("rect[stroke-dasharray]")).toHaveLength(1);
  });

  it("lists every retry attempt but colours the task by its latest attempt", async () => {
    getWorkflowRuns.mockResolvedValue([run({ status: "failed" })]);
    getRun.mockResolvedValue({
      ...run({ status: "failed" }),
      task_runs: [
        taskRun({ id: 1, task_id: 2, attempt_number: 1, status: "retrying" }),
        taskRun({ id: 2, task_id: 2, attempt_number: 2, status: "retrying" }),
        taskRun({ id: 3, task_id: 2, attempt_number: 3, status: "failed" }),
      ],
    });
    const { container } = renderDetail();

    expect(await screen.findByText(/attempt 3/)).toBeInTheDocument();
    expect(screen.getByText(/attempt 1/)).toBeInTheDocument();
    const boom = [...container.querySelectorAll("svg g")].find((g) => g.textContent.startsWith("boom"));
    expect(boom.textContent).toContain("failed"); // not "retrying"
  });

  it("triggers a run and shows it in the history", async () => {
    getWorkflowRuns.mockResolvedValueOnce([]).mockResolvedValue([run({ id: 7, status: "pending" })]);
    getRun.mockResolvedValue({ ...run({ id: 7, status: "pending" }), task_runs: [] });
    triggerRun.mockResolvedValue(run({ id: 7, status: "pending" }));
    renderDetail();

    await userEvent.click(await screen.findByRole("button", { name: "Trigger run" }));

    expect(triggerRun).toHaveBeenCalledWith(1);
    expect(await screen.findByText("Run #7")).toBeInTheDocument();
  });

  it("shows the server's message when a trigger is rejected (e.g. max_active_runs)", async () => {
    triggerRun.mockRejectedValue(new Error("Workflow already has 1 active run(s) (max_active_runs=1)"));
    renderDetail();

    await userEvent.click(await screen.findByRole("button", { name: "Trigger run" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("max_active_runs=1");
  });

  it("polls every 2s while a run is active and stops once it is terminal", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    getWorkflowRuns
      .mockResolvedValueOnce([run({ status: "running" })]) // initial load
      .mockResolvedValueOnce([run({ status: "running" })]) // poll 1
      .mockResolvedValue([run({ status: "success" })]); // poll 2 onwards
    getRun.mockResolvedValue({ ...run({ status: "success" }), task_runs: [taskRun()] });
    renderDetail();

    await screen.findByText("Run #10");
    expect(getWorkflowRuns).toHaveBeenCalledTimes(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
    expect(getWorkflowRuns).toHaveBeenCalledTimes(2);

    await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
    expect(getWorkflowRuns).toHaveBeenCalledTimes(3); // this poll saw "success"

    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(getWorkflowRuns).toHaveBeenCalledTimes(3); // ...and polling stopped
  });
});