import { describe, it, expect } from "vitest";
import { statusClass, isActive, latestStatusByTask } from "./status.js";

describe("statusClass", () => {
  it("gives every real enum value its own class", () => {
    for (const s of ["pending", "running", "success", "failed", "retrying", "skipped", "partial"]) {
      expect(statusClass(s)).toBe(`status-${s}`);
    }
  });
  it("falls back instead of crashing on unknown or missing values", () => {
    expect(statusClass("weird")).toBe("status-pending");
    expect(statusClass(null)).toBe("status-none");
  });
});

describe("isActive", () => {
  it("is true only for states that will still change", () => {
    expect(["pending", "running", "retrying"].every(isActive)).toBe(true);
    expect(["success", "failed", "skipped", "partial"].some(isActive)).toBe(false);
  });
});

describe("latestStatusByTask", () => {
  it("uses the highest attempt_number per task", () => {
    const result = latestStatusByTask([
      { task_id: 1, attempt_number: 1, status: "failed" },
      { task_id: 1, attempt_number: 2, status: "success" },
    ]);
    expect(result).toEqual({ 1: "success" });
  });
});