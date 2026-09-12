# Known Limitations

Tracking doc for gaps that are known, deliberate, or deferred — so they don't get
silently rediscovered later. Add to this file rather than letting a gap live only
in a PR description or a chat thread.

## 1. `max_active_runs` — real-completion path untested

**Status:** open, blocked on Week 4.

`max_active_runs` skip logic is verified against a manually-inserted `RUNNING` row
(unit tests + live verification against real data). It has **not** been verified
against a run that transitions out of `RUNNING` on its own, because nothing moves
a `WorkflowRun` out of `RUNNING` until the Week 4 worker pool exists.

**Resolves:** once the worker pool (Week 4) can actually complete a run, add a test
that starts a run, lets it complete, and confirms a second scheduled run is no
longer skipped.

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
