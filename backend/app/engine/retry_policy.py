"""
app/engine/retry_policy.py

Retry decision logic and TaskStatus transition validation.

Design note on RETRYING (read before modifying VALID_TRANSITIONS):
TaskRun has a unique constraint on (workflow_run_id, task_id, attempt_number).
That constraint only makes sense if each retry attempt is a NEW row, not a
mutated one. Consequently RETRYING is a *terminal* status for a given TaskRun
row -- it means "this attempt failed but another attempt (a new row, new
attempt_number) will be created", as opposed to FAILED, which means "this
attempt failed and no further attempt will be made." Do not add outgoing
transitions from RETRYING; the "next attempt" is a different row starting at
PENDING, not this row changing state again.
"""

from __future__ import annotations

import enum

from app.models.enums import TaskStatus
from app.models.entities import Task, TaskRun


class RetryStrategy(str, enum.Enum):
    FIXED = "fixed"
    EXPONENTIAL = "exponential"


# Interim project-wide default. See known_limitations.md #2: Task has no
# retry_strategy column yet, so this is a single global choice rather than
# a per-task one. Revisit if/when that column gets added.
DEFAULT_RETRY_STRATEGY = RetryStrategy.EXPONENTIAL


# --- TaskStatus transition graph -------------------------------------------

VALID_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.SKIPPED},
    TaskStatus.RUNNING: {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.RETRYING},
    TaskStatus.SUCCESS: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.RETRYING: set(),
    TaskStatus.SKIPPED: set(),
}


def is_valid_transition(current: TaskStatus, target: TaskStatus) -> bool:
    """Return True iff moving a TaskRun from `current` to `target` is allowed."""
    return target in VALID_TRANSITIONS.get(current, set())


def validate_transition(current: TaskStatus, target: TaskStatus) -> None:
    """Raise ValueError if the transition is invalid. Use at call sites that
    mutate TaskRun.status, so an illegal transition fails loudly instead of
    silently corrupting state."""
    if not is_valid_transition(current, target):
        raise ValueError(
            f"Invalid TaskStatus transition: {current.value} -> {target.value}"
        )


# --- Retry decision ----------------------------------------------------------

def should_retry(task: Task, task_run: TaskRun) -> bool:
    """
    Decide whether a failed TaskRun should get another attempt.

    Status matters, not just the attempt count: this only returns True for a
    TaskRun that is actually in FAILED. A TaskRun that's SUCCESS, SKIPPED, or
    already RETRYING has no business being reconsidered here -- calling this
    on anything but FAILED is a caller bug, and returning False (rather than
    raising) keeps it a safe no-op for a worker pool that calls this
    defensively on every terminal status.
    """
    if task_run.status != TaskStatus.FAILED:
        return False
    return task_run.attempt_number <= task.max_retries


def next_backoff_seconds(
    attempt_number: int,
    base_backoff: int,
    strategy: RetryStrategy | str = DEFAULT_RETRY_STRATEGY,
) -> int:
    """
    Seconds to wait before the next attempt.

    attempt_number is the attempt that just failed (1-indexed, matching
    TaskRun.attempt_number). base_backoff is Task.retry_backoff_seconds.
    """
    if attempt_number < 1:
        raise ValueError("attempt_number must be >= 1")
    if base_backoff < 0:
        raise ValueError("base_backoff must be >= 0")

    strategy = RetryStrategy(strategy)  # raises ValueError on an unknown string

    if strategy is RetryStrategy.FIXED:
        return base_backoff
    if strategy is RetryStrategy.EXPONENTIAL:
        return base_backoff * (2 ** (attempt_number - 1))

    # Unreachable: RetryStrategy(strategy) above would already have raised.
    raise ValueError(f"Unhandled retry strategy: {strategy}")
