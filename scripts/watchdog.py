"""Watchdog for a vLLM server: starts it, sends a periodic canary, restarts it on failure.

Known failure (seen with Qwen3.6-27B on vLLM): the output collapses to "!!!!" and stays that
way for every later request; the server does not recover by itself. Two signals trigger a
restart: (1) the canary fails twice in a row, (2) the client wrote a new line to the
degenerate-output marker file. Every event is appended to logs/incidents.jsonl.

No try/except: the canary runs in a child process and its exit code is the health signal.

Run: python scripts/watchdog.py model1 --launch scripts/vllm_model1.sh
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from feecalc import llm

STARTUP_TIMEOUT_S = 1200
CANARY_TIMEOUT_S = 120
FAILURES_BEFORE_RESTART = 2


def log(path: Path, event: str, **fields: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False), flush=True)


def start_server(launch: Path, server_log: Path) -> subprocess.Popen:
    out = open(server_log, "ab")
    return subprocess.Popen([str(launch)], stdout=out, stderr=subprocess.STDOUT, start_new_session=True)


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + 30
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.5)
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def run_canary(model_key: str) -> tuple[bool, str]:
    """Canary in a child process -> (healthy, last lines of its output)."""
    child = subprocess.Popen([sys.executable, "-m", "feecalc.llm", "canary", model_key],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.monotonic() + CANARY_TIMEOUT_S
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    if child.poll() is None:
        child.kill()
        child.wait()
        return False, f"canary timed out after {CANARY_TIMEOUT_S}s"
    output = (child.stdout.read() if child.stdout else "").strip()
    return child.returncode == 0, "\n".join(output.splitlines()[-3:])


def wait_until_ready(proc: subprocess.Popen, m: llm.Model, model_key: str) -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if llm.port_open(m) and run_canary(model_key)[0]:
            return True
        time.sleep(5)
    return False


def marker_lines(model_key: str) -> int:
    path = llm.marker_file()
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines()
               if line.strip() and json.loads(line)["model"] == model_key)


def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("model", choices=["model1", "model2"])
    a.add_argument("--launch", type=Path, required=True)
    a.add_argument("--interval", type=float, default=60.0)
    a.add_argument("--incidents", type=Path, default=Path("logs/incidents.jsonl"))
    arg = a.parse_args()
    m = llm.model1() if arg.model == "model1" else llm.model2()
    server_log = Path("logs") / f"vllm_{arg.model}.log"
    server_log.parent.mkdir(parents=True, exist_ok=True)
    if llm.port_open(m):
        sys.exit(f"{m.url} is already in use: stop that server first, the watchdog must own the process")

    proc: subprocess.Popen | None = None
    failures = 0
    seen_markers = marker_lines(arg.model)
    while True:
        if proc is None or proc.poll() is not None:
            if proc is not None:
                log(arg.incidents, "server_exited", model=arg.model, code=proc.returncode)
            proc = start_server(arg.launch, server_log)
            log(arg.incidents, "server_starting", model=arg.model, pid=proc.pid)
            started = time.monotonic()
            ready = wait_until_ready(proc, m, arg.model)
            log(arg.incidents, "server_ready" if ready else "server_start_failed", model=arg.model,
                seconds=round(time.monotonic() - started, 1))
            if not ready:
                stop_server(proc)
                time.sleep(10)
            failures = 0
            continue
        time.sleep(arg.interval)
        reason = ""
        markers = marker_lines(arg.model)
        if markers > seen_markers:
            reason = f"degenerate output reported by client ({markers - seen_markers} new marker line(s))"
            seen_markers = markers
        else:
            healthy, tail = run_canary(arg.model)
            failures = 0 if healthy else failures + 1
            if not healthy:
                log(arg.incidents, "canary_failed", model=arg.model, consecutive=failures, output=tail)
            if failures >= FAILURES_BEFORE_RESTART:
                reason = f"canary failed {failures} times in a row"
        if reason:
            log(arg.incidents, "restarting", model=arg.model, reason=reason)
            stop_server(proc)
            proc = None


if __name__ == "__main__":
    main()
