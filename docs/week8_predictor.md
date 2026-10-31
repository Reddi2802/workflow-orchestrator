# Week 8: task duration predictor

Predicts how long a task will take to execute, from its command, its place in the DAG,
the current load and (when it has run before) its own history. Read-only: nothing here
changes scheduling.

## Run it

```powershell
cd backend
uv sync
uv run python scripts/generate_timing_data.py      # needs the server running; skip if ds- data exists
uv run python scripts/train_predictor.py           # prints scores, saves models/duration_model.joblib
uv run uvicorn app.main:app --port 8000
curl http://localhost:8000/api/v1/predictor
```

The API reloads the model file when it changes; no restart after retraining.
`PREDICTOR_MODEL_PATH` overrides the file location. No model file -> the endpoints answer 503.

## Models

- **Cold-start model**: no history features. Used for tasks that never succeeded.
- **Full model**: adds count/mean of the task's earlier successful runs.
- Both are `HistGradientBoostingRegressor` pipelines. Baselines: global mean, previous mean.
- Split is by time (earliest 80% train, latest 20% holdout), never random.
- For tasks with history, training picks (on the holdout) whichever of previous-mean and the
  full model is better. On generated data previous-mean wins, so the API serves that.

Result on 222 generated rows (holdout 44): first-time tasks MAE 0.75s vs 1.45s for guessing
the average; tasks with history 0.07s using previous-mean. Re-run training to get your numbers.

## API contract (for the frontend)

`GET /api/v1/predictor` -> `trained_at, training_rows, holdout_rows, long_threshold_seconds,
long_quantile, history_strategy, holdout{model:{mae,rmse,r2,n}}`

`GET /api/v1/tasks/{id}/prediction` -> `{task_id, name, predicted_seconds, basis, likely_long, on_critical_path}`

`GET /api/v1/workflows/{id}/predictions`:

```json
{"workflow_id": 1082, "estimated_total_seconds": 9.376, "critical_path": [1822, 1825],
 "long_threshold_seconds": 4.26,
 "tasks": [{"task_id": 1822, "name": "t0", "predicted_seconds": 3.15, "basis": "history",
            "likely_long": false, "on_critical_path": true}]}
```

404 unknown id, 503 no model. `basis` is `history` or `cold_start`.

## Caveats

- Training data is synthetic: a few command families, repeated shapes. Real commands outside
  those families fall into `other` and will be predicted poorly. Do not quote the scores as general.
- "Previous mean wins" is a data finding, not a model failure: repeated identical commands.
  The ML only adds value for new tasks here.
- Target is execution time only. Queue wait and the 2-worker pool limit are ignored, so
  `estimated_total_seconds` is a lower bound for a busy system.
- `likely_long` is relative: predicted >= the 75th percentile of training durations.
- Model file is a joblib pickle tied to the scikit-learn version in `uv.lock`; retrain after
  upgrading. Never load a model file you did not train.
- The UI for showing predictions is not built; the contract above is for Paramash.
