# Known Limitations

Tracking doc for gaps that are known, deliberate, or deferred — so they don't get
silently rediscovered later. Add to this file rather than letting a gap live only
in a PR description or a chat thread.

## 1. `max_active_runs` — real-completion path untested

**Status:** resolved (Week 4).

Previously verified only against a manually-inserted `RUNNING` row. The Week 4
worker pool (`app/engine/worker_pool.py`) is what actually moves a
`WorkflowRun` out of `RUNNING` for real — `_maybe_finalize_workflow_run`
sets `SUCCESS`/`FAILED`/`PARTIAL` once every task in the run has reached a
terminal status. `test_max_active_runs_allows_new_run_after_completion`
(in `tests/test_worker_pool.py`) starts a run, lets the worker pool
actually complete it, and confirms `_active_run_count` drops to 0
afterward — i.e. a second run is no longer blocked. Passing against real
Docker Postgres + Redis, not a stub.

## 2. Retry strategy has no persisted column

**Status:** open, needs a decision before Week 4's worker pool is built.

`Task` has `max_retries` and `retry_backoff_seconds`, but no `retry_strategy`
column. `next_backoff_seconds(attempt_number, base_backoff, strategy)` takes a
`strategy` argument (`"fixed"` / `"exponential"`) with nowhere in the schema to
store which one applies to a given task.

**Interim decision:** a single global default (`RetryStrategy.EXPONENTIAL`,
see `app/engine/retry_policy.py`) is hardcoded for all tasks project-wide. The
function itself stays strategy-parameterized so both strategies remain testable
independently.

**Resolves:** if per-task strategy selection becomes a requirement, this needs a
migration adding `Task.retry_strategy` and a corresponding update to the
`POST /api/v1/workflows` schema — confirm with Paramash before adding, since it
touches the shared `Task` table shape.

## 3. `ruff` added but not enforced

**Status:** open, low priority.

`ruff` was added as a dev dependency in PR #7. It is not wired into CI or a
pre-commit hook. Do not assume either side of the codebase is currently being
linted automatically.

## 4. Diagrams not started

**Status:** open, scheduled Week 9.

ER diagram, `Task`/`WorkflowRun` state machine diagrams, and UML have not been
started. The state machine diagram should reflect the transition table in
`app/engine/retry_policy.py` (`VALID_TRANSITIONS`) once that lands, rather than
being drawn independently and risking drift.

## 5. CI configuration unscheduled

**Status:** open, deliberately deferred.

No CI pipeline exists yet. Real but lower priority than the modules above;
tentatively slotted into Week 9 if there's slack, per the original plan.

## 6. `dequeue_task` raises instead of returning `None` on timeout

**Status:** resolved.

Fixed in `fix/dequeue-task-timeout` (merged, PR #14) — `dequeue_task` now
catches `redis.exceptions.TimeoutError` internally and returns `None`,
matching its own documented contract. The Week 4 workaround in
`worker_pool.py` (`_worker_cycle_sync` catching the same exception at the
call site) is now redundant defense-in-depth rather than the only fix —
harmless to leave in place, since a well-behaved `dequeue_task` simply
means that except branch never fires.

## 7. Week 5 crash recovery — scope decisions (made without Hridhayansh present; reviewed since, see item 10)

**Status:** implemented and verified. `tests/test_recovery.py` (8 tests)
and the full suite (112 tests) pass against real Postgres + Redis. A manual
crash test on 2026-10-14 (server hard-killed while a task was `RUNNING`,
then restarted) re-enqueued the task and completed it with `attempt_number`
unchanged. Only scenario A was exercised live; scenarios B–F are covered by
tests only. See `app/engine/recovery.py`.

These decisions were originally made unilaterally while Hridhayansh was
offline for ~5 hours.

Four open questions from planning, and the call made on each:

1. **Which crash scenarios to handle** (the DB write and the Redis
   `enqueue_task` call are two separate operations — a crash can land
   between them in several distinct places, not just one). Decision:
   handle all of them — a `TaskRun` stuck `RUNNING`, a completed/failed
   task whose downstream propagation never ran, a `RETRYING` task whose
   next-attempt row was never created, and a `PENDING` task that may
   never have been enqueued. Full detail and reasoning for each is in
   `recovery.py`'s module docstring (scenarios A–F) — read that before
   relying on this summary.
2. **Whether `checkpoint_data` needs richer content than the Week 4
   completion breadcrumb.** Decision: no — recovery only needs a binary
   "did this complete or not" signal, and nothing in the schema
   (`Task.command` is a single opaque shell string, no inter-task data
   passing) suggests a need for more.
3. **Where recovery runs, relative to the worker pool starting.**
   Decision: blocking, synchronous, at startup, in `main.py`'s lifespan,
   *before* `dispatch_loop`/`worker_loop` tasks are created — removes the
   race between recovery deciding what to re-enqueue and a worker
   already dequeuing, rather than relying on dedup guards to absorb it.
4. **Whether to track live subprocesses to detect a truly-orphaned
   process vs. a dead one.** Decision: out of scope — see item 8 below.

**Also a real design decision inside the implementation, not just the
four above:** recovering a `RUNNING` `TaskRun` resets it to `PENDING` at
the *same* `attempt_number`, not a new attempt row — a crash isn't the
task's own failure, and charging it against `max_retries` would be
wrong. This requires bypassing `retry_policy.validate_transition`
(`RUNNING → PENDING` isn't legal in normal operation) — done
deliberately, with it being the one and only place in the codebase this
happens, not a general workaround.

## 8. Crash recovery cannot distinguish a dead subprocess from an orphaned one

**Status:** open, deliberately out of scope.

`process_task_run` calls `subprocess.run()` synchronously. If the worker
thread dies but the OS process hosting the whole app is killed outright
(not just the Python thread), a child process's exact fate depends on the
OS and how it was launched — it could still be running, disconnected from
the `TaskRun` row that recovery will now re-enqueue and re-execute.
Recovery has no PID tracking and treats any `RUNNING` row as simply
incomplete, re-running its command regardless. For a non-idempotent
command, this could cause double execution in a true crash.

**Resolves:** real process-group/PID tracking (what Airflow itself does)
is judged out of scope for a 3-month, 2-person capstone. If this becomes
a real requirement, it needs its own design pass, not a quick patch onto
`recovery.py`.

## 9. Recovery's handling of a `PENDING` task can occasionally double-enqueue

**Status:** open, accepted trade-off, not a bug to silently fix later
without reading this first.

Recovery cannot tell "this `PENDING` row was never enqueued" apart from
"this row was enqueued, and Redis — a separate process from the crashed
app — still has it safely sitting in the queue." It re-enqueues every
`PENDING` row unconditionally. If the row actually was already queued,
this produces a genuine duplicate `task_run_id` in the queue.

**Why this is accepted rather than fixed:** in the common case, the second
dequeue of the same `task_run_id` happens after the first worker has already
committed `RUNNING`, so `process_task_run`'s `validate_transition` call
against the row's current status is an invalid transition, which raises,
which the worker's own `try/except` catches and logs. The outcome is one
wasted queue slot and a log line, not data corruption. Building real
queue-level idempotency (dedup keys, etc.) to close this entirely is judged
beyond this project's scope.

**Known hole in that reasoning (found 2026-10-15):** `process_task_run`
reads the row with `db.get` (no row lock) and the status check and the
`RUNNING` commit are not atomic. With two workers, two copies of the same
`task_run_id` dequeued at nearly the same moment can both pass the check
while the row is still `PENDING`, and the command then executes twice. This
is narrow but real. The fix is a row lock (`SELECT ... FOR UPDATE`) or a
conditional `UPDATE ... WHERE status = 'PENDING'` inside `process_task_run`
— a change to already-merged Week 4 code, deliberately not attempted in the
recovery PR.

## 10. Recovery decisions — review outcome

**Status:** open until decision B below is made.

Reviewed 2026-10-15, after the full suite and the manual crash test passed.
The four review items and their outcome:

- **A — Recovery is not atomic.** It commits per task while walking the DAG,
  so a second crash mid-recovery can leave work for the next restart.
  **Accepted:** the propagation helpers are idempotent, so a repeat crash
  self-heals on the following startup.
- **B — No audit trail for recovered rows.** Nothing on a `TaskRun`
  distinguishes a recovered row from a normally-processed one; the startup
  log line is the only record. **Skipped for now.** Revisit in Week 6 only
  if the predictor uses run-level durations; cheaper first option is
  outlier filtering during dataset prep, then a `recovery_count` column if
  that proves insufficient.
- **C — The `RUNNING → PENDING` validator bypass.** **Kept.** A new attempt
  row would charge a retry for a failure that wasn't the task's own.
  Exercised live in the 2026-10-14 manual crash test (`attempt_number`
  stayed 1). Still the one deliberate break of the Week 3 transition rules;
  reviewers should read its justification in `recovery.py`.
- **D — Possible double-enqueue of a `PENDING` task.** **Accepted** — see
  item 9, including its known hole.

## 11. Graceful shutdown fails in-flight tasks instead of recovering them

**Status:** open, documented, not planned for Week 5/6.

Crash recovery covers hard process death only (power loss, OOM kill,
`kill -9`, `Stop-Process -Force`). On a graceful stop (Ctrl+C), the
interrupt also reaches the running task's subprocess, which exits
nonzero. The worker records that as an ordinary task failure and the run
finishes `FAILED` before the app exits, so nothing is left `RUNNING` for
recovery to find. Observed 2026-10-14 with `max_retries=0`: run 144 ended
`failed` with the subprocess's `KeyboardInterrupt` traceback as its
`error_message`. Behavior with `max_retries > 0` was not tested.

Net effect: restarting the app via Ctrl+C (or a redeploy that stops it the
same way) fails in-flight work rather than resuming it.

**Testing consequence:** to test crash recovery, start the app without
`--reload` and hard-kill the server process (`Stop-Process -Id <pid>
-Force` on Windows). Ctrl+C will not produce a recoverable state.

**Resolves:** would need shutdown-aware handling in the worker (detect
shutdown, leave the row `RUNNING` or reset it, and don't record the
child's interrupt as a task failure). Needs its own design pass.
