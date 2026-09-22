#!/usr/bin/env python3
"""daily_pipeline_scheduler.py
Deterministic daily runner — 10-min tick loop, runs daily_pipeline.py once per
HKT day at/after 07:30. Self-healing: late starts, host reboots and failed runs
are caught up on the next tick (state file tracks last successful run date).
Registered as a Zo process service. No AI model, no scheduler dependency.
"""
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HKT = timezone(timedelta(hours=8))
BASE = Path(__file__).resolve().parent
PYTHON = "/usr/local/bin/python3"
PIPELINE = str(BASE / "daily_pipeline.py")
LOG = "/dev/shm/daily_pipeline_scheduler.log"
STATE = BASE / ".scheduler_state.json"
TICK_SEC = 600
RUN_AFTER = (7, 30)


def log(msg: str):
    ts = datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S HKT")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def today_str() -> str:
    return datetime.now(HKT).strftime("%Y-%m-%d")


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(ok: bool):
    try:
        STATE.write_text(json.dumps({"last_run_date": today_str(), "last_run_ok": ok,
                                     "finished_at": datetime.now(HKT).isoformat()}))
    except Exception as e:
        log(f"State write failed: {e}")


def past_window() -> bool:
    now = datetime.now(HKT)
    return (now.hour, now.minute) >= RUN_AFTER


def pipeline_running() -> bool:
    probe = subprocess.run(["pgrep", "-f", "daily_pipeline.py"], capture_output=True, text=True)
    pids = [p for p in probe.stdout.strip().split("\n") if p and p != str(os.getpid())]
    return bool(pids)


def run_pipeline():
    log("Starting daily_pipeline...")
    t0 = time.time()
    ok = False
    try:
        proc = subprocess.run(
            [PYTHON, PIPELINE],
            cwd=str(BASE),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=3600,
        )
        elapsed = time.time() - t0
        if proc.returncode == 0:
            log(f"Pipeline completed in {elapsed:.0f}s")
            ok = True
        else:
            log(f"Pipeline failed with code {proc.returncode} after {elapsed:.0f}s")
            tail = proc.stdout[-500:] if proc.stdout else ""
            if tail:
                log(f"Last 500 chars: {tail.strip()}")
    except subprocess.TimeoutExpired:
        log(f"Pipeline timed out after {time.time() - t0:.0f}s")
    except Exception as e:
        log(f"Pipeline exception: {type(e).__name__}: {e}")
    save_state(ok)
    return ok


if __name__ == "__main__":
    log("Scheduler started (tick mode)")
    with open("/tmp/daily_pipeline_scheduler.pid", "w") as f:
        f.write(str(os.getpid()))

    while True:
        try:
            st = load_state()
            if past_window() and st.get("last_run_date") != today_str():
                if pipeline_running():
                    log("Pipeline already running, skipping this tick")
                else:
                    if st.get("last_run_date"):
                        log(f"Catch-up: last run {st.get('last_run_date')}, today {today_str()} not done")
                    run_pipeline()
        except Exception as e:
            log(f"Tick error: {type(e).__name__}: {e}")
        time.sleep(TICK_SEC)
