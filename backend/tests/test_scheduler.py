# tests/test_scheduler.py
"""
next_run_time: unit tested here against hand-verified expected times, per
Week 1 self-test requirement — this is a pure function, no DB needed.

get_due_workflows / poll_and_trigger / run_polling_loop are NOT unit
tested here — they need a real Postgres session (they query Workflow and
WorkflowRun rows). Those belong in an integration test using the real
Docker Postgres container, not mocked out here. Flagging this gap rather
than faking a DB session just to hit a coverage number.
"""
from datetime import datetime

from app.engine.scheduler import next_run_time


def test_next_run_time_daily_at_midnight():
    # "0 0 * * *" = every day at midnight
    base = datetime(2026, 1, 1, 10, 30, 0)  # Jan 1, 10:30am
    result = next_run_time("0 0 * * *", base)
    assert result == datetime(2026, 1, 2, 0, 0, 0)  # next midnight is Jan 2


def test_next_run_time_every_5_minutes():
    # "*/5 * * * *" = every 5 minutes
    base = datetime(2026, 1, 1, 10, 32, 0)
    result = next_run_time("*/5 * * * *", base)
    assert result == datetime(2026, 1, 1, 10, 35, 0)


def test_next_run_time_weekly_on_monday():
    # "0 9 * * MON" = every Monday at 9am
    # 2026-01-01 is a Thursday
    base = datetime(2026, 1, 1, 9, 0, 0)
    result = next_run_time("0 9 * * MON", base)
    assert result == datetime(2026, 1, 5, 9, 0, 0)  # following Monday
