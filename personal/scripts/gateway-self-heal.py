#!/usr/bin/env python3
"""Gateway self-healing Telegram healthcheck.

Detects Telegram connectivity failures and triggers gateway restart via tmux.
Runs independently (no dependency on the gateway process itself).
Use as a cronjob script (no_agent=True) — silent on success, delivers on error.
"""

import subprocess
import sys
import time
from datetime import datetime, timezone

# Tmux configuration
TMUX_BIN = "/ihome/alee/alc376/tmux-build/install/bin/tmux"
TMUX_SOCKET = "/vast/alee/alc376/tunnel-runtime/tmux/tmux-157528/default"
SESSION = "claude-gpu"
GATEWAY_WINDOW = "gateway"  # Window name, not index

LAST_HEAL_FILE = "/tmp/gateway-heal-last"
HEAL_COOLDOWN_SECONDS = 900  # 15 minutes — prevent restart loops

GATEWAY_LOG = "/ihome/alee/alc376/.hermes/logs/gateway.log"


def tmux_run(*args, timeout=10):
    """Run a tmux command."""
    cmd = [TMUX_BIN, "-S", TMUX_SOCKET] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip(), result.returncode


def check_telegram_connectivity() -> bool:
    """Detect if Telegram connectivity is broken.

    Robust detection: use tail+grep on recent log for Telegram-specific
    disconnection/error events. Avoids scanning entire log.
    Returns True if working, False if broken.
    """
    patterns = [
        "telegram disconnected",
        "disconnected from telegram",
        "telegram connection refused",
        "telegram connection timeout",
        "telegram unauthorized",
        "telegram flood wait",
    ]
    try:
        # Only tail last 500 lines — avoids full scan on slow /ihome mount
        result = subprocess.run(
            ["tail", "-500", GATEWAY_LOG],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return True  # Can't read log, assume fine

        recent = result.stdout.lower()
        counts = sum(1 for p in patterns if p in recent)
        # >3 telegram-specific issues in recent window = broken
        return counts <= 3

    except FileNotFoundError:
        return True
    except (subprocess.TimeoutExpired, Exception):
        return True


def find_gateway_window_index() -> str:
    """Find the tmux window index for the gateway."""
    indices, rc = tmux_run("list-windows", "-t", SESSION, "-F", "#{window_index}")
    if rc != 0:
        raise RuntimeError(f"Cannot list tmux windows: {indices}")

    names, rc = tmux_run("list-windows", "-t", SESSION, "-F", "#{window_name}")
    if rc != 0:
        raise RuntimeError(f"Cannot list window names: {names}")

    for idx, name in zip(indices.split(), names.split()):
        if name == GATEWAY_WINDOW:
            return idx

    raise RuntimeError(f"Gateway window '{GATEWAY_WINDOW}' not found in session '{SESSION}'")


def restart_gateway():
    """Restart the Hermes gateway via tmux."""
    window_idx = find_gateway_window_index()
    target = f"{SESSION}:{window_idx}"

    output_lines = []

    # Step 1: Cancel current gateway process
    output_lines.append(f"1. Cancelling gateway in W{window_idx}...")
    subprocess.run([TMUX_BIN, "-S", TMUX_SOCKET, "send-keys", "-t", target, "C-c"],
                   capture_output=True, timeout=5)
    time.sleep(3)

    # Step 2: Launch fresh gateway
    output_lines.append(f"2. Starting fresh gateway in W{window_idx}...")
    subprocess.run([TMUX_BIN, "-S", TMUX_SOCKET, "send-keys", "-t", target,
                    "hermes gateway run --replace", "C-m"],
                   capture_output=True, timeout=10)

    # Step 3: Wait for startup and verify
    output_lines.append("3. Waiting for gateway startup...")
    time.sleep(5)

    # Capture to verify it started
    capture, _ = tmux_run("capture-pane", "-t", target, "-p", "-S", "-10")
    if "gateway" in capture.lower() or "listening" in capture.lower():
        output_lines.append("✓ Gateway restarted successfully")
    else:
        output_lines.append("⚠ Gateway started but could not confirm")

    # Record heal time
    with open(LAST_HEAL_FILE, 'w') as f:
        f.write(str(int(time.time())))

    return "\n".join(output_lines)


if __name__ == "__main__":
    # Check cooldown — prevent restart loops
    try:
        with open(LAST_HEAL_FILE) as f:
            last_heal = int(f.read().strip())
        if time.time() - last_heal < HEAL_COOLDOWN_SECONDS:
            sys.exit(0)  # Within cooldown, silent
    except (FileNotFoundError, ValueError):
        pass

    # Check Telegram connectivity
    if check_telegram_connectivity():
        sys.exit(0)  # Working fine, silent

    # Telegram is broken — trigger self-heal (silently)
    try:
        restart_gateway()
        sys.exit(0)  # Restart succeeded, silent
    except Exception as e:
        # Only deliver output (and exit 0) if the restart ITSELF fails
        timestamp = datetime.now(timezone.utc).isoformat()
        print(f"[{timestamp}] TELEGRAM BROKEN — gateway self-heal failed, needs manual intervention: {e}")
        sys.exit(0)  # Exit 0 so scheduler delivers stdout normally
