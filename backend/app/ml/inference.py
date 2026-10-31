# app/ml/inference.py
"""
Turn Task rows into the feature rows the predictor expects, at prediction time.

This is the serving half of app/ml/features.py: the same columns, built from a
Task that has not run yet instead of from a finished TaskRun. It reuses
parse_command and _dag_features from features.py on purpose, so a feature can
not be computed one way for training and another way for serving.

How each column is filled when predicting for a task that is about to run:
  cmd_*                      parsed from Task.command (same parser as training)
  dag_depth, n_upstream, n_downstream, n_tasks_in_workflow
                             from the workflow's current dependency edges
  max_retries                Task.max_retries
  attempt_number             1 (a prediction is for the first attempt)
  running_at_start           how many TaskRuns are RUNNING right now. In
                             training this was "how many others were running
                             when this one started"; no TaskRun of the task
                             being predicted exists yet, so the two match.
  prev_runs, prev_mean_duration
                             this task's earlier successful runs, all of them
                             (they all finished before now)
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ml.features import FEATURE_COLUMNS, _dag_features, parse_command
from app.models.entities import Task, TaskRun
from app.models.enums import TaskStatus


def _history(db: Session, task_ids: list[int]) -> dict[int, tuple[int, float]]:
    """task_id -> (number of earlier successful runs, their mean duration in
    seconds). Only runs with both timestamps and a positive duration, the same
    rows build_dataset keeps."""
    duration = func.extract("epoch", TaskRun.completed_at - TaskRun.started_at)
    rows = db.execute(
        select(TaskRun.task_id, func.count(), func.avg(duration))
        .where(
            TaskRun.task_id.in_(task_ids),
            TaskRun.status == TaskStatus.SUCCESS,
            TaskRun.started_at.is_not(None),
            TaskRun.completed_at.is_not(None),
            duration > 0,
        )
        .group_by(TaskRun.task_id)
    ).all()
    return {task_id: (int(n), float(mean)) for task_id, n, mean in rows}


def build_inference_frame(db: Session, task_ids: list[int]) -> pd.DataFrame:
    """One row per task id that exists, indexed by task id, with exactly the
    FEATURE_COLUMNS in order. Unknown ids are simply absent from the result."""
    if not task_ids:
        return pd.DataFrame(columns=FEATURE_COLUMNS)

    tasks = db.execute(select(Task).where(Task.id.in_(task_ids))).scalars().all()
    if not tasks:
        return pd.DataFrame(columns=FEATURE_COLUMNS)

    dag = _dag_features(db, sorted({t.workflow_id for t in tasks}))
    history = _history(db, [t.id for t in tasks])
    running_now = db.execute(
        select(func.count()).select_from(TaskRun).where(TaskRun.status == TaskStatus.RUNNING)
    ).scalar_one()

    rows = []
    for task in tasks:
        prev_runs, prev_mean = history.get(task.id, (0, np.nan))
        row = {
            **parse_command(task.command),
            **dag[task.id],
            "max_retries": task.max_retries,
            "attempt_number": 1,
            "running_at_start": int(running_now),
            "prev_runs": prev_runs,
            "prev_mean_duration": prev_mean,
        }
        rows.append(row)

    frame = pd.DataFrame(rows, index=[t.id for t in tasks])
    frame.index.name = "task_id"
    return frame[FEATURE_COLUMNS]
