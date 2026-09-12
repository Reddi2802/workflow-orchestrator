# tests/test_scheduler_unit.py
"""
Pure unit tests for scheduler functions that don't touch the database —
next_run_time() takes only a cron expression and a datetime, so it's
tested here without a Postgres session, unlike test_scheduler_integration.py
which specifically needs the real DB.
"""
from datetime import datetime

from app.engine.scheduler import next_run_time


def test_next_run_time_every_minute():
    # Baseline case already implicitly covered by the integration test,
    # made explicit and deterministic here (no wall-clock dependency).
    base = datetime(2026, 9, 12, 4, 9, 41)
    result = next_run_time("* * * * *", base)
    assert result == datetime(2026, 9, 12, 4, 10, 0)


def test_next_run_time_five_minute_interval():
    # This is the case that wasn't covered before. */5 fires on wall-clock
    # minute marks divisible by 5 (:00, :05, :10...) — NOT "5 minutes after
    # base_time". base=04:12:43 isn't on a boundary, so the next fire is
    # the next boundary after it: 04:15:00, not 04:17:43.
    base = datetime(2026, 9, 12, 4, 12, 43)
    result = next_run_time("*/5 * * * *", base)
    assert result == datetime(2026, 9, 12, 4, 15, 0)


def test_next_run_time_five_minute_interval_exact_boundary():
    # Edge case: what happens when base_time IS already exactly on a
    # 5-minute mark? croniter.get_next() returns the NEXT fire time
    # strictly after base, not base itself — so 04:15:00 in should give
    # 04:20:00 out, not 04:15:00 again. This matters for get_due_workflows:
    # if a WorkflowRun's triggered_at lands exactly on a boundary and that
    # gets used as the next base_time, this confirms it won't re-fire
    # immediately on the same instant.
    base = datetime(2026, 9, 12, 4, 15, 0)
    result = next_run_time("*/5 * * * *", base)
    assert result == datetime(2026, 9, 12, 4, 20, 0)