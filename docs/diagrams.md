# Diagrams

Mermaid, so GitHub renders them in place. Each one is drawn from the code, not from the
original spec; the source file is named under each diagram so it can be re-checked.

## 1. Entity-relationship diagram

Source: `backend/app/models/entities.py`. All foreign keys are `ON DELETE CASCADE`, so deleting a
`Workflow` removes its tasks, edges, runs and task runs.

```mermaid
erDiagram
    WORKFLOW ||--o{ TASK : has
    WORKFLOW ||--o{ WORKFLOW_RUN : has
    TASK ||--o{ TASK_DEPENDENCY : "is upstream of"
    TASK ||--o{ TASK_DEPENDENCY : "is downstream of"
    WORKFLOW_RUN ||--o{ TASK_RUN : has
    TASK ||--o{ TASK_RUN : "executed as"

    WORKFLOW {
        int id PK
        string name
        string description "nullable"
        string cron_expression "nullable"
        bool is_active
        int max_active_runs
        datetime created_at
        datetime updated_at
    }
    TASK {
        int id PK
        int workflow_id FK
        string name "unique with workflow_id"
        string command
        int max_retries
        int retry_backoff_seconds
        datetime created_at
    }
    TASK_DEPENDENCY {
        int id PK
        int upstream_task_id FK
        int downstream_task_id FK "unique pair, must differ"
    }
    WORKFLOW_RUN {
        int id PK
        int workflow_id FK
        enum status
        enum trigger_type
        datetime triggered_at
        datetime started_at "nullable"
        datetime completed_at "nullable"
    }
    TASK_RUN {
        int id PK
        int workflow_run_id FK
        int task_id FK
        enum status
        int attempt_number "unique with run and task"
        datetime created_at "nullable, UTC"
        datetime started_at "nullable"
        datetime completed_at "nullable"
        string error_message "nullable"
        json checkpoint_data "nullable"
    }
```

## 2. TaskRun state machine

Source: `VALID_TRANSITIONS` in `backend/app/engine/retry_policy.py`. `RETRYING` is terminal for a
row: a retry is a NEW row (next `attempt_number`) that starts at `PENDING`. This is what the
unique constraint on `(workflow_run_id, task_id, attempt_number)` requires.

```mermaid
stateDiagram-v2
    [*] --> PENDING: row created and enqueued
    PENDING --> RUNNING: worker claims it (atomic UPDATE)
    PENDING --> SKIPPED: an upstream task FAILED for good
    RUNNING --> SUCCESS: command exits 0
    RUNNING --> FAILED: exit != 0, no retries left
    RUNNING --> RETRYING: exit != 0, retries left
    RETRYING --> [*]: terminal, next attempt is a new row
    SUCCESS --> [*]
    FAILED --> [*]
    SKIPPED --> [*]
```

Crash recovery is the one deliberate exception: `recover_incomplete_runs` moves a `RUNNING` row
back to `PENDING` at the same attempt (the process died, so the attempt never really finished).
It bypasses `validate_transition` on purpose; see the `recovery.py` docstring.

## 3. WorkflowRun state machine

Source: `scheduler.py` (creation), `worker_pool.py` (`start_workflow_run`,
`_maybe_finalize_workflow_run`).

```mermaid
stateDiagram-v2
    [*] --> PENDING: cron tick or POST /workflows/{id}/runs
    PENDING --> RUNNING: dispatch loop starts it, root TaskRuns created
    PENDING --> SUCCESS: workflow has zero tasks (known_limitations 12)
    RUNNING --> SUCCESS: every task SUCCESS or SKIPPED
    RUNNING --> PARTIAL: at least one FAILED and at least one SUCCESS
    RUNNING --> FAILED: at least one FAILED and none SUCCESS
    SUCCESS --> [*]
    PARTIAL --> [*]
    FAILED --> [*]
```

A run is finalized only when every task's latest attempt is terminal (`SUCCESS`, `FAILED`,
`SKIPPED`). Cron will not start a new run while the number of `PENDING` + `RUNNING` runs is at
`max_active_runs`.

## 4. Life of one run

```mermaid
sequenceDiagram
    participant S as Scheduler loop
    participant DB as Postgres
    participant D as Dispatch loop
    participant Q as Redis queue
    participant W as Worker (x2)

    S->>DB: insert WorkflowRun (PENDING)
    D->>DB: find PENDING runs
    D->>DB: run -> RUNNING, insert root TaskRuns (PENDING)
    D->>Q: enqueue root task_run ids
    W->>Q: dequeue task_run id
    W->>DB: UPDATE ... WHERE status = PENDING (claim, rowcount 1 wins)
    W->>W: execute task command
    alt exit 0
        W->>DB: TaskRun -> SUCCESS, write checkpoint_data
        W->>DB: downstream tasks whose upstreams are all SUCCESS -> new PENDING rows
        W->>Q: enqueue them
    else exit != 0 and retries left
        W->>DB: TaskRun -> RETRYING
        W->>W: sleep for the backoff (blocks this worker)
        W->>DB: insert next attempt (PENDING)
        W->>Q: enqueue it
    else exit != 0 and no retries
        W->>DB: TaskRun -> FAILED, downstream tasks -> SKIPPED
    end
    W->>DB: all tasks terminal? set WorkflowRun SUCCESS / PARTIAL / FAILED
```

## 5. Components

```mermaid
flowchart LR
    UI[React dashboard] -->|HTTP /api/v1| API[FastAPI routers]
    API --> DB[(Postgres)]
    subgraph Process[one FastAPI process]
        API
        SCH[Scheduler loop]
        DIS[Dispatch loop]
        WK[Worker pool]
        PRED[Duration predictor]
    end
    SCH --> DB
    DIS --> DB
    DIS --> Q[(Redis queue)]
    WK <--> Q
    WK --> DB
    API --> PRED
    PRED -.reads.-> M[models/duration_model.joblib]
    REC[recover_incomplete_runs] -->|once at startup| DB
```
