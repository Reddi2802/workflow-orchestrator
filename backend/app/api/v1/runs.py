from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.models.entities import WorkflowRun
from app.schemas.run import WorkflowRunDetailRead

router = APIRouter(prefix="/api/v1", tags=["runs"])


@router.get("/runs/{run_id}", response_model=WorkflowRunDetailRead)
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = (
        db.query(WorkflowRun)
        .options(selectinload(WorkflowRun.task_runs))
        .filter(WorkflowRun.id == run_id)
        .first()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="WorkflowRun not found")
    return run
