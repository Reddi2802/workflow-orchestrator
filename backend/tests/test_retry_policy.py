"""
tests/test_retry_policy.py

Table tests for should_retry, next_backoff_seconds, and the TaskStatus
transition validator. No DB, no Redis -- this module has zero external
dependencies by design (see Week_3_Plan.md).
"""

import pytest

from app.engine.retry_policy import (
    RetryStrategy,
    is_valid_transition,
    next_backoff_seconds,
    should_retry,
    validate_transition,
)
from app.models.enums import TaskStatus


# --- Fakes -------------------------------------------------------------------
# Minimal stand-ins so this module doesn't need a DB session. Swap for real
# model instances if/when these tests move to run against the fixture DB.

class FakeTask:
    def __init__(self, max_retries: int):
        self.max_retries = max_retries


class FakeTaskRun:
    def __init__(self, status: TaskStatus, attempt_number: int):
        self.status = status
        self.attempt_number = attempt_number


# --- should_retry --------------------------------------------------------

@pytest.mark.parametrize(
    "max_retries,status,attempt_number,expected",
    [
        # max_retries=0: first attempt fails -> no retry
        (0, TaskStatus.FAILED, 1, False),
        # max_retries=2: attempts 1 and 2 fail -> retry; attempt 3 fails -> stop
        (2, TaskStatus.FAILED, 1, True),
        (2, TaskStatus.FAILED, 2, True),
        (2, TaskStatus.FAILED, 3, False),
        # status must gate the decision, not just attempt_number vs max_retries
        (5, TaskStatus.SUCCESS, 1, False),
        (5, TaskStatus.SKIPPED, 1, False),
        (5, TaskStatus.RETRYING, 1, False),
        (5, TaskStatus.RUNNING, 1, False),
        (5, TaskStatus.PENDING, 1, False),
    ],
)
def test_should_retry(max_retries, status, attempt_number, expected):
    task = FakeTask(max_retries=max_retries)
    task_run = FakeTaskRun(status=status, attempt_number=attempt_number)
    assert should_retry(task, task_run) is expected


# --- next_backoff_seconds -------------------------------------------------

@pytest.mark.parametrize(
    "attempt_number,base_backoff,strategy,expected",
    [
        # fixed: constant regardless of attempt number
        (1, 30, RetryStrategy.FIXED, 30),
        (2, 30, RetryStrategy.FIXED, 30),
        (5, 30, RetryStrategy.FIXED, 30),
        # exponential: base * 2^(attempt-1), hand-computed
        (1, 30, RetryStrategy.EXPONENTIAL, 30),    # 30 * 2^0
        (2, 30, RetryStrategy.EXPONENTIAL, 60),    # 30 * 2^1
        (3, 30, RetryStrategy.EXPONENTIAL, 120),   # 30 * 2^2
        (4, 30, RetryStrategy.EXPONENTIAL, 240),   # 30 * 2^3
        # accepts plain strings matching the enum values too
        (2, 10, "fixed", 10),
        (2, 10, "exponential", 20),
    ],
)
def test_next_backoff_seconds(attempt_number, base_backoff, strategy, expected):
    assert next_backoff_seconds(attempt_number, base_backoff, strategy) == expected


def test_next_backoff_seconds_rejects_unknown_strategy():
    with pytest.raises(ValueError):
        next_backoff_seconds(1, 30, "linear")  # typo / unsupported strategy


def test_next_backoff_seconds_rejects_invalid_attempt_number():
    with pytest.raises(ValueError):
        next_backoff_seconds(0, 30, RetryStrategy.FIXED)


def test_next_backoff_seconds_rejects_negative_base_backoff():
    with pytest.raises(ValueError):
        next_backoff_seconds(1, -1, RetryStrategy.FIXED)


# --- TaskStatus transition validator ---------------------------------------

VALID_CASES = [
    (TaskStatus.PENDING, TaskStatus.RUNNING),
    (TaskStatus.PENDING, TaskStatus.SKIPPED),
    (TaskStatus.RUNNING, TaskStatus.SUCCESS),
    (TaskStatus.RUNNING, TaskStatus.FAILED),
    (TaskStatus.RUNNING, TaskStatus.RETRYING),
]

# Every other (from, to) pair across all six statuses is invalid. Built
# explicitly rather than "assume everything not listed is invalid" so the
# test data itself documents the full state space, per the plan's
# instruction to test every invalid transition, not just the one spec example.
ALL_STATUSES = list(TaskStatus)
INVALID_CASES = [
    (frm, to)
    for frm in ALL_STATUSES
    for to in ALL_STATUSES
    if (frm, to) not in VALID_CASES
]


@pytest.mark.parametrize("current,target", VALID_CASES)
def test_valid_transitions_allowed(current, target):
    assert is_valid_transition(current, target) is True
    validate_transition(current, target)  # must not raise


@pytest.mark.parametrize("current,target", INVALID_CASES)
def test_invalid_transitions_rejected(current, target):
    assert is_valid_transition(current, target) is False
    with pytest.raises(ValueError):
        validate_transition(current, target)


def test_success_to_pending_explicitly_rejected():
    # The one example given in the spec -- kept as an explicit standalone
    # test even though it's also covered by the parametrized sweep above,
    # so a future refactor of INVALID_CASES can't accidentally drop it.
    assert is_valid_transition(TaskStatus.SUCCESS, TaskStatus.PENDING) is False


def test_terminal_statuses_have_no_outgoing_transitions():
    for status in (
        TaskStatus.SUCCESS,
        TaskStatus.FAILED,
        TaskStatus.RETRYING,
        TaskStatus.SKIPPED,
    ):
        for target in ALL_STATUSES:
            assert is_valid_transition(status, target) is False
