# app/main.py
"""
FastAPI app entrypoint.

Combines pieces built independently by both of you:
- Paramash's Week 1 CRUD routers (workflows, runs)
- Hridhayansh's Week 1 cron polling loop
- Hridhayansh's Week 4 dispatch loop + worker pool
- Hridhayansh's Week 5 crash recovery (runs once at startup, before any
  background loop starts pulling from the queue)

All background work is launched as asyncio tasks via the lifespan hook, per
the "engine runs in-process" decision (Section 2 of the reference docs) --
no separate worker process or service to deploy/coordinate.

If either of you adds new routers or new startup/shutdown behavior later,
it goes in this same file -- don't let a second version of this file get
created independently again.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import predictions, runs, workflows
from app.core.database import SessionLocal
from app.engine.recovery import recover_incomplete_runs
from app.engine.scheduler import run_polling_loop
from app.engine.worker_pool import DEFAULT_WORKER_POOL_SIZE, dispatch_loop, worker_loop

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _recover_on_startup_sync(session_factory) -> list[int]:
    """One-shot wrapper mirroring the session-open/close pattern every
    other sync entry point in this codebase uses (_worker_cycle_sync,
    poll_and_trigger_sync's callers, etc.) -- open a session, do the
    (blocking) work, close it, regardless of outcome."""
    db = session_factory()
    try:
        return recover_incomplete_runs(db)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs to completion BEFORE any background task below is created --
    # deliberate ordering, not an oversight. Recovery re-enqueues rows for
    # WorkflowRuns that were already RUNNING when the process last died;
    # if a worker were already dequeuing while recovery was still deciding
    # what to re-enqueue, that's a real race. Blocking here removes it
    # entirely rather than depending on recovery's internal dedup guards
    # to paper over a concurrent run. asyncio.to_thread so this one-time
    # blocking DB work doesn't stall the event loop during startup, same
    # reasoning as every other sync DB call in this codebase.
    recovered_run_ids = await asyncio.to_thread(_recover_on_startup_sync, SessionLocal)
    if recovered_run_ids:
        logger.info(
            "Startup recovery: %d WorkflowRun(s) were still RUNNING from a "
            "previous process -- reconciled: %s",
            len(recovered_run_ids), recovered_run_ids,
        )
    else:
        logger.info("Startup recovery: no incomplete WorkflowRuns found")

    stop_event = asyncio.Event()

    scheduler_task = asyncio.create_task(
        run_polling_loop(SessionLocal, stop_event=stop_event)
    )
    logger.info("Scheduler polling loop started")

    dispatch_task = asyncio.create_task(
        dispatch_loop(SessionLocal, stop_event=stop_event)
    )
    logger.info("Dispatch loop started")

    # A real pool, not one worker -- each is its own asyncio task, each
    # opening/closing its own DB session per cycle (session_factory is
    # called fresh every cycle, inside _worker_cycle_sync), so sharing
    # SessionLocal across workers is safe -- no session is ever held
    # across an await point or shared between tasks.
    worker_tasks = [
        asyncio.create_task(worker_loop(SessionLocal, stop_event=stop_event))
        for _ in range(DEFAULT_WORKER_POOL_SIZE)
    ]
    logger.info("Worker pool started (%d workers)", DEFAULT_WORKER_POOL_SIZE)

    yield

    stop_event.set()

    # gather(..., return_exceptions=True) rather than sequential awaits:
    # one task raising during shutdown must not prevent the others from
    # being awaited and logged. Sequential awaits would mean a single
    # failing task skips cleanup of everything listed after it -- exactly
    # the kind of shutdown fragility that turned one worker's Redis
    # hiccup into "Application shutdown failed. Exiting." instead of a
    # clean stop.
    all_tasks = [scheduler_task, dispatch_task, *worker_tasks]
    results = await asyncio.gather(*all_tasks, return_exceptions=True)
    for task, result in zip(all_tasks, results):
        if isinstance(result, Exception):
            logger.error("Task %s raised during shutdown: %r", task.get_name(), result)

    logger.info("Scheduler, dispatch loop, and worker pool stopped")


app = FastAPI(title="Workflow Orchestrator API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(workflows.router)
app.include_router(runs.router)
app.include_router(predictions.router)


@app.get("/health")
def health():
    return {"status": "ok"}
