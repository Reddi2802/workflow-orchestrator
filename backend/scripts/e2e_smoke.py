"""
End-to-end smoke test for the whole pipeline, run against a LIVE server.

Week 7 (joint integration). Drives the real HTTP API the way the dashboard
does, lets the real engine (dispatch loop, 2 workers, Redis) execute real
commands, and checks the final state in the database through the API.

Prerequisites (from backend/):
    docker compose up -d
    uv run alembic upgrade head
    uv run uvicorn app.main:app --port 8000        # no --reload, leave it running

Then, in a second terminal (from backend/):
    uv run python scripts/e2e_smoke.py                    # all scenarios except 'crash'
    uv run python scripts/e2e_smoke.py --skip cron        # skip the ~1-2 minute cron wait
    uv run python scripts/e2e_smoke.py --only diamond,failure_skip
    uv run python scripts/e2e_smoke.py --only crash       # guided: you kill the server

Scenarios (what each one proves):
    diamond          a -> b,c -> d runs in dependency order, run ends SUCCESS
    failure_skip     a failing task FAILS, its downstream is SKIPPED, run ends PARTIAL
    retry_success    attempt 1 fails -> RETRYING, attempt 2 succeeds, run ends SUCCESS
    max_active_runs a second trigger while one run is in flight gets 409
    empty_workflow   triggering a workflow with no tasks gets 400
    cron             a cron workflow gets a run with trigger_type=cron, which finishes
    crash            (only with --only crash) hard-kill the server mid-task, restart it,
                     the task re-runs at the SAME attempt_number and the run finishes

Everything it creates is named "e2e-...". The cron workflow is deactivated at
the end so it does not keep firing every minute.

Exit code 0 = every scenario passed, 1 = at least one failed.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

PY = sys.executable
TERMINAL_RUN = {"success", "failed", "partial"}
RUN_ID = uuid.uuid4().hex[:6]


# --- tiny HTTP client (stdlib only) ---------------------------------------------

class ServerDown(Exception):
    pass


def http(api: str, method: str, path: str, body: dict | None = None):
    """Returns (status_code, parsed_json_or_None). Never raises on 4xx/5xx;
    raises ServerDown if the server cannot be reached at all."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        api + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
        raise ServerDown(str(exc)) from exc


class Check(Exception):
    """A scenario assertion failed."""


def expect(cond: bool, message: str) -> None:
    if not cond:
        raise Check(message)


# --- helpers --------------------------------------------------------------------

def cmd(code: str) -> str:
    """A python one-liner as a shell command. Double quotes only around the
    interpreter path and the -c body, so it works under cmd.exe too."""
    return f'"{PY}" -c "{code}"'


def sleep_cmd(seconds: float) -> str:
    return cmd(f"import time; time.sleep({seconds})")


FAIL_CMD = cmd("import sys; sys.exit(1)")


def create_workflow(api, name, tasks, deps=(), max_active_runs=1, cron=None):
    status, wf = http(api, "POST", "/api/v1/workflows", {
        "name": f"e2e-{RUN_ID}-{name}",
        "description": "created by scripts/e2e_smoke.py",
        "cron_expression": cron,
        "is_active": True,
        "max_active_runs": max_active_runs,
        "tasks": tasks,
        "dependencies": [{"upstream_task_name": u, "downstream_task_name": d} for u, d in deps],
    })
    expect(status == 201, f"create workflow {name}: expected 201, got {status}: {wf}")
    return wf


def task(name, command, max_retries=0, backoff=1):
    return {"name": name, "command": command, "max_retries": max_retries,
            "retry_backoff_seconds": backoff}


def trigger(api, workflow_id):
    return http(api, "POST", f"/api/v1/workflows/{workflow_id}/runs")


def wait_run(api, run_id, timeout=90, tolerate_down=False):
    deadline = time.time() + timeout
    while True:
        try:
            status, detail = http(api, "GET", f"/api/v1/runs/{run_id}")
            if status == 200 and detail["status"] in TERMINAL_RUN:
                return detail
        except ServerDown:
            if not tolerate_down:
                raise
        if time.time() > deadline:
            raise Check(f"run {run_id} did not finish within {timeout}s")
        time.sleep(0.5)


def rows_by_task_name(api, workflow, detail):
    """{task name: [task_run, ...] ordered by attempt}"""
    id_to_name = {t["id"]: t["name"] for t in workflow["tasks"]}
    out: dict[str, list] = {}
    for tr in detail["task_runs"]:
        out.setdefault(id_to_name[tr["task_id"]], []).append(tr)
    for rows in out.values():
        rows.sort(key=lambda r: r["attempt_number"])
    return out


def ts(value: str | None) -> datetime:
    return datetime.fromisoformat(value)


def run_one(api, name, tasks, deps=(), **kw):
    wf = create_workflow(api, name, tasks, deps, **kw)
    status, run = trigger(api, wf["id"])
    expect(status == 201, f"trigger: expected 201, got {status}: {run}")
    detail = wait_run(api, run["id"])
    return wf, detail, rows_by_task_name(api, wf, detail)


# --- scenarios ----------------------------------------------------------------------

def diamond(api, _):
    tasks = [task(n, sleep_cmd(0.5)) for n in "abcd"]
    deps = [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]
    wf, detail, rows = run_one(api, "diamond", tasks, deps)

    expect(detail["status"] == "success", f"run status {detail['status']}, expected success")
    for n in "abcd":
        expect(len(rows[n]) == 1 and rows[n][0]["status"] == "success",
               f"task {n}: {[(r['attempt_number'], r['status']) for r in rows[n]]}")
    a, b, c, d = (rows[n][0] for n in "abcd")
    expect(ts(b["started_at"]) >= ts(a["completed_at"]), "b started before a finished")
    expect(ts(c["started_at"]) >= ts(a["completed_at"]), "c started before a finished")
    expect(ts(d["started_at"]) >= max(ts(b["completed_at"]), ts(c["completed_at"])),
           "d started before both b and c finished")
    expect(detail["completed_at"] is not None, "run has no completed_at")


def failure_skip(api, _):
    tasks = [task("a", sleep_cmd(0.3)), task("b", FAIL_CMD), task("c", sleep_cmd(0.3))]
    wf, detail, rows = run_one(api, "failure-skip", tasks, [("a", "b"), ("b", "c")])

    expect(rows["a"][0]["status"] == "success", f"a: {rows['a'][0]['status']}")
    expect(rows["b"][0]["status"] == "failed", f"b: {rows['b'][0]['status']}")
    expect(rows["b"][0]["error_message"] is not None, "failed task has no error_message")
    expect(rows["c"][0]["status"] == "skipped", f"c: {rows['c'][0]['status']}")
    expect(detail["status"] == "partial", f"run status {detail['status']}, expected partial")


def retry_success(api, _):
    marker = Path(tempfile.gettempdir()) / f"e2e-retry-{RUN_ID}.marker"
    marker.unlink(missing_ok=True)
    # Fails the first time it runs (marker absent), succeeds the second time.
    flaky = cmd(
        f"import os,sys; f=r'{marker}'; ok=os.path.exists(f); open(f,'w').close(); sys.exit(0 if ok else 1)"
    )
    try:
        wf, detail, rows = run_one(api, "retry", [task("flaky", flaky, max_retries=1, backoff=1)])
    finally:
        marker.unlink(missing_ok=True)

    attempts = [(r["attempt_number"], r["status"]) for r in rows["flaky"]]
    expect(attempts == [(1, "retrying"), (2, "success")], f"attempts: {attempts}")
    gap = (ts(rows["flaky"][1]["started_at"]) - ts(rows["flaky"][0]["completed_at"])).total_seconds()
    expect(gap >= 0.9, f"attempt 2 started only {gap:.2f}s after attempt 1 failed; backoff is 1s")
    expect(detail["status"] == "success", f"run status {detail['status']}, expected success")


def max_active_runs(api, _):
    wf = create_workflow(api, "max-active", [task("slow", sleep_cmd(3))], max_active_runs=1)
    s1, run1 = trigger(api, wf["id"])
    expect(s1 == 201, f"first trigger: {s1} {run1}")
    s2, body = trigger(api, wf["id"])
    expect(s2 == 409, f"second trigger while one is active: expected 409, got {s2}: {body}")
    wait_run(api, run1["id"])
    s3, run3 = trigger(api, wf["id"])
    expect(s3 == 201, f"trigger after the first run finished: expected 201, got {s3}: {run3}")
    wait_run(api, run3["id"])


def empty_workflow(api, _):
    wf = create_workflow(api, "empty", [])
    status, body = trigger(api, wf["id"])
    expect(status == 400, f"empty workflow trigger: expected 400, got {status}: {body}")


def cron(api, args):
    wf = create_workflow(api, "cron", [task("tick", sleep_cmd(0.3))], cron="* * * * *")
    try:
        deadline = time.time() + args.cron_timeout
        run = None
        while time.time() < deadline:
            status, runs = http(api, "GET", f"/api/v1/workflows/{wf['id']}/runs")
            if status == 200 and runs:
                run = runs[0]
                break
            time.sleep(3)
        expect(run is not None, f"no cron run appeared within {args.cron_timeout}s")
        expect(run["trigger_type"] == "cron", f"trigger_type {run['trigger_type']}, expected cron")
        detail = wait_run(api, run["id"])
        expect(detail["status"] == "success", f"cron run status {detail['status']}")
    finally:
        # Stop it firing every minute from now on.
        http(api, "PUT", f"/api/v1/workflows/{wf['id']}", {"is_active": False})


def crash(api, args):
    wf = create_workflow(api, "crash", [task("long", sleep_cmd(25))])
    status, run = trigger(api, wf["id"])
    expect(status == 201, f"trigger: {status} {run}")

    # Wait until the task is really RUNNING.
    first_start = None
    deadline = time.time() + 30
    while time.time() < deadline:
        _, detail = http(api, "GET", f"/api/v1/runs/{run['id']}")
        running = [t for t in detail["task_runs"] if t["status"] == "running"]
        if running:
            first_start = running[0]["started_at"]
            break
        time.sleep(0.5)
    expect(first_start is not None, "the task never reached RUNNING")

    print("\n  >>> The task is RUNNING now. Hard-kill the server within 20 seconds:")
    print("  >>>   PowerShell:  Get-NetTCPConnection -LocalPort 8000 | Select-Object OwningProcess")
    print("  >>>                Stop-Process -Id <OwningProcess> -Force")
    print("  >>>   (NOT Ctrl+C: that fails the task instead, known_limitations item 11)")
    print("  >>> Then start it again:  uv run uvicorn app.main:app --port 8000")
    print(f"  >>> Waiting up to {args.crash_timeout}s for the run to finish after the restart...\n")

    detail = wait_run(api, run["id"], timeout=args.crash_timeout, tolerate_down=True)
    rows = rows_by_task_name(api, wf, detail)["long"]
    expect(detail["status"] == "success", f"run status {detail['status']}, expected success")
    expect(len(rows) == 1, f"expected 1 TaskRun row, got {len(rows)} (a crash must not create a new attempt)")
    expect(rows[0]["attempt_number"] == 1, f"attempt_number {rows[0]['attempt_number']}, expected 1")
    expect(rows[0]["status"] == "success", f"task status {rows[0]['status']}")
    expect(rows[0]["started_at"] != first_start,
           "started_at did not change, so the task does not look like it was re-run after the restart "
           "(did you kill the server before the task finished?)")


SCENARIOS = {
    "diamond": diamond,
    "failure_skip": failure_skip,
    "retry_success": retry_success,
    "max_active_runs": max_active_runs,
    "empty_workflow": empty_workflow,
    "cron": cron,
    "crash": crash,
}
DEFAULT = ["diamond", "failure_skip", "retry_success", "max_active_runs", "empty_workflow", "cron"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--only", help="comma-separated scenario names")
    ap.add_argument("--skip", help="comma-separated scenario names to skip")
    ap.add_argument("--cron-timeout", type=float, default=150.0)
    ap.add_argument("--crash-timeout", type=float, default=180.0)
    args = ap.parse_args()

    names = args.only.split(",") if args.only else list(DEFAULT)
    if args.skip:
        names = [n for n in names if n not in args.skip.split(",")]
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f"unknown scenario(s): {', '.join(unknown)}. Choose from: {', '.join(SCENARIOS)}")
        return 2

    try:
        http(args.api, "GET", "/health")
    except ServerDown as exc:
        print(f"Cannot reach the API at {args.api}: {exc}\nStart it first: uv run uvicorn app.main:app --port 8000")
        return 2

    results = []
    for name in names:
        print(f"-- {name} ...", flush=True)
        started = time.time()
        try:
            SCENARIOS[name](args.api, args)
            results.append((name, True, "", time.time() - started))
            print(f"   PASS ({time.time() - started:.1f}s)")
        except Check as exc:
            results.append((name, False, str(exc), time.time() - started))
            print(f"   FAIL: {exc}")
        except ServerDown as exc:
            results.append((name, False, f"server unreachable: {exc}", time.time() - started))
            print(f"   FAIL: server unreachable: {exc}")

    print("\nsummary")
    for name, ok, why, secs in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<16} {secs:5.1f}s  {why}")
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
