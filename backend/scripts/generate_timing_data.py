"""
Generate timing data for the bottleneck predictor by running real workflows
through the real API and engine.

Prerequisites (from backend/):
    docker compose up -d
    uv run alembic upgrade head
    uv run uvicorn app.main:app --port 8000        # no --reload, leave it running

Then, in a second terminal:
    uv run python scripts/generate_timing_data.py --workflows 3 --runs-per-workflow 2 --max-task-seconds 2   # ~1 min smoke test
    uv run python scripts/generate_timing_data.py                                                           # full run, ~10-15 min

What it does: creates workflows with varied DAG shapes (chain, diamond,
fan-out, fan-in) whose tasks run commands of four families (sleep, CPU loop,
sort, sleep + CPU) with random parameters, triggers each workflow several
times, and waits for every run to finish. Several workflows run at the same
time so tasks compete for the 2 workers and for CPU, which is the effect the
predictor is supposed to learn about.

A few workflows (--failure-rate) end in a task that always fails, so retries,
FAILED and SKIPPED rows exist in the data and the dataset code is exercised
on them. Only SUCCESS rows end up in the dataset.

Command parameters are scaled using a quick timing calibration of THIS
machine so task durations land around the requested range. The calibration
only picks parameters; it does not give the model a duration.

All workflows are named "<prefix>-<seed>-<n>-<shape>" so build_dataset can
select exactly these rows with name_prefix="<prefix>-".
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

PY = sys.executable
TERMINAL = {"success", "failed", "partial"}
WORKER_POOL_SIZE = 2   # app.engine.worker_pool.DEFAULT_WORKER_POOL_SIZE


# --- HTTP -------------------------------------------------------------------

def call(api: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        api + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path} -> {exc.code}: {exc.read().decode()[:300]}") from exc


# --- calibration and commands ---------------------------------------------------

def calibrate() -> tuple[float, float]:
    """Seconds per unit for `sum(range(n))` and per element for the sort
    command, measured here (best of 3)."""
    def best(fn, n):
        times = []
        for _ in range(3):
            t = time.perf_counter()
            fn(n)
            times.append(time.perf_counter() - t)
        return min(times) / n

    rng = random.Random(0)
    per_sum = best(lambda n: sum(range(n)), 5_000_000)
    per_sort = best(lambda n: sorted(rng.random() for _ in range(n)), 200_000)
    return per_sum, per_sort


def make_command(rng: random.Random, target_s: float, per_sum: float, per_sort: float) -> tuple[str, float]:
    """Returns (shell command, intended seconds). Double quotes only around
    the interpreter path and the -c body, so it works under cmd.exe."""
    family = rng.choice(["sleep", "cpu_sum", "sort", "sleep_cpu"])
    if family == "sleep":
        s = round(target_s, 2)
        return f'"{PY}" -c "import time; time.sleep({s})"', s
    if family == "cpu_sum":
        n = max(1000, int(target_s / per_sum))
        return f'"{PY}" -c "sum(range({n}))"', target_s
    if family == "sort":
        n = max(1000, int(target_s / per_sort))
        return (
            f'"{PY}" -c "import random; sorted(random.random() for _ in range({n}))"',
            target_s,
        )
    s = round(target_s * rng.uniform(0.3, 0.7), 2)
    n = max(1000, int((target_s - s) / per_sum))
    return f'"{PY}" -c "import time; time.sleep({s}); sum(range({n}))"', target_s


FAIL_COMMAND = f'"{PY}" -c "import sys; sys.exit(1)"'


# --- workflow shapes ----------------------------------------------------------------

def shape_edges(shape: str, rng: random.Random) -> tuple[int, list[tuple[int, int]]]:
    """(number of tasks, edges as index pairs). The LAST task is always a
    leaf, so making it fail never changes which other tasks run."""
    if shape == "chain":
        n = rng.randint(3, 5)
        return n, [(i, i + 1) for i in range(n - 1)]
    if shape == "diamond":
        return 4, [(0, 1), (0, 2), (1, 3), (2, 3)]
    if shape == "fan_out":
        k = rng.randint(3, 4)
        return k + 1, [(0, i) for i in range(1, k + 1)]
    if shape == "fan_in":
        k = rng.randint(3, 4)
        return k + 1, [(i, k) for i in range(k)]
    raise ValueError(shape)


def build_workflow(i, args, rng, per_sum, per_sort):
    shape = rng.choice(["chain", "diamond", "fan_out", "fan_in"])
    n, edges = shape_edges(shape, rng)
    fails = rng.random() < args.failure_rate

    tasks, planned = [], 0.0
    for t in range(n):
        if fails and t == n - 1:
            command, seconds = FAIL_COMMAND, 0.3
            retries = 1
        else:
            target = rng.uniform(0.8, args.max_task_seconds)
            command, seconds = make_command(rng, target, per_sum, per_sort)
            retries = rng.choice([0, 0, 1])
        planned += seconds
        tasks.append({
            "name": f"t{t}", "command": command,
            "max_retries": retries, "retry_backoff_seconds": 1,
        })

    payload = {
        "name": f"{args.prefix}-{args.seed}-{i:03d}-{shape}",
        "description": "generated by scripts/generate_timing_data.py",
        "cron_expression": None,
        "is_active": True,
        "max_active_runs": 1,
        "tasks": tasks,
        "dependencies": [
            {"upstream_task_name": f"t{u}", "downstream_task_name": f"t{d}"} for u, d in edges
        ],
    }
    return payload, planned


# --- running --------------------------------------------------------------------------

def run_workflow(api, payload, runs, timeout_s):
    wf = call(api, "POST", "/api/v1/workflows", payload)
    results = []
    for _ in range(runs):
        run = call(api, "POST", f"/api/v1/workflows/{wf['id']}/runs")
        deadline = time.time() + timeout_s
        while True:
            detail = call(api, "GET", f"/api/v1/runs/{run['id']}")
            if detail["status"] in TERMINAL:
                break
            if time.time() > deadline:
                raise RuntimeError(f"run {run['id']} did not finish within {timeout_s}s")
            time.sleep(0.5)
        results.append(detail["status"])
    return payload["name"], results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--workflows", type=int, default=36)
    ap.add_argument("--runs-per-workflow", type=int, default=3)
    ap.add_argument("--parallel", type=int, default=3, help="workflows running at the same time")
    ap.add_argument("--max-task-seconds", type=float, default=5.0)
    ap.add_argument("--failure-rate", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--prefix", default="ds")
    ap.add_argument("--run-timeout", type=float, default=300.0)
    args = ap.parse_args()

    try:
        call(args.api, "GET", "/health")
    except Exception as exc:   # noqa: BLE001
        print(f"Cannot reach the API at {args.api}: {exc}\nStart it first: uv run uvicorn app.main:app --port 8000")
        return 1

    rng = random.Random(args.seed)
    per_sum, per_sort = calibrate()
    print(f"calibration: {per_sum * 1e6:.3f} us per sum() step, {per_sort * 1e6:.3f} us per sorted element")

    workflows = [build_workflow(i, args, rng, per_sum, per_sort) for i in range(args.workflows)]
    task_runs = sum(len(p["tasks"]) for p, _ in workflows) * args.runs_per_workflow
    work_s = sum(planned for _, planned in workflows) * args.runs_per_workflow
    print(
        f"{len(workflows)} workflows x {args.runs_per_workflow} runs = about {task_runs} task executions; "
        f"roughly {work_s / WORKER_POOL_SIZE / 60:.1f} min of work for {WORKER_POOL_SIZE} workers "
        f"(real time is longer: dispatch polling, interpreter startup, retries; "
        f"{args.parallel} workflows at a time)"
    )

    done = failed = 0
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = [pool.submit(run_workflow, args.api, p, args.runs_per_workflow, args.run_timeout) for p, _ in workflows]
        for fut in as_completed(futures):
            try:
                name, statuses = fut.result()
                done += 1
                print(f"[{done + failed}/{len(workflows)}] {name}: {', '.join(statuses)}")
            except Exception as exc:   # noqa: BLE001
                failed += 1
                print(f"[{done + failed}/{len(workflows)}] FAILED to complete: {exc}")

    print(f"finished: {done} workflows completed, {failed} errored")
    print("next: uv run python scripts/check_timing_dataset.py")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
