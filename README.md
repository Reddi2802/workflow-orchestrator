# Workflow Orchestrator

A distributed workflow orchestration engine with DAG-based task scheduling, retry handling, crash recovery, and ML-driven bottleneck prediction.

## Stack

- **Backend:** FastAPI, SQLAlchemy, Alembic, PostgreSQL, Redis
- **Frontend:** React
- **ML:** scikit-learn (bottleneck prediction; anomaly detection and retry recommendation are stretch goals)
- **Package management:** `uv` (Python), `npm` (frontend)

## Prerequisites

Install these before doing anything else:

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) — confirm it's running before the next step
- [uv](https://astral.sh/uv) — Python package/env manager
  - Windows (PowerShell): see install instructions at astral.sh/uv
  - Mac/Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- [GitHub CLI (`gh`)](https://cli.github.com/) — Windows: `winget install --id GitHub.cli`
- Git, obviously

## First-time setup

1. **Clone the repo**
   ```bash
   git clone https://github.com/Reddi2802/workflow-orchestrator.git
   cd workflow-orchestrator
   ```

2. **Start infrastructure** (Postgres on 5432, Redis on 6379)
   ```bash
   docker compose up -d
   ```

3. **Verify both containers are actually healthy** — don't trust `docker compose ps` alone, confirm real connectivity:
   ```bash
   docker exec -it workflow-orchestrator-postgres-1 psql -U orchestrator -d orchestrator -c "SELECT 1;"
   docker exec -it workflow-orchestrator-redis-1 redis-cli PING
   ```
   Postgres should return a `1` row. Redis should return `PONG`. If either fails, stop here and debug before continuing — nothing downstream works without both.

4. **Set up the Python environment**
   ```bash
   cd backend
   uv sync
   ```
   Confirm it resolved cleanly:
   ```bash
   uv run python -c "import fastapi, sqlalchemy, alembic; print('ok')"
   ```

5. **Authenticate GitHub CLI** (needed for the PR workflow below)
   ```bash
   gh auth login
   ```

> **Status: STEP 4 REQUIRES `backend/pyproject.toml` AND `uv.lock` TO BE COMMITTED FIRST.**
> If `uv sync` has nothing to sync against, that file hasn't been pushed yet — check with the repo owner before assuming your setup is broken.

## Branching & PR workflow

`main` is protected: 1 approving review required, enforced even for repo admins. Direct pushes to `main` will be rejected — this is intentional, not a bug.

For every change, including small infra tweaks:
```bash
git checkout main
git pull
git checkout -b <type>/<short-description>    # e.g. feature/dag-cycle-detection
# make changes
git add .
git commit -m "clear description of what changed"
git push -u origin <branch-name>
gh pr create --title "..." --body "..." --base main
```
Then get the other person to review and approve on GitHub before merging. Don't self-approve — the point of the review is that someone other than the author reads the change.

Branch per feature/task, not per person. Both of you touch shared entities (Workflow, Task, TaskRun, etc.) constantly — long-lived personal branches cause exactly the merge conflicts this workflow exists to avoid.

## Project structure

```
workflow-orchestrator/
├── backend/
│   ├── app/
│   │   ├── api/          # FastAPI routers
│   │   ├── core/         # config, DB session
│   │   ├── models/       # SQLAlchemy models
│   │   ├── schemas/      # Pydantic schemas
│   │   ├── engine/       # scheduler, DAG resolver, worker pool
│   │   └── main.py
│   ├── tests/
│   └── alembic/           # DB migrations
├── frontend/               # TBD — not yet scaffolded
├── docs/                    # ER diagrams, state machines, UML
├── docker-compose.yml
└── .gitignore
```

## Current status

- [x] Repo created, branch protection enabled, `.gitignore` in place
- [x] Docker infra (Postgres + Redis) verified working
- [x] `backend/pyproject.toml` + `uv.lock` committed
- [ ] Entity models (Workflow, Task, TaskDependency, WorkflowRun, TaskRun)
- [ ] Alembic initial migration
- [ ] Frontend scaffold
- [ ] CI config
