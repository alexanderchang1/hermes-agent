#!/usr/bin/env bash
# Fleet Supervisor guardrail — session-based + pane-keyword constraint injection.
# Fires on pre_llm_call. Only acts for the supervisor session; silent for all others.

set -euo pipefail

payload="$(cat)"
SESSION_ID="$(echo "$payload" | jq -r '.session_id // ""' 2>/dev/null)"

# Only hit the fleet supervisor session
if [[ ! "$SESSION_ID" =~ c16568 ]]; then
    printf '{}\n'
    exit 0
fi

# ── Base constraints (always injected) ──
BASE='=== FLEET SUPERVISOR ===
ROLE: Fleet status reporter and tmux nudger only.
PROHIBITED:
- NEVER perform spec/plan reviews yourself
- NEVER delegate tasks or access project files
- NEVER execute work belonging to Claude windows
If a window skips the 5-step pipeline, ESCALATE — do not fix it yourself.
Toolset: terminal only.
====================================='

ADDITIONAL=""

# ── Scan tmux panes for keywords ──
TMUX_BIN="${TMUX_BIN:-$(command -v tmux 2>/dev/null || echo ~/tmux-build/install/bin/tmux)}"
SOCKET="/vast/alee/alc376/tunnel-runtime/tmux/tmux-157528/default"
TMPF="/tmp/_fs_pane_scan_$$.txt"
> "$TMPF"

for wid in 1 3 4 5; do
    "$TMUX_BIN" -S "$SOCKET" capture-pane -t "claude-gpu:${wid}" -p -q >> "$TMPF" 2>/dev/null || true
done

if [ -s "$TMPF" ]; then
    PC="$(cat "$TMPF")"

    if echo "$PC" | grep -qiE 'review|spec.*check|plan.*review'; then
        ADDITIONAL="${ADDITIONAL}REVIEW IN PANE: You must NOT review yourself. Nudge the window: delegate to Codex via /delegate."$'\n'
    fi

    if echo "$PC" | grep -qiE 'brainstorm|spec.*writ|design.*doc'; then
        if ! echo "$PC" | grep -qiE 'delegate.*review|delegat'; then
            ADDITIONAL="${ADDITIONAL}SPEC/DESIGN WITHOUT DELEGATION: Remind window — 5-step pipeline requires /delegate after brainstorm/spec. Do not review yourself."$'\n'
        fi
    fi

    if echo "$PC" | grep -qiE 'working|processing|executing|building'; then
        ADDITIONAL="${ADDITIONAL}ACTIVE WORK DETECTED: Do not nudge or interrupt."$'\n'
    fi

    if echo "$PC" | grep -qiE 'idle|waiting|pauas'; then
        ADDITIONAL="${ADDITIONAL}IDLE WINDOW(S) DETECTED: Monitor for wrap-up via stale pane counter."$'\n'
    fi
fi

rm -f "$TMPF"

# ── Build output ──
if [ -n "$ADDITIONAL" ]; then
    COMBINED="${BASE}"$'\n\n'"${ADDITIONAL}"
else
    COMBINED="$BASE"
fi

CURRENT_CTX="$(echo "$payload" | jq -r '.context // ""' 2>/dev/null)"
if [ -n "$CURRENT_CTX" ]; then
    FULL="${CURRENT_CTX}"$'\n\n'"${COMBINED}"
else
    FULL="$COMBINED"
fi

printf '{"context": %s}\n' "$(printf '%s' "$FULL" | jq -Rs .)"
