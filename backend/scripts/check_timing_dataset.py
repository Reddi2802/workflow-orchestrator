"""
Sanity-check the timing dataset BEFORE anyone trains on it.

    uv run python scripts/check_timing_dataset.py
    uv run python scripts/check_timing_dataset.py --prefix ds- --out data/timing_dataset.csv

Answers four questions:
  1. Is there enough data?             (row count)
  2. Is there real spread to predict?  (duration distribution)
  3. Which features are dead or alive? (constant columns, correlation with duration)
  4. Does a trivial model already beat the mean on a time-ordered test set?
     (if even a 3-feature least-squares fit cannot, the dataset is the problem,
     not the Week 8 model)

Writes the dataset to a CSV (default data/timing_dataset.csv) for inspection.
Do not commit that file.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import SessionLocal  # noqa: E402
from app.ml.features import FEATURE_COLUMNS, TARGET, build_dataset, time_split  # noqa: E402

MIN_ROWS = 300
MIN_CV = 0.3   # std / mean of duration; below this the target is nearly constant


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="ds-")
    ap.add_argument("--out", default="data/timing_dataset.csv")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        df = build_dataset(db, name_prefix=args.prefix)
    finally:
        db.close()

    problems: list[str] = []

    print(f"\n1. rows: {len(df)}  (workflows: {df['workflow_id'].nunique() if len(df) else 0}, "
          f"tasks: {df['task_id'].nunique() if len(df) else 0})")
    if len(df) < MIN_ROWS:
        problems.append(f"only {len(df)} rows, want at least {MIN_ROWS}")
    if len(df) == 0:
        print("\nNo rows. Did the generator finish, and does the prefix match?")
        return 1

    d = df[TARGET]
    cv = d.std() / d.mean()
    print("\n2. duration_seconds")
    print(d.describe(percentiles=[0.1, 0.5, 0.9]).round(3).to_string())
    print(f"   coefficient of variation: {cv:.2f}")
    if cv < MIN_CV:
        problems.append(f"duration is nearly constant (CV {cv:.2f} < {MIN_CV})")
    print("\n   mean duration by command family:")
    print(df.groupby("cmd_family")[TARGET].agg(["count", "mean", "std"]).round(2).to_string())

    print("\n3. features")
    numeric = [c for c in FEATURE_COLUMNS if pd.api.types.is_numeric_dtype(df[c])]
    constant = [c for c in FEATURE_COLUMNS if df[c].nunique(dropna=True) <= 1]
    if constant:
        print(f"   constant (carry no signal): {', '.join(constant)}")
    # Spearman = Pearson on ranks. Done by hand because pandas' own
    # method="spearman" imports scipy, which is not a project dependency yet.
    d_rank = d.rank()
    corr = pd.DataFrame({
        "pearson": [df[c].corr(d) for c in numeric],
        "spearman": [df[c].rank().corr(d_rank) for c in numeric],
    }, index=numeric).dropna(how="all")
    corr = corr.reindex(corr["spearman"].abs().sort_values(ascending=False).index)
    print(corr.round(3).to_string())
    if not (corr["spearman"].abs() > 0.3).any():
        problems.append("no feature has |spearman| > 0.3 with duration")

    bad_wait = int((df["queue_wait_seconds"] < 0).sum())
    print(f"\n   queue_wait_seconds: median {df['queue_wait_seconds'].median():.3f}s, "
          f"max {df['queue_wait_seconds'].max():.3f}s, negative: {bad_wait}")
    if bad_wait:
        problems.append(f"{bad_wait} rows have negative queue wait (clock problem?)")

    print("\n4. trivial baseline on a time-ordered 80/20 split")
    train, test = time_split(df, 0.2)
    cols = ["cmd_sleep_s", "cmd_sum_n", "cmd_sort_n"]
    x_tr = np.column_stack([np.ones(len(train))] + [train[c] for c in cols])
    x_te = np.column_stack([np.ones(len(test))] + [test[c] for c in cols])
    coef, *_ = np.linalg.lstsq(x_tr, train[TARGET].to_numpy(), rcond=None)
    pred = x_te @ coef
    mae_mean = float(np.abs(test[TARGET] - train[TARGET].mean()).mean())
    mae_lin = float(np.abs(test[TARGET] - pred).mean())
    ss_res = float(((test[TARGET] - pred) ** 2).sum())
    ss_tot = float(((test[TARGET] - test[TARGET].mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    print(f"   train {len(train)} / test {len(test)}")
    print(f"   MAE predicting the train mean:      {mae_mean:.3f}s")
    print(f"   MAE of 3-feature least squares:     {mae_lin:.3f}s   (R2 {r2:.2f})")
    if mae_lin >= mae_mean:
        problems.append("a 3-feature linear fit does not beat predicting the mean")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"\nwrote {out}  (do not commit)")

    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nOK: enough rows, real spread, live features, baseline beats the mean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
