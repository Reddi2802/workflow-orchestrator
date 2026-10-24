# app/ml/features.py
"""
Week 6: turn TaskRun history into a training table for the Week 8 bottleneck
predictor.

    build_dataset(db) -> pandas.DataFrame      one row per successful TaskRun
    time_split(df)    -> (train, test)         split by time, never at random

TARGET: `duration_seconds` = completed_at - started_at of a TaskRun. Only
SUCCESS rows with both timestamps and a positive duration are kept. A failed
or retried attempt's duration means something different (it died early), and
mixing it in would teach the model the wrong thing.

Column groups (see the *_COLUMNS constants):
  ID_COLUMNS       keys and timestamps for joining back and for splitting.
                   Never features.
  FEATURE_COLUMNS  what a model may use. Everything here is knowable when the
                   task is about to start. `queue_wait_seconds` is NOT in this
                   list: it is only known once the task has started.

LEAKAGE RULES (the reason this module exists as code, not a notebook):
  1. History features (`prev_runs`, `prev_mean_duration`) use only runs of the
     same task that FINISHED strictly before this row STARTED. A row is never
     in its own history, and a run that overlapped it is not either.
  2. `running_at_start` counts only what was already running at the instant
     this row started.
  3. Split by time (`time_split`) or by workflow, never `train_test_split`
     with shuffling: rows from one workflow run share structure, and a random
     split lets the model see the future.

Deliberately NOT included: hour-of-day / day-of-week. A dataset generated in
one sitting has a constant hour, so those columns would carry no signal and
only invite the model to memorise noise. Revisit if real, long-running
history exists.
"""
from __future__ import annotations

import re

import networkx as nx
import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Task, TaskDependency, TaskRun, Workflow, WorkflowRun
from app.models.enums import TaskStatus

TARGET = "duration_seconds"

ID_COLUMNS = [
    "task_run_id", "workflow_id", "workflow_run_id", "task_id",
    "started_at", "completed_at",
]

FEATURE_COLUMNS = [
    # parsed from Task.command
    "cmd_family", "cmd_length", "cmd_sleep_s", "cmd_sum_n", "cmd_sort_n",
    # shape of the DAG
    "dag_depth", "n_upstream", "n_downstream", "n_tasks_in_workflow",
    # retry config / attempt
    "max_retries", "attempt_number",
    # system load at the moment this task started
    "running_at_start",
    # this task's own earlier results (leak-safe, see module docstring)
    "prev_runs", "prev_mean_duration",
]

# prev_mean_duration is legitimately missing for a task's first ever run.
# Everything else must be present in every row.
REQUIRED_COLUMNS = (
    ID_COLUMNS + [TARGET, "queue_wait_seconds"]
    + [c for c in FEATURE_COLUMNS if c != "prev_mean_duration"]
)

OUTPUT_COLUMNS = ID_COLUMNS + [TARGET, "queue_wait_seconds"] + FEATURE_COLUMNS


# --- command parsing ---------------------------------------------------------

_SLEEP_RE = re.compile(r"time\.sleep\(\s*([0-9]*\.?[0-9]+)\s*\)")
_SUM_RE = re.compile(r"sum\(\s*range\(\s*(\d+)\s*\)\s*\)")
_SORT_RE = re.compile(r"random\.random\(\)\s+for\s+_\s+in\s+range\(\s*(\d+)\s*\)")


def parse_command(command: str) -> dict:
    """Pull numeric parameters out of a task's shell command.

    Patterns are anchored on the call syntax (`time.sleep(...)`,
    `sum(range(N))`, ...), not "any number in the string": the command also
    contains the interpreter path, which can carry digits (python3.11).
    Unrecognised commands get family "other" and zeros.
    """
    sleep = _SLEEP_RE.search(command)
    sum_n = _SUM_RE.search(command)
    sort_n = _SORT_RE.search(command)

    if sleep and sum_n:
        family = "sleep_cpu"
    elif sleep:
        family = "sleep"
    elif sum_n:
        family = "cpu_sum"
    elif sort_n:
        family = "sort"
    else:
        family = "other"

    return {
        "cmd_family": family,
        "cmd_length": len(command),
        "cmd_sleep_s": float(sleep.group(1)) if sleep else 0.0,
        "cmd_sum_n": int(sum_n.group(1)) if sum_n else 0,
        "cmd_sort_n": int(sort_n.group(1)) if sort_n else 0,
    }


# --- DAG features ------------------------------------------------------------

def _dag_features(db: Session, workflow_ids: list[int]) -> dict[int, dict]:
    """task_id -> {dag_depth, n_upstream, n_downstream, n_tasks_in_workflow}.

    dag_depth = length of the longest path from any root to the task (roots
    are 0). n_upstream / n_downstream are DIRECT neighbours only.
    """
    out: dict[int, dict] = {}
    if not workflow_ids:
        return out

    tasks = db.execute(
        select(Task.id, Task.workflow_id).where(Task.workflow_id.in_(workflow_ids))
    ).all()
    by_workflow: dict[int, list[int]] = {}
    for task_id, workflow_id in tasks:
        by_workflow.setdefault(workflow_id, []).append(task_id)

    all_task_ids = [t[0] for t in tasks]
    edges = db.execute(
        select(TaskDependency.upstream_task_id, TaskDependency.downstream_task_id)
        .where(TaskDependency.upstream_task_id.in_(all_task_ids))
    ).all()

    graph = nx.DiGraph()
    graph.add_nodes_from(all_task_ids)
    graph.add_edges_from(edges)

    depth: dict[int, int] = {}
    for node in nx.topological_sort(graph):
        preds = list(graph.predecessors(node))
        depth[node] = 0 if not preds else 1 + max(depth[p] for p in preds)

    for workflow_id, task_ids in by_workflow.items():
        for task_id in task_ids:
            out[task_id] = {
                "dag_depth": depth[task_id],
                "n_upstream": graph.in_degree(task_id),
                "n_downstream": graph.out_degree(task_id),
                "n_tasks_in_workflow": len(task_ids),
            }
    return out


# --- load / history features ---------------------------------------------------

def _epoch_seconds(series: pd.Series) -> np.ndarray:
    return ((series - pd.Timestamp("1970-01-01")).dt.total_seconds()).to_numpy(dtype=float)


def _running_at_start(db: Session, started_at: pd.Series) -> np.ndarray:
    """For each row: how many OTHER TaskRuns (any status) were already running
    at the instant this row started. A TaskRun with a start but no end is
    treated as still running."""
    rows = db.execute(
        select(TaskRun.started_at, TaskRun.completed_at).where(TaskRun.started_at.is_not(None))
    ).all()
    all_df = pd.DataFrame(rows, columns=["s", "e"])
    starts = _epoch_seconds(all_df["s"])
    ends = np.where(all_df["e"].isna(), np.inf, _epoch_seconds(all_df["e"].fillna(pd.Timestamp("1970-01-01"))))

    mine = _epoch_seconds(started_at)
    # `-1` removes the row itself (its own start <= mine and its end > mine).
    return np.array([int(((starts <= s) & (ends > s)).sum()) - 1 for s in mine])


def _add_history(df: pd.DataFrame) -> pd.DataFrame:
    """prev_runs / prev_mean_duration per row, using only runs of the same
    task that completed strictly before this row started."""
    df = df.copy()
    df["prev_runs"] = 0
    df["prev_mean_duration"] = np.nan

    for _, idx in df.groupby("task_id").groups.items():
        group = df.loc[idx]
        order = group["completed_at"].argsort(kind="stable").to_numpy()
        completed = _epoch_seconds(group["completed_at"])[order]
        cum = np.cumsum(group[TARGET].to_numpy()[order])

        starts = _epoch_seconds(group["started_at"])
        k = np.searchsorted(completed, starts, side="left")  # runs finished before each start

        df.loc[idx, "prev_runs"] = k
        means = np.where(k > 0, cum[np.maximum(k - 1, 0)] / np.maximum(k, 1), np.nan)
        df.loc[idx, "prev_mean_duration"] = means
    df["prev_runs"] = df["prev_runs"].astype(int)
    return df


# --- public API -------------------------------------------------------------

def build_dataset(
    db: Session,
    *,
    name_prefix: str | None = None,
    require_created_at: bool = True,
) -> pd.DataFrame:
    """One row per successful TaskRun. See the module docstring for what is
    kept, what the columns mean and the leakage rules.

    name_prefix: only workflows whose name starts with this (the data
        generator names its workflows "ds-..."), so hand-made or test rows in
        the same database do not leak into the dataset.
    require_created_at: drop rows with no created_at (inserted before the
        TaskRun.created_at migration), since their queue wait is unknown.
        Set False to keep them (queue_wait_seconds is then NaN).
    """
    stmt = (
        select(
            TaskRun.id, Workflow.id, WorkflowRun.id, Task.id,
            TaskRun.started_at, TaskRun.completed_at, TaskRun.created_at,
            Task.command, Task.max_retries, TaskRun.attempt_number,
        )
        .join(Task, TaskRun.task_id == Task.id)
        .join(WorkflowRun, TaskRun.workflow_run_id == WorkflowRun.id)
        .join(Workflow, WorkflowRun.workflow_id == Workflow.id)
        .where(
            TaskRun.status == TaskStatus.SUCCESS,
            TaskRun.started_at.is_not(None),
            TaskRun.completed_at.is_not(None),
        )
    )
    if name_prefix:
        stmt = stmt.where(Workflow.name.startswith(name_prefix))

    columns = [
        "task_run_id", "workflow_id", "workflow_run_id", "task_id",
        "started_at", "completed_at", "created_at",
        "command", "max_retries", "attempt_number",
    ]
    df = pd.DataFrame(db.execute(stmt).all(), columns=columns)
    for col in ("started_at", "completed_at", "created_at"):
        df[col] = pd.to_datetime(df[col])

    df[TARGET] = (df["completed_at"] - df["started_at"]).dt.total_seconds()
    df = df[df[TARGET] > 0].reset_index(drop=True)

    if df.empty:
        return pd.DataFrame({c: pd.Series(dtype="object") for c in OUTPUT_COLUMNS})

    # command features
    parsed = pd.DataFrame([parse_command(c) for c in df["command"]])
    df = pd.concat([df, parsed], axis=1)

    # DAG features
    dag = _dag_features(db, sorted(df["workflow_id"].unique().tolist()))
    dag_df = pd.DataFrame([dag[t] for t in df["task_id"]])
    df = pd.concat([df, dag_df], axis=1)

    # load + history (history is computed BEFORE dropping rows without
    # created_at: those rows still finished earlier and still inform it)
    df["running_at_start"] = _running_at_start(db, df["started_at"])
    df = _add_history(df)

    df["queue_wait_seconds"] = (df["started_at"] - df["created_at"]).dt.total_seconds()
    if require_created_at:
        df = df[df["created_at"].notna()].reset_index(drop=True)

    return df[OUTPUT_COLUMNS].sort_values("started_at").reset_index(drop=True)


def time_split(df: pd.DataFrame, test_fraction: float = 0.2) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Chronological split: the earliest (1 - test_fraction) of rows by
    started_at go to train, the rest to test. Every test row starts no earlier
    than any train row, so the model is always evaluated on the future."""
    if not 0 < test_fraction < 1:
        raise ValueError("test_fraction must be between 0 and 1 (exclusive)")
    ordered = df.sort_values("started_at").reset_index(drop=True)
    cut = int(round(len(ordered) * (1 - test_fraction)))
    return ordered.iloc[:cut].copy(), ordered.iloc[cut:].copy()
