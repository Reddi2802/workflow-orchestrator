import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import WorkflowList from "./WorkflowList.jsx";
import { listWorkflows } from "../api.js";

vi.mock("../api.js", () => ({ listWorkflows: vi.fn(), deleteWorkflow: vi.fn() }));

const wf = (over = {}) => ({ id: 1, name: "wf", task_count: 2, latest_run_status: null, ...over });

beforeEach(() => vi.resetAllMocks());

describe("WorkflowList", () => {
  it("shows the empty state, not a crash or a spinner, when there are no workflows", async () => {
    listWorkflows.mockResolvedValue([]);
    render(<WorkflowList onSelect={() => {}} onCreate={() => {}} />);

    expect(await screen.findByText("Your workspace is ready")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create your first workflow" })).toBeInTheDocument();
    expect(screen.queryByText("Loading workflows")).not.toBeInTheDocument();
  });

  it("renders task counts (with correct pluralisation) and a distinct 'No runs yet' pill", async () => {
    listWorkflows.mockResolvedValue([
      wf({ id: 1, name: "diamond", task_count: 4 }),
      wf({ id: 2, name: "solo", task_count: 1 }),
    ]);
    render(<WorkflowList onSelect={() => {}} onCreate={() => {}} />);

    expect(await screen.findByText("diamond")).toBeInTheDocument();
    expect(screen.getByText("4 tasks")).toBeInTheDocument();
    expect(screen.getByText("1 task")).toBeInTheDocument();
    expect(screen.getAllByText("No runs yet")).toHaveLength(2);
  });

  it("styles a PARTIAL latest run differently from FAILED", async () => {
    listWorkflows.mockResolvedValue([
      wf({ id: 1, name: "a", latest_run_status: "partial" }),
      wf({ id: 2, name: "b", latest_run_status: "failed" }),
    ]);
    render(<WorkflowList onSelect={() => {}} onCreate={() => {}} />);

    const partial = await screen.findByText("partial");
    const failed = screen.getByText("failed");
    expect(partial).toHaveClass("status-partial");
    expect(partial).not.toHaveClass("status-failed");
    expect(failed).toHaveClass("status-failed");
  });

  it("shows the API error message when loading fails", async () => {
    listWorkflows.mockRejectedValue(new Error("Not Found"));
    render(<WorkflowList onSelect={() => {}} onCreate={() => {}} />);

    expect(await screen.findByText("Not Found")).toBeInTheDocument();
    expect(screen.queryByText("Your workspace is ready")).not.toBeInTheDocument();
  });

  it("calls onSelect with the workflow id when a row is clicked", async () => {
    listWorkflows.mockResolvedValue([wf({ id: 7, name: "pick-me" })]);
    const onSelect = vi.fn();
    render(<WorkflowList onSelect={onSelect} onCreate={() => {}} />);

    await userEvent.click(await screen.findByText("pick-me"));
    expect(onSelect).toHaveBeenCalledWith(7);
  });
});