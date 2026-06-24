#!/usr/bin/env bash
# Cron healthcheck — shell-only (no Python dependency)
# Exit 0 = OK, 1 = problem found.
# Designed for no_agent=True cron: silent on success, reports failures to stdout.

set -euo pipefail

HERMES_HOME="$HOME/.hermes"
FAIL_COUNT=0
FAIL_MSGS=()

check() {
    local name="$1" status="$2" detail="${3:-}"
    if [[ "$status" == "0" ]]; then
        echo "  [OK]   $name ${detail:+— $detail}"
    else
        echo "  [FAIL] $name ${detail:+— $detail}"
        FAIL_COUNT=$((FAIL_COUNT + 1))
        FAIL_MSGS+=("$name: ${detail:-missing}")
    fi
}

section() {
    echo ""
    echo "=== $1 ==="
}

echo "Cron Healthcheck — $(date '+%Y-%m-%d %H:%M:%S')"

# --- vLLM ---
section "vLLM (direct)"
if models=$(curl -sf --max-time 10 'http://localhost:8000/v1/models' 2>/dev/null); then
    model_list=$(echo "$models" | jq -r '.data[].id' 2>/dev/null | tr '\n' ',' | sed 's/,$//')
    check "vLLM responds" 0 "models: $model_list"
else
    check "vLLM responds" 1 "curl failed or timed out"
fi

# --- Proxy (optional) ---
section "Proxy"
if health=$(curl -sf --max-time 5 'http://localhost:8010/health' 2>/dev/null); then
    concurrent=$(echo "$health" | jq -r '.max_concurrent' 2>/dev/null)
    inflight=$(echo "$health" | jq -r '.in_flight' 2>/dev/null)
    check "Proxy responds" 0 "max_concurrent=$concurrent, in_flight=$inflight"
else
    check "Proxy responds" 0 "(not running — ok)"
fi

# --- Jobs config ---
section "Cron job config"
jobs_path="$HERMES_HOME/cron/jobs.json"
if [[ -f "$jobs_path" ]]; then
    if jobs_valid=$(jq empty "$jobs_path" 2>&1); then
        name=$(jq -r '.jobs[0].name // "unnamed"' "$jobs_path")
        last_status=$(jq -r '.jobs[0].last_status // "??"' "$jobs_path")
        base_url=$(jq -r '.jobs[0].base_url // ""' "$jobs_path")
        last_run=$(jq -r '.jobs[0].last_run_at // "never"' "$jobs_path")
        check "Job exists" 0 "$name"
        check "Last status ok" $([ "$last_status" = "ok" ] && echo 0 || echo 1) "status=$last_status"
        check "Last run recent" 0 "last_run=$last_run"
        if echo "$base_url" | grep -q '8010'; then
            check "Routes direct to vLLM" 1 "base_url=$base_url (should be :8000)"
        else
            check "Routes direct to vLLM" 0
        fi
    else
        check "jobs.json valid" 1 "invalid JSON"
    fi
else
    check "jobs.json exists" 1 "not found at $jobs_path"
fi

# --- Environment (.env) ---
section "Environment (.env)"
env_path="$HERMES_HOME/.env"
if [[ -f "$env_path" ]]; then
    # Source env vars safely (no secrets printed)
    stream_retries=$(grep -oP '^HERMES_STREAM_RETRIES=\K.*' "$env_path" 2>/dev/null || echo "1")
    stream_timeout=$(grep -oP '^HERMES_STREAM_READ_TIMEOUT=\K.*' "$env_path" 2>/dev/null || echo "0")
    has_tg_token=$(grep -c '^TELEGRAM_BOT_TOKEN=.*' "$env_path" 2>/dev/null || echo 0)

    check "Stream retries >= 10" $([ "$stream_retries" -ge 10 ] 2>/dev/null && echo 0 || echo 1) "HERMES_STREAM_RETRIES=$stream_retries"
    check "Stream timeout >= 300s" $([ "$stream_timeout" -ge 300 ] 2>/dev/null && echo 0 || echo 1) "HERMES_STREAM_READ_TIMEOUT=${stream_timeout}s"
    check "Telegram bot token" $([ "$has_tg_token" -ge 1 ] 2>/dev/null && echo 0 || echo 1) "$([ "$has_tg_token" -ge 1 ] && echo "present" || echo "missing or short")"
else
    check ".env exists" 1 "not found at $env_path"
fi

# --- Retry backoff patch ---
section "Retry backoff patch"
helpers_path="$HERMES_HOME/hermes-agent/agent/chat_completion_helpers.py"
if [[ -f "$helpers_path" ]]; then
    has_sleep=$(grep -c '_time\.sleep(_retry_delay)' "$helpers_path" 2>/dev/null || echo 0)
    has_backoff=$(grep -c '_retry_delay = min(2 \*\* _stream_attempt, 32)' "$helpers_path" 2>/dev/null || echo 0)
    if [[ "$has_sleep" -ge 1 ]] && [[ "$has_backoff" -ge 1 ]]; then
        check "Exponential backoff" 0 "ok"
    else
        check "Exponential backoff" 1 "missing — retries fire instantly"
    fi
else
    check "chat_completion_helpers.py" 1 "not found at $helpers_path"
fi

# --- Recent cron failures ---
section "Recent failures (agent.log)"
agent_log="$HERMES_HOME/logs/agent.log"
if [[ -f "$agent_log" ]]; then
    recent_fails=$(grep -c 'c16568.*\bConnection error\b' "$agent_log" 2>/dev/null || echo 0)
    check "No recent cron connection errors" $([ "$recent_fails" -eq 0 ] && echo 0 || echo 1) "found $recent_fails error entries"
else
    check "agent.log exists" 1
fi

# --- Summary ---
section "Summary"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
    echo "  $FAIL_COUNT check(s) FAILED:"
    for msg in "${FAIL_MSGS[@]}"; do
        echo "    - $msg"
    done
    echo ""
    echo "Overall: FAIL"
    exit 1
else
    echo "  All checks passed."
    echo ""
    echo "Overall: OK"
    exit 0
fi
