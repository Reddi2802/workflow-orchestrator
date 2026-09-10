# app/main.py
"""
FastAPI app entrypoint.

Combines two independently-built pieces:
- Paramash's Week 1 CRUD routers (workflows, runs)
- Hridhayansh's Week 1 polling loop, launched as a background asyncio
  task via the lifespan hook, per the "engine runs in-process" decision.

If either of you adds new routers or new startup/shutdown behavior later,
it goes in this same file — don't let a second version of this file get
created independently again.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import runs, workflows
from app.core.database import SessionLocal
from app.engine.scheduler import run_polling_loop

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_event = asyncio.Event()
    polling_task = asyncio.create_task(
        run_polling_loop(SessionLocal, stop_event=stop_event)
    )
    logger.info("Polling loop started")

    yield

    stop_event.set()
    await polling_task
    logger.info("Polling loop stopped cleanly")


app = FastAPI(title="Workflow Orchestrator API", version="0.1.0", lifespan=lifespan)

app.include_router(workflows.router)
app.include_router(runs.router)


@app.get("/health")
def health():
    return {"status": "ok"}
