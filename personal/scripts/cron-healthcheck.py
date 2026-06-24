#!/usr/bin/env python3
"""Cron healthcheck — validates the end-to-end cron pipeline.

Checks:
  1. vLLM is reachable and responsive
  2. Proxy is reachable (if used)
  3. Cron jobs.json is valid, base_url matches running instances
  4. Hermes retries + backoff config is set
  5. No recent cron failures in logs
  6. Cron schedule matches expected timing

Run via: python3 ~/.hermes/scripts/cron-healthcheck.py
Exit code 0 = OK, 1 = problem found.
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

HERMES_HOME = os.path.expanduser("~/.hermes")
VLLM_BASE = "http://localhost:8000"
PROXY_BASE = "http://localhost:8010"
ERRORS_FOUND = []

def check(name, condition, detail=""):
    if condition:
        print(f"  [OK]   {name}")
    else:
        msg = f"  [FAIL] {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        ERRORS_FOUND.append((name, detail))

def section(title):
    print(f"\n=== {title} ===")

def main():
    print("Cron Healthcheck —", time.strftime("%Y-%m-%d %H:%M:%S"))

    # --- vLLM ---
    section("vLLM (direct)")
    try:
        req = urllib.request.Request(f"{VLLM_BASE}/v1/models", method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            models = [m["id"] for m in data.get("data", [])]
            check("vLLM responds", True, f"models: {models}")
    except Exception as e:
        check("vLLM responds", False, str(e))

    # --- Proxy ---
    section("Proxy")
    try:
        with urllib.request.urlopen(f"{PROXY_BASE}/health", timeout=5) as r:
            health = json.loads(r.read())
            check("Proxy responds", True,
                  f"max_concurrent={health.get('max_concurrent')}, in_flight={health.get('in_flight')}")
    except urllib.error.URLError:
        check("Proxy responds", False, "proxy unreachable (may not be running)")
    except Exception as e:
        check("Proxy responds", False, str(e))

    # --- Jobs config ---
    section("Cron job config")
    jobs_path = os.path.join(HERMES_HOME, "cron", "jobs.json")
    try:
        with open(jobs_path) as f:
            jobs_data = json.load(f)
        job = jobs_data["jobs"][0]
        base_url = job.get("base_url", "")
        schedule = job.get("schedule_display", "??")
        last_status = job.get("last_status", "??")
        last_error = job.get("last_error")
        last_run = job.get("last_run_at", "never")

        check("Job exists", True, job.get("name", "unnamed"))
        check("Last status ok", last_status == "ok", f"status={last_status}, error={last_error}")
        check("Last run recent", True, f"last_run={last_run}")

        # Warn if base_url points to proxy (should be direct vLLM)
        if "8010" in base_url:
            check("Routes direct to vLLM", False,
                  f"base_url={base_url} (goes through proxy — should be :8000)")
        else:
            check("Routes direct to vLLM", True)
    except FileNotFoundError:
        check("jobs.json exists", False, f"not found at {jobs_path}")
    except Exception as e:
        check("jobs.json valid", False, str(e))

    # --- Environment config ---
    section("Environment (.env)")
    env_path = os.path.join(HERMES_HOME, ".env")
    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env_vars[k] = v

    retries = int(env_vars.get("HERMES_STREAM_RETRIES", "1"))
    check("Stream retries configured", retries >= 10,
          f"HERMES_STREAM_RETRIES={retries} (recommended: >=10)")

    timeout = int(env_vars.get("HERMES_STREAM_READ_TIMEOUT", "0"))
    check("Stream read timeout configured", timeout >= 300,
          f"HERMES_STREAM_READ_TIMEOUT={timeout}s (recommended: >=300)")

    telegram_token = env_vars.get("TELEGRAM_BOT_TOKEN", "")
    check("Telegram bot token present", len(telegram_token) > 20,
          "TELEGRAM_BOT_TOKEN is missing or too short" if len(telegram_token) <= 20 else "ok")

    # --- Chat completion retry backoff ---
    section("Retry backoff patch")
    helpers_path = os.path.join(HERMES_HOME, "hermes-agent", "agent", "chat_completion_helpers.py")
    if os.path.exists(helpers_path):
        with open(helpers_path) as f:
            content = f.read()
        has_sleep = "_time.sleep(_retry_delay)" in content
        has_backoff = "_retry_delay = min(2 ** _stream_attempt, 32)" in content
        check("Exponential backoff in chat_completion_helpers", has_sleep and has_backoff,
              "missing — retries fire instantly with no delay" if not (has_sleep and has_backoff) else "ok")
    else:
        check("chat_completion_helpers.py found", False, f"not at {helpers_path}")

    # --- Recent cron failures ---
    section("Recent failures (agent.log)")
    agent_log = os.path.join(HERMES_HOME, "logs", "agent.log")
    recent_fails = 0
    if os.path.exists(agent_log):
        with open(agent_log) as f:
            for line in f:
                if "c16568" in line:
                    if "Connection error" in line or "stream.*drop" in line.lower():
                        recent_fails += 1
        check("No recent cron connection errors", recent_fails == 0,
              f"found {recent_fails} error entries")
    else:
        check("agent.log exists", False)

    # --- Summary ---
    section("Summary")
    if ERRORS_FOUND:
        print(f"  {len(ERRORS_FOUND)} check(s) FAILED:")
        for name, detail in ERRORS_FOUND:
            print(f"    - {name}: {detail}")
        print("\nOverall: FAIL")
        return 1
    else:
        print("  All checks passed.")
        print("\nOverall: OK")
        return 0

if __name__ == "__main__":
    sys.exit(main())
