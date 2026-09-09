from fastapi import FastAPI

from app.api.v1 import runs, workflows

app = FastAPI(title="Workflow Orchestrator API", version="0.1.0")

app.include_router(workflows.router)
app.include_router(runs.router)


@app.get("/health")
def health():
    return {"status": "ok"}
