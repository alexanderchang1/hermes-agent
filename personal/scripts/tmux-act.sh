#!/usr/bin/env bash
# tmux-act — precoded tmux action library for fleet supervision.
# Each action is a tested, atomic tmux interaction.
# Usage: tmux-act <ACTION> [W=<window>] [TEXT=<message>]
#
# Actions:
#   capture    W=<n>                  — capture last 30 lines of pane
#   answer_delegate W=<n>             — answer "delegate vs skip" menu (option 1)
#   answer_proceed  W=<n>             — send "proceed" + Enter
#   approve_default W=<n>             — approve dialog (Enter = option 1)
#   approve_edit    W=<n>             — approve "allow all edits" (option 2)
#   dismiss_survey  W=<n>             — dismiss Claude Code survey modal
#   activate_goal   W=<n>             — activate pending goal (Enter)
#   nudge_goal      W=<n> TEXT=<msg>  — send /goal <msg> 30m
#   send_text       W=<n> TEXT=<msg>  — send arbitrary text + Enter
#   send_key        W=<n> KEY=<key>   — send a raw tmux key (e.g. Down, Up, C-m)
#   submit_text     W=<n> TEXT=<msg>  — send text + CR (submit in Claude Code editor/input)
#   submit_proceed  W=<n>             — send "proceed" + CR (force-submit in Claude Code)
#   submit_default  W=<n>             — send CR only (force-submit in Claude Code)
#
# All actions use the custom tmux binary + socket.
# NOTE: C-j (LF) works for tmux menus. Claude Code interprets C-j as newline in its
# input buffer (Shift+Enter). Claude Code submits on C-m (CR). Use the "submit_*"
# actions for Claude Code input fields.
set -euo pipefail

TMUX_BIN="/ihome/alee/alc376/tmux-build/install/bin/tmux"
TMUX_SOCKET="/vast/alee/alc376/tunnel-runtime/tmux/tmux-157528/default"
TMPL=("${TMUX_BIN}" "-S" "${TMUX_SOCKET}")
SESSION="claude-gpu"

# Parse KEY=VALUE args
W=""; TEXT=""; KEY=""
for arg in "$@"; do
  case "$arg" in
    W=*)  W="${arg#W=}" ;;
    TEXT=*) TEXT="${arg#TEXT=}" ;;
    KEY=*) KEY="${arg#KEY=}" ;;
  esac
done

ACTION="${1:-help}"
shift 2>/dev/null || true

# Guard: if action requires W= and W is set, verify window exists
if [[ -n "$W" && "$ACTION" != "capture" && "$ACTION" != "help" ]]; then
  if ! "${TMPL[@]}" list-windows -t "${SESSION}" -F '#{window_index}' 2>/dev/null | grep -qx "${W}"; then
    echo "SKIP: window W${W} does not exist in ${SESSION}"
    exit 0
  fi
fi

# Helper: send Enter key reliably (literal, not string "C-m")
send_enter() {
  "${TMPL[@]}" send-keys -t "${SESSION}:${W}" C-j
}

# Helper: send CR (Carriage Return) — Claude Code's actual "submit" key.
# Claude Code's PTY treats C-j (LF) as newline in text fields but submits on C-m (CR).
send_submit() {
  "${TMPL[@]}" send-keys -t "${SESSION}:${W}" C-m
}

# Helper: send text then Enter
send_then_enter() {
  local text="$1"
  "${TMPL[@]}" send-keys -t "${SESSION}:${W}" -- "$text"
  sleep 0.5
  send_enter
}

# Helper: send text then CR (submit for Claude Code input fields)
send_then_submit() {
  local text="$1"
  "${TMPL[@]}" send-keys -t "${SESSION}:${W}" -- "$text"
  sleep 0.5
  send_submit
}

case "$ACTION" in
  capture)
    [[ -z "$W" ]] && { echo "ERROR: W required"; exit 1; }
    "${TMPL[@]}" capture-pane -t "${SESSION}:${W}" -p -S -30
    ;;

  answer_delegate)
    [[ -z "$W" ]] && { echo "ERROR: W required"; exit 1; }
    send_then_submit "1"
    ;;

  answer_proceed)
    [[ -z "$W" ]] && { echo "ERROR: W required"; exit 1; }
    send_then_submit "proceed"
    ;;

  approve_default)
    [[ -z "$W" ]] && { echo "ERROR: W required"; exit 1; }
    send_submit
    ;;

  approve_edit)
    [[ -z "$W" ]] && { echo "ERROR: W required"; exit 1; }
    send_then_submit "2"
    ;;

  dismiss_survey)
    [[ -z "$W" ]] && { echo "ERROR: W required"; exit 1; }
    "${TMPL[@]}" send-keys -t "${SESSION}:${W}" Down
    sleep 0.5
    send_submit
    ;;

  activate_goal)
    [[ -z "$W" ]] && { echo "ERROR: W required"; exit 1; }
    send_submit
    ;;

  nudge_goal)
    [[ -z "$W" ]] && { echo "ERROR: W required"; exit 1; }
    [[ -z "$TEXT" ]] && { echo "ERROR: TEXT required"; exit 1; }
    send_then_submit "/goal \"$TEXT\" 30m"
    ;;

  send_text)
    [[ -z "$W" ]] && { echo "ERROR: W required"; exit 1; }
    [[ -z "$TEXT" ]] && { echo "ERROR: TEXT required"; exit 1; }
    send_then_enter "$TEXT"
    ;;

  submit_text)
    [[ -z "$W" ]] && { echo "ERROR: W required"; exit 1; }
    [[ -z "$TEXT" ]] && { echo "ERROR: TEXT required"; exit 1; }
    send_then_submit "$TEXT"
    ;;

  submit_proceed)
    [[ -z "$W" ]] && { echo "ERROR: W required"; exit 1; }
    send_then_submit "proceed"
    ;;

  submit_default)
    [[ -z "$W" ]] && { echo "ERROR: W required"; exit 1; }
    send_submit
    ;;

  send_key)
    [[ -z "$W" ]] && { echo "ERROR: W required"; exit 1; }
    [[ -z "$KEY" ]] && { echo "ERROR: KEY required"; exit 1; }
    "${TMPL[@]}" send-keys -t "${SESSION}:${W}" "$KEY"
    ;;

  append_log)
    [[ -z "${TEXT:-}" ]] && { echo "ERROR: TEXT required"; exit 1; }
    LOG_FILE="/ix1/alee/LO_LAB/Personal/Alexander_Chang/alc376/vault/lab/agents/supervisor/coordination-log.md"
    ts=$(date -u +"%Y-%m-%dT%H:%M")
    echo "${ts} ${TEXT}" >> "${LOG_FILE}"
    echo "LOGGED: ${ts} ${TEXT}"
    ;;

  help|*)
    echo "tmux-act — precoded tmux actions"
    echo "Usage: tmux-act <ACTION> [W=<n>] [TEXT=<msg>] [KEY=<key>]"
    echo ""
    echo "Actions:"
    echo "  capture         W=<n>                  capture last 30 lines"
    echo "  answer_delegate W=<n>                  answer delegate vs skip (opt 1)"
    echo "  answer_proceed  W=<n>                  send 'proceed' + Enter"
    echo "  approve_default W=<n>                  approve dialog (Enter = opt 1)"
    echo "  approve_edit    W=<n>                  approve allow-all edits (opt 2)"
    echo "  dismiss_survey  W=<n>                  dismiss Claude Code survey"
    echo "  activate_goal   W=<n>                  activate pending goal"
    echo "  nudge_goal      W=<n> TEXT=<msg>       send /goal <msg> 30m"
    echo "  send_text       W=<n> TEXT=<msg>       send text + LF (C-j, menu Enter)"
    echo "  send_key        W=<n> KEY=<key>        send raw tmux key (Down, Up, C-m)"
    echo "  submit_text     W=<n> TEXT=<msg>       send text + CR (C-m, Claude Code submit)"
    echo "  submit_proceed  W=<n>                  send 'proceed' + CR (Claude Code submit)"
    echo "  submit_default  W=<n>                  send CR only (Claude Code submit)"
    echo "  append_log      TEXT=<msg>             append one line to coordination log"
    ;;
esac
