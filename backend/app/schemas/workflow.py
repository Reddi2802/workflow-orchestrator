from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.schemas.task import (
    TaskCreate,
    TaskDependencyCreate,
    TaskDependencyRead,
    TaskRead,
)


class WorkflowCreate(BaseModel):
    """
    POST /api/v1/workflows body — per Section 4, the workflow plus its
    tasks and dependencies arrive in ONE payload. `has_cycle()` runs
    against `tasks` + `dependencies` before anything is written; a cycle
    means a 400 and nothing lands in the DB (enforced in the endpoint,
    not here).
    """

    name: str
    description: str | None = None
    cron_expression: str | None = None
    is_active: bool = True
    max_active_runs: int = 1
    tasks: list[TaskCreate] = []
    dependencies: list[TaskDependencyCreate] = []

    @field_validator("dependencies")
    @classmethod
    def dependency_task_names_must_exist(
        cls, deps: list[TaskDependencyCreate], info
    ) -> list[TaskDependencyCreate]:
        tasks = info.data.get("tasks", [])
        task_names = {t.name for t in tasks}
        for dep in deps:
            if dep.upstream_task_name not in task_names:
                raise ValueError(
                    f"unknown upstream task name: {dep.upstream_task_name!r}"
                )
            if dep.downstream_task_name not in task_names:
                raise ValueError(
                    f"unknown downstream task name: {dep.downstream_task_name!r}"
                )
        return deps


class WorkflowUpdate(BaseModel):
    """PUT /api/v1/workflows/{id} — all fields optional (partial update)."""

    name: str | None = None
    description: str | None = None
    cron_expression: str | None = None
    is_active: bool | None = None
    max_active_runs: int | None = None


class WorkflowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    cron_expression: str | None
    is_active: bool
    max_active_runs: int
    created_at: datetime
    updated_at: datetime


class WorkflowDetailRead(WorkflowRead):
    """GET /api/v1/workflows/{id} — workflow with tasks + dependencies nested."""

    tasks: list[TaskRead] = []
    dependencies: list[TaskDependencyRead] = []
