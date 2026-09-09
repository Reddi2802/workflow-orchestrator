from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator


class TaskCreate(BaseModel):
    """Nested inside WorkflowCreate — no `id` or `workflow_id`, those are
    assigned server-side."""

    name: str
    command: str
    max_retries: int = 0
    retry_backoff_seconds: int = 30


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workflow_id: int
    name: str
    command: str
    max_retries: int
    retry_backoff_seconds: int
    created_at: datetime


class TaskDependencyCreate(BaseModel):
    """
    Nested inside WorkflowCreate. References tasks by their `name` within
    the same payload (not by id — ids don't exist yet until the tasks are
    inserted). The API layer resolves name -> id during the insert.

    NOTE (Section 3 correction): TaskDependency has NO workflow_id column.
    It's derived via either task's workflow_id. This schema reflects the
    request shape (still scoped to one workflow's payload); the DB insert
    just doesn't get a workflow_id field to set.
    """

    upstream_task_name: str
    downstream_task_name: str

    @model_validator(mode="after")
    def upstream_and_downstream_must_differ(self) -> "TaskDependencyCreate":
        if self.upstream_task_name == self.downstream_task_name:
            raise ValueError(
                "upstream_task_name and downstream_task_name must differ "
                "(a task cannot depend on itself)"
            )
        return self


class TaskDependencyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    upstream_task_id: int
    downstream_task_id: int
