"""
Week 8: duration predictions, read-only.

    GET /api/v1/predictor                         what model is loaded and how good it was
    GET /api/v1/tasks/{task_id}/prediction        one task
    GET /api/v1/workflows/{workflow_id}/predictions
                                                  every task, plus the critical path

All three answer 503 with an explanation if no model has been trained yet
(`uv run python scripts/train_predictor.py`). Nothing here writes to the
database or changes how runs execute: predictions are advice, not scheduling.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.ml.inference import build_inference_frame
from app.ml.predictor import PredictorNotAvailable, critical_path, get_predictor
from app.models.entities import Task, TaskDependency, Workflow
from app.schemas.prediction import (
    PredictorInfoRead,
    TaskPredictionRead,
    WorkflowPredictionRead,
)

router = APIRouter(prefix="/api/v1", tags=["predictions"])


def _predictor():
    try:
        return get_predictor()
    except PredictorNotAvailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


def _predict_tasks(db: Session, predictor, tasks: list[Task]) -> list[TaskPredictionRead]:
    frame = build_inference_frame(db, [t.id for t in tasks])
    result = predictor.predict(frame)
    out = []
    for task in tasks:
        seconds = round(float(result.loc[task.id, "predicted_seconds"]), 3)
        out.append(TaskPredictionRead(
            task_id=task.id,
            name=task.name,
            predicted_seconds=seconds,
            basis=result.loc[task.id, "basis"],
            likely_long=predictor.is_long(seconds),
        ))
    return out


@router.get("/predictor", response_model=PredictorInfoRead)
def predictor_info():
    meta = _predictor().metadata
    return PredictorInfoRead(
        trained_at=meta["trained_at"],
        training_rows=meta["training_rows"],
        holdout_rows=meta["holdout_rows"],
        long_threshold_seconds=meta["long_threshold_seconds"],
        long_quantile=meta["long_quantile"],
        history_strategy=meta["history_strategy"],
        holdout=meta["holdout"],
    )


@router.get("/tasks/{task_id}/prediction", response_model=TaskPredictionRead)
def task_prediction(task_id: int, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    predictor = _predictor()
    return _predict_tasks(db, predictor, [task])[0]


@router.get("/workflows/{workflow_id}/predictions", response_model=WorkflowPredictionRead)
def workflow_predictions(workflow_id: int, db: Session = Depends(get_db)):
    workflow = db.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    predictor = _predictor()

    tasks = sorted(workflow.tasks, key=lambda t: t.id)
    if not tasks:
        return WorkflowPredictionRead(
            workflow_id=workflow_id, estimated_total_seconds=0.0, critical_path=[],
            long_threshold_seconds=predictor.long_threshold_seconds, tasks=[],
        )

    predictions = _predict_tasks(db, predictor, tasks)
    edges = [
        (d.upstream_task_id, d.downstream_task_id)
        for d in db.execute(
            select(TaskDependency).where(TaskDependency.upstream_task_id.in_([t.id for t in tasks]))
        ).scalars().all()
    ]
    path, total = critical_path({p.task_id: p.predicted_seconds for p in predictions}, edges)
    on_path = set(path)
    for p in predictions:
        p.on_critical_path = p.task_id in on_path

    return WorkflowPredictionRead(
        workflow_id=workflow_id,
        estimated_total_seconds=round(total, 3),
        critical_path=path,
        long_threshold_seconds=predictor.long_threshold_seconds,
        tasks=predictions,
    )
