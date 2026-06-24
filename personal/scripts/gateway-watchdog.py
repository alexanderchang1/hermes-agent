#!/usr/bin/env python3
"""
Gateway watchdog — ties hermes-gateway lifecycle to the Slurm job.
When the parent Slurm batch job dies (timeout, preemption, user logout),
this script kills the hermes-gateway process so we don't leave orphaned
processes on the compute node.

Usage:
    python3 ~/.hermes/scripts/gateway-watchdog.py
"""

import os
import signal
import subprocess
import sys
import time

# How often to check (seconds)
POLL_INTERVAL = 2

def find_slurm_root_pid():
    """Find the Slurm batch script PID (the root ancestor of our session)."""
    # SLURM_TASK_PID is the slurm_script PID that started the job
    slurm_task_pid = os.environ.get("SLURM_TASK_PID")
    if slurm_task_pid:
        return int(slurm_task_pid)

    # Fallback: walk the process tree up to find slurm_script
    my_pid = os.getppid()
    for _ in range(20):
        try:
            with open(f"/proc/{my_pid}/comm") as f:
                comm = f.read().strip()
            if comm == "slurm_script":
                return my_pid
        except (FileNotFoundError, ProcessLookupError):
            # Parent already gone — job is dying
            return None
        # Walk up
        try:
            cmd = subprocess.run(["ps", "-o", "ppid=", "-p", str(my_pid)],
                                 capture_output=True, text=True)
            parent = cmd.stdout.strip()
            if not parent or int(parent) <= 1:
                break
            my_pid = int(parent)
        except (ValueError, subprocess.SubprocessError, FileNotFoundError):
            break

    return None

def find_gateway_pids():
    """Find all hermes gateway processes (NOT this watchdog or the CLI)."""
    pids = []
    try:
        cmd = subprocess.run(
            ["pgrep", "-f", "hermes.*gateway"],
            capture_output=True, text=True
        )
        for line in cmd.stdout.strip().split("\n"):
            pid = line.strip()
            if pid and int(pid) != os.getpid():
                pids.append(int(pid))
    except FileNotFoundError:
        pass
    return pids

def is_pid_alive(pid):
    """Check if a pid still exists."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False

def main():
    slurm_root = find_slurm_root_pid()
    if slurm_root is None:
        print("WARNING: Could not find Slurm root PID — no watchdog active", flush=True)
        sys.exit(0)

    print(f"Watchdog: monitoring Slurm job root PID {slurm_root}", flush=True)
    print(f"Watchdog: own PID {os.getpid()}", flush=True)

    while is_pid_alive(slurm_root):
        time.sleep(POLL_INTERVAL)

    # Slurm root is gone — kill gateway processes
    print(f"FATAL: Slurm root PID {slurm_root} died — terminating gateway")
    for pid in find_gateway_pids():
        try:
            print(f"  Killing gateway PID {pid}")
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    # Grace period — let gateway shut down cleanly
    time.sleep(3)

    # Force kill straggler
    for pid in find_gateway_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

if __name__ == "__main__":
    main()
