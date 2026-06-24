#!/bin/bash
# Gateway self-healing Telegram healthcheck
# Shell-only — avoids python3 hangs on this host

set -euo pipefail

GATEWAY_LOG="/ihome/alee/alc376/.hermes/logs/gateway.log"
LAST_HEAL_FILE="/tmp/gateway-heal-last"
RESTARTING_FILE="/tmp/gateway-restarting"
HEAL_COOLDOWN=1800  # 30 minutes — increased from 15

TMUX_BIN="/ihome/alee/alc376/tmux-build/install/bin/tmux"
TMUX_SOCKET="/vast/alee/alee/tunnel-runtime/tmux/tmux-157528/default"
SESSION="claude-gpu"

# --- Circuit breaker #1: just restarted, wait for stabilization ---
if [ -f "$RESTARTING_FILE" ]; then
    restart_ts=$(cat "$RESTARTING_FILE" 2>/dev/null || echo 0)
    elapsed=$(( $(date +%s) - restart_ts ))
    if [ "$elapsed" -lt 180 ]; then
        exit 0  # Gateway just restarted, wait 3 min for stabilization
    fi
    rm -f "$RESTARTING_FILE"
fi

# --- Circuit breaker #2: check if gateway actually connected recently ---
if [ -f "$GATEWAY_LOG" ]; then
    if tail -200 "$GATEWAY_LOG" 2>/dev/null | grep -qi 'telegram.*connected to telegram\|connection established\|telegram.*ready\|telegram.*connected successfully'; then
        exit 0  # Gateway is healthy — recent connection success
    fi
fi

# --- Cooldown ---
if [ -f "$LAST_HEAL_FILE" ]; then
    last_heal=$(cat "$LAST_HEAL_FILE" 2>/dev/null || echo 0)
    now=$(date +%s)
    elapsed=$((now - last_heal))
    if [ "$elapsed" -lt "$HEAL_COOLDOWN" ]; then
        exit 0
    fi
fi

# --- Detect Telegram disconnect events (last 5 min) ---
NOW=$(date +%s)
CUTOFF_EPOCH=$((NOW - 300))
HITS=0
if [ -f "$GATEWAY_LOG" ]; then
    while IFS= read -r line; do
        ts="${line%%,*}"
        line_epoch=$(date -d "$ts" +%s 2>/dev/null || echo 0)
        if [ "$line_epoch" -lt "$CUTOFF_EPOCH" ]; then
            continue
        fi
        lower=$(echo "$line" | tr '[:upper:]' '[:lower:]')
        case "$lower" in
            *"telegram disconnected"*|*"disconnected from telegram"*|*"telegram connection refused"*|*"telegram unauthorized"*|*"telegram flood wait"*)
                HITS=$((HITS + 1))
                ;;
        esac
    done < <(tail -500 "$GATEWAY_LOG" 2>/dev/null || true)
fi

if [ "$HITS" -le 3 ]; then
    exit 0
fi

# --- Telegram broken → restart gateway ---
GW_WIN_IDX=$(for idx in $($TMUX_BIN -S "$TMUX_SOCKET" list-windows -t "$SESSION" -F '#{window_index}' 2>/dev/null); do
    name=$($TMUX_BIN -S "$TMUX_SOCKET" list-windows -t "$SESSION" -F '#{window_index}:#{window_name}' 2>/dev/null | grep "^${idx}:" | cut -d: -f2)
    if [ "$name" = "gateway" ]; then
        echo "$idx"
        break
    fi
done)

if [ -z "$GW_WIN_IDX" ]; then
    echo "[SELF-HEAL] Telegram broken but gateway window not found"
    exit 0
fi

TARGET="${SESSION}:${GW_WIN_IDX}"

$TMUX_BIN -S "$TMUX_SOCKET" send-keys -t "$TARGET" C-c 2>/dev/null || true
sleep 3

$TMUX_BIN -S "$TMUX_SOCKET" send-keys -t "$TARGET" "hermes gateway run --replace" Enter 2>/dev/null || true

# Mark as restarting so next runs don't cascade
date +%s > "$RESTARTING_FILE"
date +%s > "$LAST_HEAL_FILE"

exit 0
