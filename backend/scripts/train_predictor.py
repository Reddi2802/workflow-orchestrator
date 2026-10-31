"""
Train the task-duration predictor, judge it honestly, and save it.

    uv run python scripts/train_predictor.py
    uv run python scripts/train_predictor.py --prefix ds- --out models/duration_model.joblib

Needs the timing dataset in the database (scripts/generate_timing_data.py).
Writes models/duration_model.joblib (and a .metrics.json next to it); the API
loads that file, and reloads it automatically when it changes, so you do not
need to restart the server after retraining.

What it prints, and how to read it:
  1. HOLDOUT SCORES: every model and two baselines, on the LATEST 20% of rows
     (time-ordered, never random). MAE is the average error in seconds: lower
     is better. R2 near 1 is good, near 0 means no better than the average.
  2. TWO SUBSETS. Rows whose task has run before ("with history") and rows
     that have not ("first time"). They are different problems and need
     different baselines: a task you have seen before is predicted well by
     "what it took last time"; a new task has nothing to copy.
  3. BY FAMILY: where the model is weak (CPU-bound commands are noisier than sleeps).
  4. SAMPLES: holdout rows with actual vs predicted, for checking by hand.
  5. VERDICT: plain yes/no on whether the model learned something beyond its baseline.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import SessionLocal  # noqa: E402
from app.ml.features import TARGET, build_dataset  # noqa: E402
from app.ml.predictor import DEFAULT_MODEL_PATH, train_models  # noqa: E402

NAMES = ["global_mean", "previous_mean", "cold_start_model", "history_model"]


def _table(rows: dict[str, dict]) -> str:
    lines = [f"  {'model':<18} {'MAE (s)':>8} {'RMSE (s)':>9} {'R2':>7} {'rows':>5}"]
    for name in NAMES:
        s = rows.get(name)
        if s:
            lines.append(f"  {name:<18} {s['mae']:>8.3f} {s['rmse']:>9.3f} {s['r2']:>7.3f} {s['n']:>5}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prefix", default="ds-", help="only workflows whose name starts with this")
    ap.add_argument("--out", default=str(DEFAULT_MODEL_PATH))
    ap.add_argument("--test-fraction", type=float, default=0.2)
    ap.add_argument("--long-quantile", type=float, default=0.75)
    ap.add_argument("--samples", type=int, default=12)
    args = ap.parse_args()

    db = SessionLocal()
    try:
        df = build_dataset(db, name_prefix=args.prefix)
    finally:
        db.close()
    print(f"dataset: {len(df)} successful TaskRuns (prefix {args.prefix!r})")
    if len(df) == 0:
        print("No rows. Run scripts/generate_timing_data.py first, and check --prefix.")
        return 1

    try:
        predictor, report = train_models(
            df, test_fraction=args.test_fraction, long_quantile=args.long_quantile
        )
    except ValueError as exc:
        print(f"cannot train: {exc}")
        return 1

    print(f"split by time: train {report['train_rows']} rows (earliest), holdout {report['test_rows']} rows (latest)\n")

    print("1. HOLDOUT SCORES, all holdout rows")
    print(_table(report["holdout"]))
    for label, title in (("rows_with_history", "2a. rows whose task HAS run before"),
                         ("first_time_rows", "2b. rows whose task has NOT run before (first time)")):
        sub = report["subsets"][label]
        print(f"\n{title}")
        print(_table(sub) if sub else "  (too few rows)")

    print("\n3. MAE BY COMMAND FAMILY (seconds)")
    fam = pd.DataFrame(report["by_family"]).T
    cols = ["n"] + [f"mae_{n}" for n in NAMES]
    print(fam[cols].rename(columns={f"mae_{n}": n for n in NAMES}).round(3).to_string())

    print(f"\n4. {args.samples} HOLDOUT ROWS, actual vs predicted (seconds)")
    samples = report["samples"].sample(
        min(args.samples, len(report["samples"])), random_state=0
    ).sort_values("task_run_id")
    # one "size" column: loop count for CPU/sort commands, 0 for pure sleeps
    samples = samples.assign(n=samples["cmd_sum_n"] + samples["cmd_sort_n"])
    view = samples[["task_run_id", "cmd_family", "cmd_sleep_s", "n", "prev_runs", TARGET,
                    "pred_previous_mean", "pred_cold", "pred_full"]].copy()
    view.columns = ["task_run", "family", "sleep_s", "loop_n", "prev_runs", "ACTUAL",
                    "prev_mean", "cold_model", "history_model"]
    print(view.round(2).to_string(index=False))

    holdout = report["holdout"]
    subs = report["subsets"]
    print("\n5. VERDICT")
    cold_wins = holdout["cold_start_model"]["mae"] < holdout["global_mean"]["mae"]
    first = subs["first_time_rows"]
    if first:
        cold_vs = first["cold_start_model"]["mae"] / first["global_mean"]["mae"]
        print(
            f"  new tasks (no history): cold-start model error is {cold_vs:.0%} of the guess-the-average error "
            f"({first['cold_start_model']['mae']:.2f}s vs {first['global_mean']['mae']:.2f}s). "
            f"{'It learned from the command, DAG and load.' if cold_vs < 0.8 else 'WEAK: barely better than the average.'}"
        )
    elif cold_wins:
        print("  cold-start model beats guessing the average overall.")
    else:
        print("  WEAK: the cold-start model does not beat guessing the average.")

    hist = subs["rows_with_history"]
    strategy = predictor.metadata["history_strategy"]
    if hist:
        pm, hm = hist["previous_mean"]["mae"], hist["history_model"]["mae"]
        if strategy == "previous_mean":
            print(
                f"  tasks with history: copying the previous mean ({pm:.2f}s) is at least as good as the "
                f"full model ({hm:.2f}s), so that is what the API serves for them. This is common when the same "
                f"commands repeat, and it means the ML only adds value for NEW tasks on this data."
            )
        else:
            print(f"  tasks with history: the full model ({hm:.2f}s) beats the previous mean ({pm:.2f}s) and is served.")
    print("  caveat: this data was generated by scripts/generate_timing_data.py from a few command families;")
    print("  see docs/week8_predictor.md before quoting these numbers anywhere.")

    out = Path(args.out)
    predictor.save(out)
    metrics_path = out.with_suffix(".metrics.json")
    metrics_path.write_text(json.dumps(
        {k: v for k, v in predictor.metadata.items() if k not in ("features_full", "features_cold")}, indent=2
    ))
    print(f"\nsaved {out}\nsaved {metrics_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
