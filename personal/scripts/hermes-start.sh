#!/usr/bin/env bash
# Start Hermes gateway + watchdog (tied to Slurm job lifecycle).
# Usage: hermes-start
#
# When the Slurm job ends (timeout/preemption/SIGKILL), the watchdog
# kills the gateway so we don't orphan processes on the compute node.

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
VENV_BIN="$HERMES_HOME/hermes-agent/venv/bin"

# Start gateway in background (detached to this shell, NOT a new tmux window)
"$VENV_BIN/python" -m hermes_cli.main gateway run --replace &
GATEWAY_PID=$!
echo "Gateway started (PID $GATEWAY_PID)"

# Start watchdog in background — kills gateway when Slurm job dies
"$VENV_BIN/python" "$HERMES_HOME/scripts/gateway-watchdog.py" &
WATCHDOG_PID=$!
echo "Watchdog started (PID $WATCHDOG_PID, monitoring Slurm PID $SLURM_TASK_PID)"

# Save PIDs so we can optionally clean up
echo "$GATEWAY_PID" > "$HERMES_HOME/.gateway.pid"
echo "$WATCHDOG_PID" > "$HERMES_HOME/.gateway-watchdog.pid"

# Wait for the gateway — when it exits (killed by watchdog or user), kill watchdog too
wait $GATEWAY_PID || true
kill $WATCHDOG_PID 2>/dev/null
echo "Gateway stopped."
