from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.models.entities import Task, TaskDependency, Workflow, WorkflowRun
from app.schemas.run import WorkflowRunRead
from app.schemas.workflow import (
    WorkflowCreate,
    WorkflowDetailRead,
    WorkflowRead,
    WorkflowUpdate,
)

router = APIRouter(prefix="/api/v1", tags=["workflows"])


@router.post("/workflows", response_model=WorkflowDetailRead, status_code=201)
def create_workflow(payload: WorkflowCreate, db: Session = Depends(get_db)):
    """
    POST /api/v1/workflows

    NOTE: cycle detection is NOT wired in here yet on purpose — that's a
    Week 2 task, blocked on Hridhayansh's has_cycle(tasks, dependencies).
    Once he delivers it, call it on payload.tasks/payload.dependencies
    BEFORE the db.add() calls below and raise HTTPException(400, ...) if
    it returns True, so nothing partial gets committed.
    """
    workflow = Workflow(
        name=payload.name,
        description=payload.description,
        cron_expression=payload.cron_expression,
        is_active=payload.is_active,
        max_active_runs=payload.max_active_runs,
    )
    db.add(workflow)
    db.flush()  # assigns workflow.id without committing yet

    name_to_task: dict[str, Task] = {}
    for task_in in payload.tasks:
        task = Task(
            workflow_id=workflow.id,
            name=task_in.name,
            command=task_in.command,
            max_retries=task_in.max_retries,
            retry_backoff_seconds=task_in.retry_backoff_seconds,
        )
        db.add(task)
        name_to_task[task_in.name] = task
    db.flush()  # assigns each task.id

    # Section 3 correction: TaskDependency has no workflow_id — just the
    # two task FKs, resolved here from the names in the request payload.
    for dep_in in payload.dependencies:
        db.add(
            TaskDependency(
                upstream_task_id=name_to_task[dep_in.upstream_task_name].id,
                downstream_task_id=name_to_task[dep_in.downstream_task_name].id,
            )
        )

    db.commit()
    db.refresh(workflow)

    # NOTE: Workflow has no `dependencies` relationship on the ORM model
    # (only `tasks` and `runs`), so response_model can't auto-populate it —
    # same reason get_workflow() below fetches these manually. Skipping
    # this would silently return an empty `dependencies: []` instead of
    # an error.
    created_dependencies = (
        db.query(TaskDependency)
        .filter(TaskDependency.upstream_task_id.in_([t.id for t in name_to_task.values()]))
        .all()
    )

    result = WorkflowDetailRead.model_validate(workflow)
    result.dependencies = created_dependencies
    return result


@router.get("/workflows", response_model=list[WorkflowRead])
def list_workflows(db: Session = Depends(get_db)):
    return db.query(Workflow).order_by(Workflow.id).all()


@router.get("/workflows/{workflow_id}", response_model=WorkflowDetailRead)
def get_workflow(workflow_id: int, db: Session = Depends(get_db)):
    workflow = (
        db.query(Workflow)
        .options(selectinload(Workflow.tasks))
        .filter(Workflow.id == workflow_id)
        .first()
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Section 3 correction: no workflow_id on TaskDependency, so edges for
    # this workflow are found by joining through its task ids.
    task_ids = [t.id for t in workflow.tasks]
    dependencies = (
        db.query(TaskDependency)
        .filter(TaskDependency.upstream_task_id.in_(task_ids))
        .all()
        if task_ids
        else []
    )

    result = WorkflowDetailRead.model_validate(workflow)
    result.dependencies = dependencies
    return result


@router.put("/workflows/{workflow_id}", response_model=WorkflowRead)
def update_workflow(workflow_id: int, payload: WorkflowUpdate, db: Session = Depends(get_db)):
    workflow = db.query(Workflow).filter(Workflow.id == workflow_id).first()
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(workflow, field, value)

    db.commit()
    db.refresh(workflow)
    return workflow


@router.delete("/workflows/{workflow_id}", status_code=204)
def delete_workflow(workflow_id: int, db: Session = Depends(get_db)):
    workflow = db.query(Workflow).filter(Workflow.id == workflow_id).first()
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Relies on cascade="all, delete-orphan" on Workflow.tasks/.runs, and
    # ondelete=CASCADE at the DB level from Task -> TaskDependency and
    # WorkflowRun -> TaskRun (per Hridhayansh's entities.py).
    db.delete(workflow)
    db.commit()
    return None


@router.get("/workflows/{workflow_id}/runs", response_model=list[WorkflowRunRead])
def list_workflow_runs(workflow_id: int, db: Session = Depends(get_db)):
    workflow = db.query(Workflow).filter(Workflow.id == workflow_id).first()
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    return (
        db.query(WorkflowRun)
        .filter(WorkflowRun.workflow_id == workflow_id)
        .order_by(WorkflowRun.triggered_at.desc())
        .all()
    )
