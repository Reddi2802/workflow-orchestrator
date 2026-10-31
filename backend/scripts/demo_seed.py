"""
Create a small set of demo workflows through the public API, run them, and
print what to look at. Used for the Week 10 demo.

    uv run python scripts/demo_seed.py            # server must be running on :8000
    uv run python scripts/demo_seed.py --api http://localhost:8000

Creates three workflows (names start with "demo-"):
  demo-happy     diamond DAG, everything succeeds         -> SUCCESS
  demo-retry     one task always fails, 2 retries         -> 3 attempts (RETRYING, RETRYING, FAILED), downstream SKIPPED, run PARTIAL
  demo-partial   one branch fails, the other succeeds     -> PARTIAL
Re-running adds another copy; delete old ones with DELETE /api/v1/workflows/{id}.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def call(api: str, method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        api + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        sys.exit(f"{method} {path} -> {exc.code}: {exc.read().decode()[:300]}")
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach {api} ({exc.reason}). Start the server first.")


def py(code: str) -> str:
    # Same interpreter as this script, so it works on Windows and Linux alike.
    return f'"{sys.executable}" -c "{code}"'


OK = py("import time; time.sleep(1)")
BAD = py("import sys; sys.exit(1)")

WORKFLOWS = {
    "demo-happy": {
        "tasks": [
            {"name": "extract", "command": OK},
            {"name": "clean", "command": OK},
            {"name": "features", "command": OK},
            {"name": "publish", "command": OK},
        ],
        "dependencies": [
            {"upstream_task_name": "extract", "downstream_task_name": "clean"},
            {"upstream_task_name": "extract", "downstream_task_name": "features"},
            {"upstream_task_name": "clean", "downstream_task_name": "publish"},
            {"upstream_task_name": "features", "downstream_task_name": "publish"},
        ],
    },
    "demo-retry": {
        "tasks": [
            {"name": "fetch", "command": OK},
            {"name": "flaky", "command": BAD, "max_retries": 2, "retry_backoff_seconds": 1},
            {"name": "report", "command": OK},
        ],
        "dependencies": [
            {"upstream_task_name": "fetch", "downstream_task_name": "flaky"},
            {"upstream_task_name": "flaky", "downstream_task_name": "report"},
        ],
    },
    "demo-partial": {
        "tasks": [
            {"name": "start", "command": OK},
            {"name": "good-branch", "command": OK},
            {"name": "bad-branch", "command": BAD},
        ],
        "dependencies": [
            {"upstream_task_name": "start", "downstream_task_name": "good-branch"},
            {"upstream_task_name": "start", "downstream_task_name": "bad-branch"},
        ],
    },
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--timeout", type=int, default=90, help="seconds to wait for all runs to finish")
    args = ap.parse_args()

    runs = {}
    for name, spec in WORKFLOWS.items():
        wf = call(args.api, "POST", "/api/v1/workflows", {"name": name, **spec})
        run = call(args.api, "POST", f"/api/v1/workflows/{wf['id']}/runs")
        runs[name] = (wf["id"], run["id"])
        print(f"created {name}: workflow {wf['id']}, run {run['id']}")

    deadline = time.time() + args.timeout
    final = {}
    while time.time() < deadline and len(final) < len(runs):
        for name, (_, run_id) in runs.items():
            if name in final:
                continue
            status = call(args.api, "GET", f"/api/v1/runs/{run_id}")["status"]
            if status not in ("pending", "running"):
                final[name] = status
        time.sleep(1)

    expected = {"demo-happy": "success", "demo-retry": "partial", "demo-partial": "partial"}
    bad = 0
    for name, (wf_id, run_id) in runs.items():
        got = final.get(name, "still running")
        flag = "ok" if got == expected[name] else "UNEXPECTED"
        bad += got != expected[name]
        print(f"{name:<13} {got:<14} expected {expected[name]:<8} [{flag}]  http://localhost:5173 / run {run_id}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
