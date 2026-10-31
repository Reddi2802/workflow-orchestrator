# tests/test_predictor.py
"""
Week 8 tests: the predictor, the Task -> feature-row step, the critical path
and the three API endpoints.

Training logic is tested on SYNTHETIC data whose true answer is known (duration
is a clean function of the command's parameter), so "the model learned the
signal" is a checkable statement, not a vibe. The API is tested with a stub
predictor whose output is exact, so the critical path and the flags have
exact expected values. Real Postgres for everything that touches the database.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.api.v1 import predictions as predictions_api
from app.ml import predictor as predictor_module
from app.ml.features import FEATURE_COLUMNS, TARGET
from app.ml.inference import build_inference_frame
from app.ml.predictor import (
    COLD_FEATURES,
    DurationPredictor,
    PredictorNotAvailable,
    critical_path,
    get_predictor,
    train_models,
)
from app.models.entities import Task, TaskDependency, TaskRun, Workflow, WorkflowRun
from app.models.enums import TaskStatus, TriggerType, WorkflowRunStatus

T0 = datetime(2026, 1, 1, 12, 0, 0)


# --- synthetic data with a known answer ---------------------------------------------

def synthetic(n: int = 300, seed: int = 0, prev_mean_mode: str = "good") -> pd.DataFrame:
    """duration = sleep seconds, or loop size times a constant, plus a small
    constant and a little noise. prev_mean_mode: 'good' = close to the true
    duration, 'exact' = equal to it, 'garbage' = unrelated random numbers."""
    rng = np.random.default_rng(seed)
    family = rng.choice(["sleep", "cpu_sum", "sort"], n)
    sleep_s = np.where(family == "sleep", rng.uniform(0.8, 5.0, n), 0.0)
    sum_n = np.where(family == "cpu_sum", rng.integers(10_000_000, 150_000_000, n), 0)
    sort_n = np.where(family == "sort", rng.integers(200_000, 2_000_000, n), 0)
    duration = sleep_s + sum_n * 2.2e-8 + sort_n * 3.0e-6 + 0.2 + rng.normal(0, 0.05, n)

    prev_runs = rng.integers(0, 4, n)
    if prev_mean_mode == "exact":
        prev_mean = duration.copy()
    elif prev_mean_mode == "garbage":
        prev_mean = rng.uniform(0.0, 20.0, n)
    else:
        prev_mean = duration + rng.normal(0, 0.1, n)
    prev_mean = np.where(prev_runs >= 1, prev_mean, np.nan)

    started = [T0 + timedelta(seconds=10 * i) for i in range(n)]
    return pd.DataFrame({
        "task_run_id": np.arange(n),
        "workflow_id": 1, "workflow_run_id": 1, "task_id": np.arange(n),
        "started_at": started,
        "completed_at": [s + timedelta(seconds=float(d)) for s, d in zip(started, duration)],
        TARGET: duration,
        "queue_wait_seconds": 0.0,
        "cmd_family": family,
        "cmd_length": 100,
        "cmd_sleep_s": sleep_s,
        "cmd_sum_n": sum_n,
        "cmd_sort_n": sort_n,
        "dag_depth": rng.integers(0, 3, n),
        "n_upstream": rng.integers(0, 3, n),
        "n_downstream": rng.integers(0, 3, n),
        "n_tasks_in_workflow": 4,
        "max_retries": 0,
        "attempt_number": 1,
        "running_at_start": rng.integers(0, 3, n),
        "prev_runs": prev_runs,
        "prev_mean_duration": prev_mean,
    })


# --- training --------------------------------------------------------------------------

def test_cold_start_model_learns_the_command_signal():
    _, report = train_models(synthetic())

    cold, baseline = report["holdout"]["cold_start_model"], report["holdout"]["global_mean"]
    assert cold["mae"] < 0.3 * baseline["mae"]
    assert cold["r2"] > 0.9


def test_holdout_is_the_latest_rows_in_time_order():
    df = synthetic()

    _, report = train_models(df, test_fraction=0.2)

    assert report["train_rows"] == 240 and report["test_rows"] == 60
    # the holdout samples are the newest rows, so their ids are all in the last 20%
    assert report["samples"]["task_run_id"].min() >= 240


def test_exact_history_makes_the_previous_mean_the_served_strategy():
    predictor, report = train_models(synthetic(prev_mean_mode="exact"))

    assert predictor.metadata["history_strategy"] == "previous_mean"
    frame = synthetic(prev_mean_mode="exact").iloc[:40]
    result = predictor.predict(frame)
    has_history = (frame["prev_runs"] >= 1).to_numpy()
    np.testing.assert_allclose(
        result.loc[has_history, "predicted_seconds"].to_numpy(),
        frame.loc[has_history, "prev_mean_duration"].to_numpy(),
    )


def test_useless_history_makes_the_model_the_served_strategy():
    predictor, _ = train_models(synthetic(prev_mean_mode="garbage"))

    assert predictor.metadata["history_strategy"] == "history_model"


def test_basis_is_history_only_when_the_task_has_run_before():
    predictor, _ = train_models(synthetic())
    frame = synthetic().iloc[:30].copy()
    frame.loc[frame.index[0], ["prev_runs", "prev_mean_duration"]] = [0, np.nan]
    frame.loc[frame.index[1], ["prev_runs", "prev_mean_duration"]] = [2, 3.0]

    result = predictor.predict(frame)

    assert result.loc[frame.index[0], "basis"] == "cold_start"
    assert result.loc[frame.index[1], "basis"] == "history"
    assert (result["predicted_seconds"] >= 0).all()


def test_long_threshold_is_the_requested_quantile_of_training_durations():
    df = synthetic()

    predictor, _ = train_models(df, long_quantile=0.9)

    assert predictor.long_threshold_seconds == pytest.approx(df[TARGET].quantile(0.9))
    assert predictor.is_long(predictor.long_threshold_seconds)
    assert not predictor.is_long(predictor.long_threshold_seconds - 0.01)


def test_too_few_rows_is_refused_with_a_useful_message():
    with pytest.raises(ValueError, match="at least"):
        train_models(synthetic(n=20))


@pytest.mark.parametrize("bad", [0, 1, 1.5])
def test_bad_long_quantile_is_refused(bad):
    with pytest.raises(ValueError):
        train_models(synthetic(), long_quantile=bad)


def test_save_and_load_give_identical_predictions(tmp_path):
    predictor, _ = train_models(synthetic())
    frame = synthetic().iloc[:25]

    path = predictor.save(tmp_path / "m.joblib")
    loaded = DurationPredictor.load(path)

    pd.testing.assert_frame_equal(predictor.predict(frame), loaded.predict(frame))
    assert loaded.metadata["trained_at"] == predictor.metadata["trained_at"]


def test_loading_a_missing_or_corrupt_file_raises_predictor_not_available(tmp_path):
    with pytest.raises(PredictorNotAvailable, match="train_predictor"):
        DurationPredictor.load(tmp_path / "nope.joblib")

    bad = tmp_path / "bad.joblib"
    bad.write_text("this is not a model")
    with pytest.raises(PredictorNotAvailable):
        DurationPredictor.load(bad)


def test_get_predictor_reloads_when_the_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "m.joblib"
    monkeypatch.setenv("PREDICTOR_MODEL_PATH", str(path))
    monkeypatch.setitem(predictor_module._cache, "predictor", None)

    with pytest.raises(PredictorNotAvailable):
        get_predictor()

    first, _ = train_models(synthetic(seed=1), long_quantile=0.5)
    first.save(path)
    assert get_predictor().metadata["long_quantile"] == 0.5

    second, _ = train_models(synthetic(seed=2), long_quantile=0.9)
    second.save(path)
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))   # make the change visible on coarse clocks
    assert get_predictor().metadata["long_quantile"] == 0.9


# --- critical path ---------------------------------------------------------------------------

def test_critical_path_of_a_diamond_follows_the_slowest_branch():
    durations = {1: 1.0, 2: 5.0, 3: 2.0, 4: 1.0}
    path, total = critical_path(durations, [(1, 2), (1, 3), (2, 4), (3, 4)])

    assert path == [1, 2, 4]
    assert total == pytest.approx(7.0)


def test_critical_path_of_a_chain_is_the_whole_chain():
    path, total = critical_path({1: 1.0, 2: 2.0, 3: 3.0}, [(1, 2), (2, 3)])

    assert path == [1, 2, 3] and total == pytest.approx(6.0)


def test_critical_path_with_no_edges_is_the_single_longest_task():
    path, total = critical_path({1: 1.0, 2: 4.0, 3: 2.0}, [])

    assert path == [2] and total == pytest.approx(4.0)


def test_critical_path_of_nothing():
    assert critical_path({}, []) == ([], 0.0)


# --- Task -> feature row (database) -------------------------------------------------------

def _sleep_cmd(seconds) -> str:
    return f'"{sys.executable}" -c "import time; time.sleep({seconds})"'


def _make_diamond(db, durations=(1, 5, 2, 1), name="pred"):
    wf = Workflow(name=name)
    db.add(wf)
    db.flush()
    tasks = [Task(workflow_id=wf.id, name=n, command=_sleep_cmd(d)) for n, d in zip("abcd", durations)]
    db.add_all(tasks)
    db.flush()
    a, b, c, d = tasks
    for up, down in [(a, b), (a, c), (b, d), (c, d)]:
        db.add(TaskDependency(upstream_task_id=up.id, downstream_task_id=down.id))
    db.flush()
    return wf, tasks


def test_inference_frame_has_the_training_columns_and_correct_values(db_session):
    wf, (a, b, c, d) = _make_diamond(db_session)
    run = WorkflowRun(workflow_id=wf.id, status=WorkflowRunStatus.RUNNING, trigger_type=TriggerType.MANUAL)
    db_session.add(run)
    db_session.flush()
    # task a has two earlier successes (2s and 4s); task b has a failure only
    for i, secs in enumerate((2, 4)):
        db_session.add(TaskRun(
            workflow_run_id=run.id, task_id=a.id, status=TaskStatus.SUCCESS, attempt_number=i + 1,
            started_at=T0, completed_at=T0 + timedelta(seconds=secs),
        ))
    db_session.add(TaskRun(
        workflow_run_id=run.id, task_id=b.id, status=TaskStatus.FAILED, attempt_number=1,
        started_at=T0, completed_at=T0 + timedelta(seconds=9),
    ))
    # one task currently running -> running_at_start is 1
    db_session.add(TaskRun(workflow_run_id=run.id, task_id=c.id, status=TaskStatus.RUNNING, attempt_number=1, started_at=T0))
    db_session.flush()

    frame = build_inference_frame(db_session, [a.id, b.id, c.id, d.id, 999999999])

    assert list(frame.columns) == FEATURE_COLUMNS
    assert sorted(frame.index) == sorted([a.id, b.id, c.id, d.id])      # the unknown id is absent
    assert frame.loc[a.id, "prev_runs"] == 2
    assert frame.loc[a.id, "prev_mean_duration"] == pytest.approx(3.0)
    assert frame.loc[b.id, "prev_runs"] == 0 and np.isnan(frame.loc[b.id, "prev_mean_duration"])
    assert frame.loc[a.id, "cmd_family"] == "sleep" and frame.loc[a.id, "cmd_sleep_s"] == 1.0
    assert (frame.loc[a.id, "dag_depth"], frame.loc[d.id, "dag_depth"]) == (0, 2)
    assert frame.loc[d.id, "n_upstream"] == 2 and frame.loc[a.id, "n_downstream"] == 2
    assert (frame["n_tasks_in_workflow"] == 4).all()
    assert (frame["attempt_number"] == 1).all()
    assert (frame["running_at_start"] == 1).all()


def test_a_model_trained_on_the_training_columns_can_score_an_inference_frame(db_session):
    """Guards against train/serve skew: if build_inference_frame ever drifts from
    FEATURE_COLUMNS, training succeeds but this fails."""
    predictor, _ = train_models(synthetic())
    _, tasks = _make_diamond(db_session, name="skew")

    frame = build_inference_frame(db_session, [t.id for t in tasks])
    result = predictor.predict(frame)

    assert len(result) == 4
    assert set(result["basis"]) == {"cold_start"}
    assert result["predicted_seconds"].notna().all()
    assert set(COLD_FEATURES) <= set(frame.columns)


def test_empty_inputs_give_empty_frames(db_session):
    assert build_inference_frame(db_session, []).empty
    assert build_inference_frame(db_session, [999999999]).empty


# --- API (stub predictor with exact output) ---------------------------------------------------

class StubPredictor:
    """Predicts exactly the sleep seconds in each task's command, so a task's
    command decides its predicted duration."""

    long_threshold_seconds = 4.0
    metadata = {
        "trained_at": "2026-01-01T00:00:00+00:00",
        "training_rows": 400,
        "holdout_rows": 80,
        "long_threshold_seconds": 4.0,
        "long_quantile": 0.75,
        "history_strategy": "previous_mean",
        "holdout": {"cold_start_model": {"mae": 0.3, "rmse": 0.4, "r2": 0.9, "n": 80}},
    }

    def predict(self, frame):
        return pd.DataFrame(
            {"predicted_seconds": frame["cmd_sleep_s"].astype(float),
             "basis": np.where(frame["prev_runs"] >= 1, "history", "cold_start")},
            index=frame.index,
        )

    def is_long(self, seconds):
        return seconds >= self.long_threshold_seconds


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(predictions_api, "get_predictor", lambda: StubPredictor())


@pytest.fixture
def no_model(monkeypatch):
    def missing():
        raise PredictorNotAvailable("No trained model. Train one: uv run python scripts/train_predictor.py")
    monkeypatch.setattr(predictions_api, "get_predictor", missing)


def _create_diamond_via_api(client, durations=(1, 5, 2, 1)):
    resp = client.post("/api/v1/workflows", json={
        "name": "pred-api",
        "tasks": [{"name": n, "command": _sleep_cmd(d)} for n, d in zip("abcd", durations)],
        "dependencies": [{"upstream_task_name": u, "downstream_task_name": d}
                         for u, d in [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]],
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_workflow_predictions_give_the_critical_path_and_flags(client, stub):
    wf = _create_diamond_via_api(client)
    ids = {t["name"]: t["id"] for t in wf["tasks"]}

    resp = client.get(f"/api/v1/workflows/{wf['id']}/predictions")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["critical_path"] == [ids["a"], ids["b"], ids["d"]]
    assert body["estimated_total_seconds"] == pytest.approx(7.0)
    assert body["long_threshold_seconds"] == 4.0
    by_name = {t["name"]: t for t in body["tasks"]}
    assert {n: by_name[n]["predicted_seconds"] for n in "abcd"} == {"a": 1.0, "b": 5.0, "c": 2.0, "d": 1.0}
    assert {n: by_name[n]["on_critical_path"] for n in "abcd"} == {"a": True, "b": True, "c": False, "d": True}
    assert {n: by_name[n]["likely_long"] for n in "abcd"} == {"a": False, "b": True, "c": False, "d": False}
    assert all(t["basis"] == "cold_start" for t in body["tasks"])


def test_task_prediction(client, stub):
    wf = _create_diamond_via_api(client)
    task_id = next(t["id"] for t in wf["tasks"] if t["name"] == "b")

    resp = client.get(f"/api/v1/tasks/{task_id}/prediction")

    assert resp.status_code == 200
    assert resp.json() == {
        "task_id": task_id, "name": "b", "predicted_seconds": 5.0,
        "basis": "cold_start", "likely_long": True, "on_critical_path": False,
    }


def test_empty_workflow_predictions_are_empty_not_an_error(client, stub):
    wf = client.post("/api/v1/workflows", json={"name": "pred-empty", "tasks": []}).json()

    resp = client.get(f"/api/v1/workflows/{wf['id']}/predictions")

    assert resp.status_code == 200
    assert resp.json()["tasks"] == [] and resp.json()["estimated_total_seconds"] == 0.0


def test_predictor_info(client, stub):
    resp = client.get("/api/v1/predictor")

    assert resp.status_code == 200
    assert resp.json()["training_rows"] == 400
    assert resp.json()["history_strategy"] == "previous_mean"
    assert resp.json()["holdout"]["cold_start_model"]["mae"] == 0.3


def test_unknown_ids_are_404(client, stub):
    assert client.get("/api/v1/tasks/999999999/prediction").status_code == 404
    assert client.get("/api/v1/workflows/999999999/predictions").status_code == 404


def test_every_endpoint_says_503_with_instructions_when_no_model_exists(client, no_model):
    wf = _create_diamond_via_api(client)
    task_id = wf["tasks"][0]["id"]

    for url in ("/api/v1/predictor", f"/api/v1/tasks/{task_id}/prediction",
                f"/api/v1/workflows/{wf['id']}/predictions"):
        resp = client.get(url)
        assert resp.status_code == 503, url
        assert "train_predictor" in resp.json()["detail"]
