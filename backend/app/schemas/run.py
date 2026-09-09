from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.enums import TaskStatus, TriggerType, WorkflowRunStatus


class TaskRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workflow_run_id: int
    task_id: int
    status: TaskStatus
    attempt_number: int
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None
    checkpoint_data: dict[str, Any] | None


class WorkflowRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workflow_id: int
    status: WorkflowRunStatus
    trigger_type: TriggerType
    triggered_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class WorkflowRunDetailRead(WorkflowRunRead):
    """GET /api/v1/runs/{id} — run with its TaskRuns nested."""

    task_runs: list[TaskRunRead] = []
