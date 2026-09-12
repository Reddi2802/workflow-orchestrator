from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.engine.dag import has_cycle
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
    """Create a workflow after validating that its task graph is acyclic."""
    # has_cycle operates on ORM objects by task ID. Assign stable temporary
    # IDs for validation only — nothing is written to the DB yet, so this
    # never needs a rollback if it's cyclic.
    task_ids = {task.name: index for index, task in enumerate(payload.tasks, start=1)}
    graph_tasks = [Task(id=task_id) for task_id in task_ids.values()]
    graph_dependencies = [
        TaskDependency(
            upstream_task_id=task_ids[dependency.upstream_task_name],
            downstream_task_id=task_ids[dependency.downstream_task_name],
        )
        for dependency in payload.dependencies
    ]
    if has_cycle(graph_tasks, graph_dependencies):
        raise HTTPException(
            status_code=400,
            detail="Workflow dependencies must not contain a cycle",
        )

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

    db.delete(workflow)
    db.commit()


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