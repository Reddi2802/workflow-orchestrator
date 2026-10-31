from typing import Any, Literal

from pydantic import BaseModel


class TaskPredictionRead(BaseModel):
    task_id: int
    name: str
    predicted_seconds: float
    # "history": the task has run before, the estimate leans on its own past runs.
    # "cold_start": it has never succeeded, the estimate comes from its command,
    # its place in the DAG and the current load only.
    basis: Literal["history", "cold_start"]
    # Predicted at or above the training set's long_quantile of durations
    # (see PredictorInfoRead.long_threshold_seconds). Relative, not absolute.
    likely_long: bool
    on_critical_path: bool = False


class WorkflowPredictionRead(BaseModel):
    workflow_id: int
    # Longest chain of dependent tasks by predicted duration. Execution time
    # only: it ignores waiting for a free worker (the pool has 2) and
    # assumes everything else could run in parallel.
    estimated_total_seconds: float
    critical_path: list[int]
    long_threshold_seconds: float
    tasks: list[TaskPredictionRead]


class PredictorInfoRead(BaseModel):
    trained_at: str
    training_rows: int
    holdout_rows: int
    long_threshold_seconds: float
    long_quantile: float
    history_strategy: Literal["previous_mean", "history_model"]
    # Holdout scores per model: {"cold_start_model": {"mae": ..., "rmse": ..., "r2": ..., "n": ...}, ...}
    holdout: dict[str, Any]
