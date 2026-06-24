#!/usr/bin/env python3
"""Pre-flight: wait for vLLM to be healthy before fleet supervisor cron proceeds.
If vLLM never comes up within MAX_WAIT, exit silently (no delivery).
stdout is injected into the agent prompt as context.
"""
import subprocess, time, sys

URL = "http://localhost:8000/v1"
MAX_WAIT = 60  # 1 minute — fail fast when vLLM is genuinely down
POLL_INTERVAL = 5  # seconds
max_attempts = MAX_WAIT // POLL_INTERVAL

for i in range(max_attempts):
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             f"{URL}/models", "--connect-timeout", "10", "--max-time", "20"],
            capture_output=True, text=True, timeout=25
        )
        code = result.stdout.strip()
        if code == "200":
            print(f"[VLLM_READY] after {i+1} attempts ({i*POLL_INTERVAL}s wait)")
            sys.exit(0)
    except subprocess.TimeoutExpired:
        pass
    if i < max_attempts - 1:
        time.sleep(POLL_INTERVAL)

# Timeout — vLLM never came up within MAX_WAIT.
# Exit 0 with empty output so _build_job_prompt returns None
# and the agent is silently skipped (no connection attempt, no error).
sys.exit(0)
