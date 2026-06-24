#!/usr/bin/env python3
"""
Fleet supervisor — combined gate + formatter for cron no_agent mode.

Pre-flight: checks vLLM is healthy.
Then: runs the collector script, reads pacing.json, and formats a Telegram report.
If nothing changed → exit silently (empty stdout).
"""
import json
import os
import subprocess
import sys
import time

VAULT = "/ix1/alee/LO_LAB/Personal/Alexander_Chang/alc376/vault"
COLLECT_SCRIPT = os.path.join(VAULT, "lab/agents/supervisor/scripts/hermes-supervisor-collect.py")
PACING_PATH = os.path.join(VAULT, "lab/agents/supervisor/pacing.json")
TMUX_BIN = "/ihome/alee/alc376/tmux-build/install/bin/tmux"
TMUX_SOCKET = "/vast/alee/alc376/tunnel-runtime/tmux/tmux-157528/default"

MAX_WAIT = 120
POLL_INTERVAL = 10


def check_vllm():
    for i in range(MAX_WAIT // POLL_INTERVAL):
        try:
            r = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "http://localhost:8000/v1/models", "--connect-timeout", "10", "--max-time", "20"],
                capture_output=True, text=True, timeout=25,
            )
            if r.stdout.strip() == "200":
                return True
        except subprocess.TimeoutExpired:
            pass
        if i < MAX_WAIT // POLL_INTERVAL - 1:
            time.sleep(POLL_INTERVAL)
    return False


def read_pacing():
    try:
        with open(PACING_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def format_report(collect_json_str):
    """Parse collector JSON and format a Telegram fleet report."""
    try:
        data = json.loads(collect_json_str)
    except json.JSONDecodeError:
        return f"[ERROR] Collect script produced invalid JSON"

    fleet = data.get("fleet", {})
    state_changed = data.get("escalation_summaries", []) or data.get("_state_changed", False)
    slurm_jobs = data.get("slurm_jobs", [])
    warnings = data.get("warnings", [])

    # If nothing changed → silent
    if not state_changed and not fleet:
        print("[SILENT]")
        return

    lines = []
    lines.append("FLEET STATUS")

    # Per-window status
    for wid, info in sorted(fleet.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
        if isinstance(info, dict):
            wname = info.get("window_name", wid)
            base = wname.split("-")[0] if wname else wid
            state = info.get("state", "unknown")
            ctx = info.get("context_pct", "?")
            details = info.get("details", [])
            detail_str = details[0] if isinstance(details, list) and details else ""

            if state in ("working", "goal_active", "subagent_menu"):
                lines.append(f"W{wid} {base} {ctx}%: {state} - {detail_str}")
            elif state == "idle":
                lines.append(f"W{wid} {base} {ctx}%: idle")
            elif state == "blocked_waiting":
                lines.append(f"W{wid} {base} {ctx}%: BLOCKED - {detail_str}")
            elif state == "needs_approval":
                lines.append(f"W{wid} {base} {ctx}%: NEEDS APPROVAL - {detail_str}")
            else:
                lines.append(f"W{wid} {base} {ctx}%: {state} - {detail_str}")

    # Escalations
    if state_changed and isinstance(state_changed, list):
        for esc in state_changed:
            if isinstance(esc, str) and esc.strip():
                lines.append(esc.strip())

    # SLURM summary
    if slurm_jobs:
        lines.append(f"SLURM: {len(slurm_jobs)} job(s) tracked")
        for job in slurm_jobs[:5]:
            if isinstance(job, dict):
                jid = job.get("job_id", "?")
                jstate = job.get("state", "?")
                lines.append(f"  Job {jid}: {jstate}")

    # Warnings
    for w in warnings[:3]:
        lines.append(f"WARNING: {w}")

    return "\n".join(lines)


def main():
    os.environ["TMUX"] = TMUX_SOCKET
    os.environ["TMUX_BIN"] = TMUX_BIN

    # Check vLLM
    if not check_vllm():
        # vLLM is down — exit silently (no delivery)
        sys.exit(0)

    # Run collector
    env = os.environ.copy()
    env["TMUX"] = TMUX_SOCKET
    result = subprocess.run(
        ["python3", COLLECT_SCRIPT, "--json"],
        capture_output=True, text=True, timeout=60, env=env,
    )

    if result.returncode != 0:
        # Collector failed — still report the error
        stderr_snippet = result.stderr[:200].strip()
        print(f"[ERROR] Collect script failed (exit {result.returncode}): {stderr_snippet}")
        return

    collect_json = result.stdout.strip()
    if not collect_json:
        print("[SILENT]")
        return

    # Format report
    report = format_report(collect_json)
    if report == "[SILENT]":
        print("[SILENT]")
        return

    # Read and update pacing
    pacing = read_pacing()
    if pacing:
        recommended = pacing.get("recommended_interval", "30m")
        if recommended and recommended != "30m":
            print(f"[SCHEDULE:{recommended}]")

    print(report)


if __name__ == "__main__":
    main()
