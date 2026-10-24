# tests/test_features.py
"""
Tests for app/ml/features.py. Real Postgres via the rolled-back db_session
fixture; every timestamp is set explicitly so expected values are exact and
nothing depends on how fast the test runs.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.ml.features import (
    FEATURE_COLUMNS, ID_COLUMNS, REQUIRED_COLUMNS, TARGET,
    build_dataset, parse_command, time_split,
)
from app.models.entities import Task, TaskDependency, TaskRun, Workflow, WorkflowRun
from app.models.enums import TaskStatus, TriggerType, WorkflowRunStatus

T0 = datetime(2026, 1, 1, 12, 0, 0)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _workflow(db, name="ds-test"):
    wf = Workflow(name=name)
    db.add(wf)
    db.flush()
    return wf


def _task(db, wf, name, command="exit 0", max_retries=0):
    t = Task(workflow_id=wf.id, name=name, command=command, max_retries=max_retries)
    db.add(t)
    db.flush()
    return t


def _dep(db, up, down):
    db.add(TaskDependency(upstream_task_id=up.id, downstream_task_id=down.id))
    db.flush()


def _wf_run(db, wf):
    run = WorkflowRun(
        workflow_id=wf.id, status=WorkflowRunStatus.SUCCESS, trigger_type=TriggerType.MANUAL,
    )
    db.add(run)
    db.flush()
    return run


def _task_run(db, run, task, start, end, status=TaskStatus.SUCCESS, created=None, attempt=1):
    """start/end/created are seconds after T0 (or None)."""
    tr = TaskRun(
        workflow_run_id=run.id, task_id=task.id, status=status, attempt_number=attempt,
        started_at=_at(start) if start is not None else None,
        completed_at=_at(end) if end is not None else None,
        created_at=_at(created) if created is not None else _at(start - 1) if start is not None else None,
    )
    db.add(tr)
    db.flush()
    return tr


def _row(df, task_run_id):
    return df[df["task_run_id"] == task_run_id].iloc[0]


# --- join, filtering ------------------------------------------------------------

def test_join_and_basic_columns(db_session):
    wf = _workflow(db_session)
    a = _task(db_session, wf, "a")
    b = _task(db_session, wf, "b")
    _dep(db_session, a, b)
    run = _wf_run(db_session, wf)
    ra = _task_run(db_session, run, a, 10, 14, created=7)
    rb = _task_run(db_session, run, b, 14, 19.5, created=14)

    df = build_dataset(db_session)

    assert list(df.columns) == ID_COLUMNS + [TARGET, "queue_wait_seconds"] + FEATURE_COLUMNS
    assert len(df) == 2
    ra_row, rb_row = _row(df, ra.id), _row(df, rb.id)

    assert ra_row[TARGET] == pytest.approx(4.0)
    assert rb_row[TARGET] == pytest.approx(5.5)
    assert ra_row["queue_wait_seconds"] == pytest.approx(3.0)
    assert rb_row["queue_wait_seconds"] == pytest.approx(0.0)
    assert ra_row["workflow_id"] == wf.id and ra_row["workflow_run_id"] == run.id
    assert ra_row["task_id"] == a.id

    # DAG shape: a -> b
    assert (ra_row["dag_depth"], ra_row["n_upstream"], ra_row["n_downstream"]) == (0, 0, 1)
    assert (rb_row["dag_depth"], rb_row["n_upstream"], rb_row["n_downstream"]) == (1, 1, 0)
    assert ra_row["n_tasks_in_workflow"] == 2


def test_diamond_depth_is_longest_path(db_session):
    wf = _workflow(db_session)
    a, b, c, d = (_task(db_session, wf, n) for n in "abcd")
    for up, down in [(a, b), (a, c), (b, d), (c, d)]:
        _dep(db_session, up, down)
    run = _wf_run(db_session, wf)
    rows = {n: _task_run(db_session, run, t, i * 10, i * 10 + 2) for i, (n, t) in enumerate(zip("abcd", (a, b, c, d)))}

    df = build_dataset(db_session)

    assert _row(df, rows["d"].id)["dag_depth"] == 2
    assert _row(df, rows["d"].id)["n_upstream"] == 2
    assert _row(df, rows["a"].id)["n_downstream"] == 2


@pytest.mark.parametrize(
    "status",
    [TaskStatus.FAILED, TaskStatus.SKIPPED, TaskStatus.RETRYING, TaskStatus.PENDING, TaskStatus.RUNNING],
)
def test_non_success_rows_are_excluded(db_session, status):
    wf = _workflow(db_session)
    t = _task(db_session, wf, "t")
    run = _wf_run(db_session, wf)
    good = _task_run(db_session, run, t, 0, 3)
    _task_run(db_session, run, t, 10, 13, status=status, attempt=2)

    df = build_dataset(db_session)

    assert df["task_run_id"].tolist() == [good.id]


def test_rows_missing_a_timestamp_are_excluded(db_session):
    wf = _workflow(db_session)
    t = _task(db_session, wf, "t")
    run = _wf_run(db_session, wf)
    good = _task_run(db_session, run, t, 0, 3)
    _task_run(db_session, run, t, 10, None, attempt=2)    # no completed_at
    _task_run(db_session, run, t, None, 20, attempt=3)    # no started_at

    df = build_dataset(db_session)

    assert df["task_run_id"].tolist() == [good.id]


def test_zero_and_negative_durations_are_excluded(db_session):
    wf = _workflow(db_session)
    t = _task(db_session, wf, "t")
    run = _wf_run(db_session, wf)
    good = _task_run(db_session, run, t, 0, 3)
    _task_run(db_session, run, t, 10, 10, attempt=2)      # zero
    _task_run(db_session, run, t, 20, 18, attempt=3)      # negative

    df = build_dataset(db_session)

    assert df["task_run_id"].tolist() == [good.id]
    assert (df[TARGET] > 0).all()


def test_no_nans_in_required_columns(db_session):
    wf = _workflow(db_session)
    t = _task(db_session, wf, "t", command="python -c \"import time; time.sleep(2)\"")
    run = _wf_run(db_session, wf)
    for i in range(3):
        _task_run(db_session, run, t, i * 10, i * 10 + 2, attempt=i + 1)

    df = build_dataset(db_session)

    assert len(df) == 3
    assert not df[REQUIRED_COLUMNS].isna().any().any()
    # the one column allowed to be NaN is a task's first-ever run
    assert df["prev_mean_duration"].isna().sum() == 1


def test_empty_database_gives_empty_frame_with_the_right_columns(db_session):
    df = build_dataset(db_session)

    assert len(df) == 0
    assert list(df.columns) == ID_COLUMNS + [TARGET, "queue_wait_seconds"] + FEATURE_COLUMNS


# --- created_at handling, prefix filter ---------------------------------------

def test_rows_without_created_at_are_dropped_unless_asked(db_session):
    wf = _workflow(db_session)
    t = _task(db_session, wf, "t")
    run = _wf_run(db_session, wf)
    with_created = _task_run(db_session, run, t, 0, 3, created=-1)
    old = _task_run(db_session, run, t, 10, 13, attempt=2)
    old.created_at = None          # as if inserted before the migration
    db_session.flush()

    strict = build_dataset(db_session)
    lenient = build_dataset(db_session, require_created_at=False)

    assert strict["task_run_id"].tolist() == [with_created.id]
    assert sorted(lenient["task_run_id"].tolist()) == sorted([with_created.id, old.id])
    assert lenient[lenient["task_run_id"] == old.id]["queue_wait_seconds"].isna().all()


def test_name_prefix_filters_workflows(db_session):
    mine = _workflow(db_session, name="ds-a")
    other = _workflow(db_session, name="manual-b")
    r1 = _task_run(db_session, _wf_run(db_session, mine), _task(db_session, mine, "t"), 0, 2)
    _task_run(db_session, _wf_run(db_session, other), _task(db_session, other, "t"), 5, 7)

    df = build_dataset(db_session, name_prefix="ds-")

    assert df["task_run_id"].tolist() == [r1.id]


# --- leakage rules --------------------------------------------------------------

def test_history_uses_only_runs_finished_before_this_one_started(db_session):
    wf = _workflow(db_session)
    t = _task(db_session, wf, "t")
    run = _wf_run(db_session, wf)
    # durations 2, 4, then a run that OVERLAPS the third (starts at 20, ends
    # at 40, the third starts at 30 while it is still running), then the third.
    r1 = _task_run(db_session, run, t, 0, 2, attempt=1)
    r2 = _task_run(db_session, run, t, 10, 14, attempt=2)
    r_overlap = _task_run(db_session, run, t, 20, 40, attempt=3)
    r3 = _task_run(db_session, run, t, 30, 36, attempt=4)

    df = build_dataset(db_session)

    first = _row(df, r1.id)
    assert first["prev_runs"] == 0 and np.isnan(first["prev_mean_duration"])

    second = _row(df, r2.id)
    assert second["prev_runs"] == 1
    assert second["prev_mean_duration"] == pytest.approx(2.0)

    # r_overlap started at 20: only r1 (2s) and r2 (4s) had finished -> mean 3
    assert _row(df, r_overlap.id)["prev_mean_duration"] == pytest.approx(3.0)

    # r3 started at 30: r_overlap (ends 40) has NOT finished yet, so it must
    # not be in the history. Only r1 and r2 count -> mean 3, not (2+4+20)/3.
    third = _row(df, r3.id)
    assert third["prev_runs"] == 2
    assert third["prev_mean_duration"] == pytest.approx(3.0)


def test_history_is_per_task(db_session):
    wf = _workflow(db_session)
    a = _task(db_session, wf, "a")
    b = _task(db_session, wf, "b")
    run = _wf_run(db_session, wf)
    _task_run(db_session, run, a, 0, 10)
    rb = _task_run(db_session, run, b, 20, 21)

    df = build_dataset(db_session)

    assert _row(df, rb.id)["prev_runs"] == 0


def test_running_at_start_counts_only_runs_already_in_progress(db_session):
    wf = _workflow(db_session)
    a = _task(db_session, wf, "a")
    b = _task(db_session, wf, "b")
    c = _task(db_session, wf, "c")
    run = _wf_run(db_session, wf)
    ra = _task_run(db_session, run, a, 0, 10)
    rb = _task_run(db_session, run, b, 5, 8)      # starts while a is running
    rc = _task_run(db_session, run, c, 12, 15)    # a and b both finished
    # a FAILED attempt still occupies a worker, so it counts as load
    _task_run(db_session, run, a, 11, 20, status=TaskStatus.FAILED, attempt=2)

    df = build_dataset(db_session)

    assert _row(df, ra.id)["running_at_start"] == 0
    assert _row(df, rb.id)["running_at_start"] == 1
    assert _row(df, rc.id)["running_at_start"] == 1   # only the failed attempt


# --- command parsing -------------------------------------------------------------

@pytest.mark.parametrize(
    "command, family, sleep_s, sum_n, sort_n",
    [
        ('"C:\\py\\python3.11.exe" -c "import time; time.sleep(2.5)"', "sleep", 2.5, 0, 0),
        ('"python" -c "sum(range(40000000))"', "cpu_sum", 0.0, 40000000, 0),
        ('"python" -c "import random; sorted(random.random() for _ in range(900000))"', "sort", 0.0, 0, 900000),
        ('"python" -c "import time; time.sleep(1); sum(range(5000000))"', "sleep_cpu", 1.0, 5000000, 0),
        ("echo hello", "other", 0.0, 0, 0),
    ],
)
def test_parse_command(command, family, sleep_s, sum_n, sort_n):
    parsed = parse_command(command)

    assert parsed["cmd_family"] == family
    assert parsed["cmd_sleep_s"] == pytest.approx(sleep_s)
    assert parsed["cmd_sum_n"] == sum_n
    assert parsed["cmd_sort_n"] == sort_n
    assert parsed["cmd_length"] == len(command)


def test_interpreter_path_digits_are_not_mistaken_for_parameters():
    parsed = parse_command('"C:\\Python311\\python3.11.exe" -c "exit(0)"')

    assert parsed["cmd_family"] == "other"
    assert parsed["cmd_sleep_s"] == 0.0 and parsed["cmd_sum_n"] == 0


# --- time split ---------------------------------------------------------------------

def test_time_split_is_chronological_and_never_overlaps():
    df = pd.DataFrame({"started_at": [T0 + timedelta(seconds=s) for s in (9, 1, 5, 3, 7, 2, 8, 4, 6, 10)]})

    train, test = time_split(df, test_fraction=0.2)

    assert len(train) == 8 and len(test) == 2
    assert train["started_at"].max() <= test["started_at"].min()


@pytest.mark.parametrize("bad", [0, 1, -0.1, 1.5])
def test_time_split_rejects_bad_fraction(bad):
    with pytest.raises(ValueError):
        time_split(pd.DataFrame({"started_at": [T0]}), test_fraction=bad)
