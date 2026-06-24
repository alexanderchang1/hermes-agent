#!/bin/bash
# Shell wrapper for the collector — adds hard timeout to avoid python3 hangs
# Output: collector JSON on stdout, or empty on failure
set -euo pipefail

COLLECT_SCRIPT="/ihome/alee/alc376/.hermes/scripts/hermes-supervisor-collect.py"
TMUX_SOCKET="/vast/alee/alc376/tunnel-runtime/tmux/tmux-157528/default"

# Robust tmux socket detection — prefer 'default', fall back to 'gemma.sock'
if [ ! -S "$TMUX_SOCKET" ]; then
    TMUX_SOCKET="/vast/alee/alc376/tunnel-runtime/tmux/tmux-157528/gemma.sock"
fi
export TMUX_SOCKET

# Run collector with 75s hard timeout — python3 hangs are a known host issue
# Use -u for unbuffered output so we don't lose data on timeout
RESULT=$(env -u TMUX TMUX_SOCKET_PATH="$TMUX_SOCKET" timeout 75 python3 -u "$COLLECT_SCRIPT" --json 2>/dev/null) || {
    # On failure (timeout/exit), output minimal progress report
    echo '{"progress": "collector_failed","fallback":"shell-capture"}'
    exit 0
}
echo "$RESULT"
