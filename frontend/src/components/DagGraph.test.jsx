import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import DagGraph from "./DagGraph.jsx";

const tasks = [
  { id: 1, name: "extract" },
  { id: 2, name: "clean" },
  { id: 3, name: "enrich" },
  { id: 4, name: "load" },
];
const deps = [
  { id: 1, upstream_task_id: 1, downstream_task_id: 2 },
  { id: 2, upstream_task_id: 1, downstream_task_id: 3 },
  { id: 3, upstream_task_id: 2, downstream_task_id: 4 },
  { id: 4, upstream_task_id: 3, downstream_task_id: 4 },
];

// Node x-position, read from the <g transform="translate(x y)"> wrapping its label.
const xOf = (container, name) => {
  const g = [...container.querySelectorAll("g")].find((n) => n.textContent.startsWith(name));
  return Number(g.getAttribute("transform").match(/translate\(([-\d.]+)/)[1]);
};

describe("DagGraph", () => {
  it("shows a message instead of an empty box when there are no tasks", () => {
    render(<DagGraph tasks={[]} dependencies={[]} />);
    expect(screen.getByText("This workflow has no tasks.")).toBeInTheDocument();
  });

  it("draws every task by name and one arrow per dependency", () => {
    const { container } = render(<DagGraph tasks={tasks} dependencies={deps} />);
    for (const t of tasks) expect(screen.getByText(t.name)).toBeInTheDocument();
    expect(container.querySelectorAll("path[marker-end]")).toHaveLength(4);
  });

  it("lays a diamond out left-to-right so structure is visible, not just a row of boxes", () => {
    const { container } = render(<DagGraph tasks={tasks} dependencies={deps} />);
    const [e, c, n, l] = ["extract", "clean", "enrich", "load"].map((x) => xOf(container, x));
    expect(e).toBeLessThan(c);
    expect(c).toBe(n); // the two parallel branches share a column
    expect(c).toBeLessThan(l);
  });

  it("outlines only skipped tasks with a dashed border", () => {
    const { container } = render(
      <DagGraph tasks={tasks} dependencies={deps} taskStatuses={{ 1: "success", 2: "failed", 3: "success", 4: "skipped" }} />
    );
    expect(container.querySelectorAll("rect[stroke-dasharray]")).toHaveLength(1);
  });

  it("does not crash on a status value it has never heard of", () => {
    render(<DagGraph tasks={tasks} dependencies={deps} taskStatuses={{ 1: "some_future_status" }} />);
    expect(screen.getByText("some_future_status")).toBeInTheDocument();
  });
});