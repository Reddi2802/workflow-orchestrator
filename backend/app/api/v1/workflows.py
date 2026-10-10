from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.engine.dag import has_cycle
from app.models.entities import Task, TaskDependency, Workflow, WorkflowRun
from app.models.enums import TriggerType, WorkflowRunStatus
from app.schemas.run import WorkflowRunRead
from app.schemas.workflow import (
    WorkflowCreate,
    WorkflowDetailRead,
    WorkflowRead,
    WorkflowUpdate,
)

router = APIRouter(prefix="/api/v1", tags=["workflows"])


def _latest_run_status_by_workflow(db: Session, workflow_ids: list[int]) -> dict[int, str]:
    """One workflow_id -> most-recent WorkflowRun.status, for every id in
    workflow_ids. A workflow with no runs yet simply has no key in the
    result -- callers treat a missing key as "no runs", not as an error.

    DISTINCT ON is Postgres-specific, which is fine here: Section 2 pins
    Postgres as the DB, and this avoids an N+1 query (one run-lookup per
    workflow) that an ORM-loop version would otherwise need -- the whole
    point of computing this server-side instead of making the frontend
    fetch each workflow's runs separately to render a status badge.
    DISTINCT ON requires its own column(s) to lead ORDER BY, which is why
    workflow_id comes before triggered_at here.
    """
    if not workflow_ids:
        return {}
    rows = (
        db.query(WorkflowRun.workflow_id, WorkflowRun.status)
        .filter(WorkflowRun.workflow_id.in_(workflow_ids))
        .distinct(WorkflowRun.workflow_id)
        .order_by(WorkflowRun.workflow_id, WorkflowRun.triggered_at.desc())
        .all()
    )
    return {workflow_id: status for workflow_id, status in rows}


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
    # Eager-load tasks so we can report a task_count per workflow without
    # returning the full nested tasks payload — WorkflowRead is meant to
    # stay a lightweight summary; full task detail lives behind
    # get_workflow()/WorkflowDetailRead instead.
    workflows = (
        db.query(Workflow)
        .options(selectinload(Workflow.tasks))
        .order_by(Workflow.id)
        .all()
    )

    latest_status = _latest_run_status_by_workflow(db, [w.id for w in workflows])

    results = []
    for workflow in workflows:
        item = WorkflowRead.model_validate(workflow)
        item.task_count = len(workflow.tasks)
        item.latest_run_status = latest_status.get(workflow.id)
        results.append(item)
    return results


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
    # task_count and latest_run_status aren't real attributes on the
    # Workflow ORM model -- model_validate() silently falls back to each
    # field's schema default (0 / None) for them unless set explicitly
    # here, the same way list_workflows() already had to for task_count.
    # Missing this was a real bug: every GET /workflows/{id} response
    # would otherwise report task_count=0 regardless of actual task
    # count. Not currently visible in the merged frontend (WorkflowDetail
    # renders workflow.tasks.length instead of workflow.task_count), but
    # a landmine for anything that reads it later.
    result.task_count = len(workflow.tasks)
    result.latest_run_status = _latest_run_status_by_workflow(db, [workflow.id]).get(workflow.id)
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


@router.post("/workflows/{workflow_id}/runs", response_model=WorkflowRunRead, status_code=201)
def trigger_workflow_run(workflow_id: int, db: Session = Depends(get_db)):
    """Manually trigger a run. Creates the row as PENDING with trigger_type=MANUAL;
    the dispatch loop (app/engine/worker_pool.py) picks up any PENDING run, so
    nothing in app/engine/ needs to change.

    Respects max_active_runs the same way the cron scheduler does, so a manual
    trigger can't be used to bypass it -- 409 if the limit is already reached.
    """
    workflow = db.query(Workflow).filter(Workflow.id == workflow_id).first()
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    active = (
        db.query(WorkflowRun)
        .filter(
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.status.in_([WorkflowRunStatus.PENDING, WorkflowRunStatus.RUNNING]),
        )
        .count()
    )
    if active >= workflow.max_active_runs:
        raise HTTPException(
            status_code=409,
            detail=f"Workflow already has {active} active run(s) (max_active_runs={workflow.max_active_runs})",
        )

    run = WorkflowRun(
        workflow_id=workflow_id,
        status=WorkflowRunStatus.PENDING,
        trigger_type=TriggerType.MANUAL,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run