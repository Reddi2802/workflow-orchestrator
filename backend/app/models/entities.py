# app/models/entities.py
from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Integer,
    JSON, String, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import TaskStatus, TriggerType, WorkflowRunStatus


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    cron_expression: Mapped[str | None] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_active_runs: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    tasks: Mapped[list["Task"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan"
    )
    runs: Mapped[list["WorkflowRun"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan"
    )


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("workflow_id", "name", name="uq_task_workflow_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    command: Mapped[str] = mapped_column(String, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retry_backoff_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    workflow: Mapped["Workflow"] = relationship(back_populates="tasks")

    # Edges where this task is the upstream (downstream depends on this task)
    downstream_dependencies: Mapped[list["TaskDependency"]] = relationship(
        foreign_keys="TaskDependency.upstream_task_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    # Edges where this task is the downstream (this task depends on upstream)
    upstream_dependencies: Mapped[list["TaskDependency"]] = relationship(
        foreign_keys="TaskDependency.downstream_task_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class TaskDependency(Base):
    __tablename__ = "task_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "upstream_task_id", "downstream_task_id", name="uq_dependency_pair"
        ),
        CheckConstraint(
            "upstream_task_id != downstream_task_id", name="ck_no_self_dependency"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    upstream_task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    downstream_task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[WorkflowRunStatus] = mapped_column(nullable=False)
    trigger_type: Mapped[TriggerType] = mapped_column(nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    workflow: Mapped["Workflow"] = relationship(back_populates="runs")
    task_runs: Mapped[list["TaskRun"]] = relationship(
        back_populates="workflow_run", cascade="all, delete-orphan"
    )


class TaskRun(Base):
    __tablename__ = "task_runs"
    __table_args__ = (
        UniqueConstraint(
            "workflow_run_id", "task_id", "attempt_number", name="uq_task_run_attempt"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_run_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[TaskStatus] = mapped_column(nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    checkpoint_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    workflow_run: Mapped["WorkflowRun"] = relationship(back_populates="task_runs")
