# app/ml/predictor.py
"""
Week 8: the bottleneck predictor. Trains regression models on the Week 6
dataset (app/ml/features.py) and predicts how long a task will run.

This module has NO database code: it takes DataFrames in and gives DataFrames
out, so it is testable without Postgres. Turning a Task row into the feature
row the model expects lives in app/ml/inference.py; the HTTP endpoints live in
app/api/v1/predictions.py.

TWO MODELS, ONE PREDICTOR
  full  uses every feature, including the task's own history
        (prev_runs, prev_mean_duration). Used when the task has run before.
  cold  uses everything EXCEPT history. Used for a task that has never run.

For a task that HAS run before, the predictor serves whichever is better on the
holdout rows that have history: the full model, or simply the task's previous
mean ("history_strategy" in the metadata). On repetitive workloads the previous
mean is very hard to beat, and shipping a model that loses to a one-line
baseline would be worse than shipping the baseline.

Why two, not one: on the Week 6 data a task's previous mean duration alone
correlates about 0.99 with its duration (the generator repeats the same
commands). A single model fed that column mostly learns "copy the last
result", and its score says nothing about whether it learned anything from the
command, the DAG or the load. The cold model is the honest test of that, and
it is also what a brand-new task needs in practice.

HOW IT IS JUDGED (see train_models): on a time-ordered holdout, never a
random split, against two baselines that need no learning:
  global_mean     predict the training mean for everything
  previous_mean   predict the task's own previous mean (global mean if none)
A model that does not beat its baseline has learned nothing useful, and the
training script says so instead of printing a good-looking R2.

"LIKELY LONG" is relative: a task is flagged when its predicted duration is at
or above the `long_quantile` (default 75th percentile) of the training
durations. It is not an absolute number of seconds.

The saved file is a plain dict written with joblib, so loading it does not
depend on this class staying importable under the same name. It does depend on
the scikit-learn version, which uv.lock pins for both of you.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import networkx as nx
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from app.ml.features import FEATURE_COLUMNS, TARGET, time_split

logger = logging.getLogger(__name__)

FAMILIES = ["sleep", "cpu_sum", "sort", "sleep_cpu", "other"]
HISTORY_COLUMNS = ["prev_runs", "prev_mean_duration"]
FULL_FEATURES = list(FEATURE_COLUMNS)
COLD_FEATURES = [c for c in FEATURE_COLUMNS if c not in HISTORY_COLUMNS]

MIN_HISTORY_RUNS = 1        # a task needs this many earlier successes to use the history path
MIN_TRAINING_ROWS = 50
MIN_SELECTION_ROWS = 10     # holdout rows with history needed before the strategy choice is trusted
MODEL_FORMAT = 1

DEFAULT_MODEL_PATH = Path(__file__).resolve().parents[2] / "models" / "duration_model.joblib"


class PredictorNotAvailable(Exception):
    """No trained model file exists (or it could not be read)."""


# --- models -------------------------------------------------------------------------

def _make_pipeline(features: list[str], random_state: int) -> Pipeline:
    """One-hot the command family, pass numbers through, fit a gradient-boosted
    tree model. Trees need no scaling and handle the missing
    prev_mean_duration (a task's first run) natively."""
    pre = ColumnTransformer(
        [(
            "family",
            OneHotEncoder(categories=[FAMILIES], handle_unknown="ignore", sparse_output=False),
            ["cmd_family"],
        )],
        remainder="passthrough",
    )
    model = HistGradientBoostingRegressor(
        max_iter=300,
        learning_rate=0.05,
        max_leaf_nodes=15,
        min_samples_leaf=10,
        l2_regularization=1.0,
        early_stopping=False,
        random_state=random_state,
    )
    return Pipeline([("pre", pre), ("model", model)])


def _fit(df: pd.DataFrame, features: list[str], random_state: int) -> Pipeline:
    pipe = _make_pipeline(features, random_state)
    pipe.fit(df[features], df[TARGET])
    return pipe


def _scores(y_true, y_pred) -> dict[str, float]:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    return {
        "mae": float(mean_absolute_error(y_true, y_pred)),
        "rmse": float(np.sqrt(mean_squared_error(y_true, y_pred))),
        "r2": float(r2_score(y_true, y_pred)) if len(y_true) > 1 else float("nan"),
        "n": int(len(y_true)),
    }


class DurationPredictor:
    """Holds the two fitted models plus metadata, and picks the right one per row."""

    def __init__(self, cold_model: Pipeline, full_model: Pipeline, metadata: dict[str, Any]):
        self.cold_model = cold_model
        self.full_model = full_model
        self.metadata = metadata

    @property
    def long_threshold_seconds(self) -> float:
        return float(self.metadata["long_threshold_seconds"])

    def predict(self, frame: pd.DataFrame) -> pd.DataFrame:
        """frame has the FEATURE_COLUMNS. Returns a frame with the same index and
        columns predicted_seconds, basis ('history' | 'cold_start')."""
        use_full = ((frame["prev_runs"] >= MIN_HISTORY_RUNS) & frame["prev_mean_duration"].notna()).to_numpy()
        predicted = np.zeros(len(frame), dtype=float)
        if use_full.any():
            if self.metadata.get("history_strategy") == "previous_mean":
                predicted[use_full] = frame.loc[use_full, "prev_mean_duration"].to_numpy(dtype=float)
            else:
                predicted[use_full] = self.full_model.predict(frame.loc[use_full, FULL_FEATURES])
        if (~use_full).any():
            predicted[~use_full] = self.cold_model.predict(frame.loc[~use_full, COLD_FEATURES])
        predicted = np.maximum(predicted, 0.0)
        return pd.DataFrame(
            {"predicted_seconds": predicted, "basis": np.where(use_full, "history", "cold_start")},
            index=frame.index,
        )

    def is_long(self, seconds: float) -> bool:
        return bool(seconds >= self.long_threshold_seconds)

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {"format": MODEL_FORMAT, "cold": self.cold_model, "full": self.full_model, "metadata": self.metadata},
            path,
        )
        return path

    @classmethod
    def load(cls, path: Path | str) -> "DurationPredictor":
        try:
            blob = joblib.load(path)
        except FileNotFoundError as exc:
            raise PredictorNotAvailable(
                f"No trained model at {path}. Train one: uv run python scripts/train_predictor.py"
            ) from exc
        except Exception as exc:   # noqa: BLE001 -- corrupt file, version mismatch, ...
            raise PredictorNotAvailable(f"Could not read the model at {path}: {exc}") from exc
        if not isinstance(blob, dict) or blob.get("format") != MODEL_FORMAT:
            raise PredictorNotAvailable(f"{path} is not a format-{MODEL_FORMAT} model file; retrain it.")
        return cls(blob["cold"], blob["full"], blob["metadata"])


# --- training and evaluation --------------------------------------------------------

def train_models(
    df: pd.DataFrame,
    *,
    test_fraction: float = 0.2,
    long_quantile: float = 0.75,
    random_state: int = 0,
) -> tuple[DurationPredictor, dict[str, Any]]:
    """Evaluate on a time-ordered holdout, then refit on ALL rows for serving.

    The reported metrics are from the models fitted on the earliest
    (1 - test_fraction) of rows only, scored on the latest test_fraction. The
    served models are refit on everything so they also learn from the most
    recent runs; their quality is what the holdout numbers estimate.

    Returns (predictor, report). report["holdout"] maps model name -> scores;
    report["samples"] is a DataFrame of holdout rows with each model's
    prediction, for checking a few by hand.
    """
    if len(df) < MIN_TRAINING_ROWS:
        raise ValueError(
            f"need at least {MIN_TRAINING_ROWS} rows to train, got {len(df)}. "
            f"Generate more data with scripts/generate_timing_data.py."
        )
    if not 0 < long_quantile < 1:
        raise ValueError("long_quantile must be between 0 and 1 (exclusive)")

    train, test = time_split(df, test_fraction)
    if len(test) < 2:
        raise ValueError("the holdout has fewer than 2 rows; lower test_fraction or add data")

    global_mean = float(train[TARGET].mean())
    test = test.copy()
    test["pred_global_mean"] = global_mean
    test["pred_previous_mean"] = test["prev_mean_duration"].fillna(global_mean)

    cold_eval = _fit(train, COLD_FEATURES, random_state)
    full_eval = _fit(train, FULL_FEATURES, random_state)
    test["pred_cold"] = cold_eval.predict(test[COLD_FEATURES])
    test["pred_full"] = full_eval.predict(test[FULL_FEATURES])

    names = {
        "global_mean": "pred_global_mean",
        "previous_mean": "pred_previous_mean",
        "cold_start_model": "pred_cold",
        "history_model": "pred_full",
    }
    y = test[TARGET]
    holdout = {name: _scores(y, test[col]) for name, col in names.items()}

    with_history = test["prev_runs"] >= MIN_HISTORY_RUNS
    subsets = {}
    for label, mask in (("rows_with_history", with_history), ("first_time_rows", ~with_history)):
        part = test[mask]
        subsets[label] = (
            {name: _scores(part[TARGET], part[col]) for name, col in names.items()} if len(part) >= 2 else {}
        )

    by_family = {}
    for family, part in test.groupby("cmd_family"):
        by_family[family] = {
            "n": int(len(part)),
            **{f"mae_{name}": float(mean_absolute_error(part[TARGET], part[col])) for name, col in names.items()},
        }

    # Which prediction to serve for a task that has history: decided on the
    # holdout rows that have history, ties go to the simpler previous mean.
    # Needs enough such rows to mean anything; otherwise default to the model.
    hist = subsets["rows_with_history"]
    if hist and hist["previous_mean"]["n"] >= MIN_SELECTION_ROWS and (
        hist["previous_mean"]["mae"] <= hist["history_model"]["mae"]
    ):
        history_strategy = "previous_mean"
    else:
        history_strategy = "history_model"

    metadata: dict[str, Any] = {
        "history_strategy": history_strategy,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "training_rows": int(len(df)),
        "holdout_rows": int(len(test)),
        "test_fraction": test_fraction,
        "long_quantile": long_quantile,
        "long_threshold_seconds": float(df[TARGET].quantile(long_quantile)),
        "global_mean_seconds": float(df[TARGET].mean()),
        "holdout": holdout,
        "fit": "metrics are from models fitted on the earliest rows only; the saved models are refit on all rows",
        "features_full": FULL_FEATURES,
        "features_cold": COLD_FEATURES,
    }

    predictor = DurationPredictor(
        cold_model=_fit(df, COLD_FEATURES, random_state),
        full_model=_fit(df, FULL_FEATURES, random_state),
        metadata=metadata,
    )

    report = {
        "holdout": holdout,
        "subsets": subsets,
        "by_family": by_family,
        "samples": test[["task_run_id", "cmd_family", "cmd_sleep_s", "cmd_sum_n", "cmd_sort_n", "prev_runs",
                         TARGET, "pred_previous_mean", "pred_cold", "pred_full"]].copy(),
        "train_rows": int(len(train)),
        "test_rows": int(len(test)),
    }
    return predictor, report


# --- critical path ------------------------------------------------------------------

def critical_path(durations: dict[int, float], edges: list[tuple[int, int]]) -> tuple[list[int], float]:
    """The longest chain of dependent tasks by predicted duration: (task ids
    from first to last, total seconds). These are the tasks that decide how
    long the workflow takes when nothing waits for a worker, i.e. its
    bottleneck. Edges are (upstream_task_id, downstream_task_id)."""
    if not durations:
        return [], 0.0
    graph = nx.DiGraph()
    graph.add_nodes_from(durations)
    graph.add_edges_from((u, d) for u, d in edges if u in durations and d in durations)

    best: dict[int, float] = {}
    came_from: dict[int, int | None] = {}
    for node in nx.topological_sort(graph):
        preds = list(graph.predecessors(node))
        if preds:
            parent = max(preds, key=lambda p: best[p])
            best[node] = best[parent] + durations[node]
            came_from[node] = parent
        else:
            best[node] = durations[node]
            came_from[node] = None

    end = max(best, key=best.get)
    path: list[int] = []
    cursor: int | None = end
    while cursor is not None:
        path.append(cursor)
        cursor = came_from[cursor]
    path.reverse()
    return path, float(best[end])


# --- loading for the API ------------------------------------------------------------

_cache: dict[str, Any] = {"path": None, "mtime": None, "predictor": None}


def model_path() -> Path:
    return Path(os.environ.get("PREDICTOR_MODEL_PATH", DEFAULT_MODEL_PATH))


def get_predictor() -> DurationPredictor:
    """The saved model, reloaded automatically if the file changes (so
    retraining does not need a server restart). Raises PredictorNotAvailable
    if there is no model."""
    path = model_path()
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError as exc:
        _cache.update(path=None, mtime=None, predictor=None)
        raise PredictorNotAvailable(
            f"No trained model at {path}. Train one: uv run python scripts/train_predictor.py"
        ) from exc
    if _cache["predictor"] is None or _cache["path"] != str(path) or _cache["mtime"] != mtime:
        _cache.update(path=str(path), mtime=mtime, predictor=DurationPredictor.load(path))
        logger.info("Loaded duration model from %s", path)
    return _cache["predictor"]
