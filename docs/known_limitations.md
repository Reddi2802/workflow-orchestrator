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

**Status:** open, needs a fix from Paramash (owns `app/queue/redis_queue.py`).

`dequeue_task`'s own docstring promises it never raises on a plain timeout,
but under live testing it does: `redis_client.brpop(...)` intermittently
(in practice, on essentially every empty-queue cycle) raises
`redis.exceptions.TimeoutError` instead of returning `None`. Likely cause:
the client's own socket-level read timeout racing the server-side `BRPOP`
timeout, possibly related to RESP3 protocol handling in this redis-py
version — root cause not fully confirmed.

**Interim mitigation (Week 4, `app/engine/worker_pool.py`):**
`_worker_cycle_sync` catches `redis.exceptions.TimeoutError` specifically
and treats it as an empty cycle, since that's functionally what it is from
the caller's side. This stops it from crashing a worker or flooding logs,
but it's a workaround at the call site, not a fix to the actual function.

**Resolves:** `dequeue_task` should catch `redis.exceptions.TimeoutError`
internally and return `None`, matching its own documented contract:

```python
def dequeue_task(timeout_seconds: int) -> int | None:
    try:
        result = redis_client.brpop(TASK_QUEUE_KEY, timeout=timeout_seconds)
    except redis.exceptions.TimeoutError:
        return None
    if result is None:
        return None
    _, value = result
    return int(value)
```

Needs its own branch/PR reviewed by Hridhayansh, same as any other change
to this file — not bundled into the Week 4 worker pool PR, since it's a
fix to already-merged Week 3 code owned by Paramash, not new Week 4 work.
