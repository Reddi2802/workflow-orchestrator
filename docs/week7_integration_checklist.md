# Week 7: joint integration checklist

Goal: one demonstrable pass of the whole pipeline (UI -> API -> engine -> queue
-> database -> UI), with every bug found written down. Bugs only, no new
features: Week 10 is the only buffer.

Fill in the boxes and the bug table as you go. Commit it with the results.

## 0. Before the session (each person, alone, about 30 minutes)

- [ ] `git checkout main && git pull`, and both of you are on the same commit: `git log --oneline -1` = ____________
- [ ] `docker compose up -d`
- [ ] `cd backend; uv sync; uv run alembic upgrade head` (needed for `TaskRun.created_at` and pandas)
- [ ] `uv run pytest -q` passes. Count: ______ (Paramash: only if no live server is running, the tests wipe the database and the queue)
- [ ] `cd frontend; npm ci; npm test` passes. Count: ______
- [ ] Server starts without `--reload`: `uv run uvicorn app.main:app --port 8000`

## 1. Automated checks (Hridhayansh, about 10 minutes)

With the server running (second terminal, from `backend/`):

```
uv run python scripts/e2e_smoke.py
```

- [ ] diamond  - [ ] failure_skip  - [ ] retry_success  - [ ] max_active_runs  - [ ] empty_workflow  - [ ] cron

Also run once with the server stopped: `uv run pytest tests/test_e2e_pipeline.py -q` (5 tests, about 20 s).

## 2. Contract check (together, 30 minutes)

- [ ] Frontend handles every task status: pending, running, success, failed, retrying, skipped
- [ ] Frontend handles every run status: pending, running, success, failed, partial
- [ ] A 400 (workflow has no tasks) shows a readable message in the UI
- [ ] A 409 (max_active_runs reached) shows a readable message in the UI
- [ ] Decision on known_limitations item 14: expose `created_at` in `TaskRunRead`? ______________

## 3. UI against the real engine (together, about 2 hours)

Create each shape from the UI (or `scripts/generate_timing_data.py`) and trigger it:

- [ ] chain  - [ ] diamond  - [ ] fan-out  - [ ] fan-in: the DAG graph has the right shape
- [ ] Run history: status colours and ordering are right (newest first)
- [ ] A running workflow updates without a manual refresh? If not, is that acceptable? ______________
- [ ] A failing task shows as failed, its downstream as skipped, the run as partial
- [ ] A retry shows both attempts
- [ ] Soak test: start the dashboard, then run `uv run python scripts/generate_timing_data.py` (about 15 to 20 minutes) and watch the UI stay correct while about 480 tasks run

## 4. Live crash recovery (together, 30 minutes)

Only scenario A has been exercised live outside the script; B to F are covered by tests only.

- [ ] `uv run python scripts/e2e_smoke.py --only crash`, then do what it prints: hard-kill the server while the task is RUNNING, restart it
- [ ] The dashboard shows the run finishing, the task has `attempt_number` 1, no extra attempt row
- [ ] Use `Stop-Process -Force`, not Ctrl+C (Ctrl+C fails the task, known_limitations item 11)

## 5. Decisions to make before Week 8 (together, 30 minutes)

- [ ] Prediction target: execution duration, queue wait, or end-to-end latency? ______________ (see known_limitations item 13: queue wait is as large as execution time)
- [ ] Baseline the model must beat: predict the task's previous mean duration (`prev_mean_duration`). Agreed? ____
- [ ] Split: by time, never random. Agreed? ____
- [ ] Prediction API: path and response shape: ______________________
- [ ] Who shows predictions in the dashboard, and in which week? ______________ (nobody owns this in the current plan)

## Bug log

| # | Where (UI / API / engine) | What happened | Severity (blocks demo / annoying / cosmetic) | Owner | Fixed in PR |
|---|---|---|---|---|---|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |

Anything not fixed this week: copy it into `docs/known_limitations.md` as a new
numbered item so it is not rediscovered later.

## Done when

- [ ] Section 1 and the crash scenario in section 4 pass against the live stack
- [ ] The UI shows correct state for every status in section 2
- [ ] Every bug found is in the table, and the ones that block the demo are fixed
- [ ] The decisions in section 5 are filled in
