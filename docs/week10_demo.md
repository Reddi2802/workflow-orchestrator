# Week 10: demo script and freeze checklist

Rule for this week: bug fixes only, no new features. Every change still goes through branch, PR, review.

## Before the demo (do once, on the demo machine)

```powershell
docker compose up -d
cd backend
uv sync
uv run alembic upgrade head
uv run python scripts/generate_timing_data.py --workflows 20   # only if no ds- data yet; takes a while
uv run python scripts/train_predictor.py
```
```powershell
cd ..\frontend
npm ci
```

Two terminals, left running: `uv run uvicorn app.main:app --port 8000` (in `backend`, no `--reload`) and `npm run dev` (in `frontend`).
Do NOT run `pytest` while these are up: the tests wipe every workflow and the Redis queue.

## The 8-minute run

| # | Show | Do | What to say / point at |
|---|---|---|---|
| 1 | Architecture | open `docs/diagrams.md` (component + sequence diagram) | one process; scheduler, dispatch, 2 workers; Redis decouples scheduling from execution |
| 2 | DAG validation | `POST /api/v1/workflows` with a cycle (A->B, B->A) | 400, nothing written to the DB |
| 3 | Happy path | `uv run python scripts/demo_seed.py` | dashboard: `demo-happy` diamond runs B and C only after A, D after both |
| 4 | Retries | open `demo-retry` run | 3 attempts (retrying, retrying, failed), downstream skipped, run PARTIAL |
| 5 | Partial failure | open `demo-partial` | one branch succeeded, one failed, run PARTIAL |
| 6 | Crash recovery | see below | killed mid-run, restarted, finishes without redoing finished tasks |
| 7 | Predictor | `curl.exe localhost:8000/api/v1/workflows/<id>/predictions` | per-task estimate, critical path, `likely_long`; first-time tasks vs history |
| 8 | Tests | `uv run pytest -q --cov=app` | 188 tests, ~95% coverage, CI on every PR |

### Crash recovery demo (step 6)

Start a slow run, kill the server hard, restart, watch it resume.

```powershell
# create a workflow with a 20s task and trigger it (replace the id)
curl.exe -X POST localhost:8000/api/v1/workflows/<id>/runs
# while a task is RUNNING, hard-kill the server (Ctrl+C is NOT a crash: it fails the task)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ? CommandLine -match 'uvicorn' | % { Stop-Process -Id $_.ProcessId -Force }
# restart; the log line "Startup recovery: ... reconciled" shows which run was picked up
uv run uvicorn app.main:app --port 8000
```
Known catch (`docs/known_limitations.md`, item 8): the killed task's subprocess can keep running and
the re-run starts a second copy. Say so if asked; do not demo with a command that has side effects.

## Questions to be ready for

- *Why in-process, not a separate worker service?* No service discovery or deployment coordination for a 2-person, 3-month scope; the cost is that a crash takes the workers down too, which is what recovery covers.
- *Why can't two workers run the same task?* Claim is a conditional `UPDATE ... WHERE status = PENDING`; rowcount 0 means someone else won.
- *Is the predictor any good?* On generated data, only for new tasks; for repeat tasks, "what it took last time" beat the model. Data is synthetic. See `docs/week8_predictor.md`.
- *What breaks?* `docs/known_limitations.md`: say them before they are found.

## Freeze checklist (day before)

- [ ] `main` green in CI; all week PRs merged (each approved by the other person)
- [ ] fresh clone on the demo machine: `docker compose up -d`, `uv sync`, `alembic upgrade head`, `pytest -q` all pass
- [ ] `scripts/demo_seed.py` prints three `[ok]` lines
- [ ] model trained (`backend/models/duration_model.joblib` exists; it is gitignored, so train on the demo machine)
- [ ] dashboard shows the seeded runs; browser at 100% zoom
- [ ] `known_limitations.md` reread; nothing in it will surprise you
- [ ] a screen recording of steps 3-7 as a fallback if something fails live
