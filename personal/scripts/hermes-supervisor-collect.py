#!/usr/bin/env python3
"""
Hermes Supervisor — Fleet Status Collector v2
Gathers all fleet state in a single short-lived run for the supervisor cron.

NEW in v2:
- SLURM job completion detection (sacct for recent transitions)
- Git diff summary per project (actual code changes)
- SLURM stderr scan for recent errors
- Pacing.json read/write for adaptive scheduling
- Handoff queue aging detection
- Window-notes auto-update helper

Design principles:
- Runs FAST (<10 seconds) — never pings cluster admin
- No slurm job submission, no long-running processes
- Immutable: only reads state, never modifies anything
- Outputs structured JSON to stdout for the Hermes agent to consume
- Idempotent: safe to run multiple times

Usage: python3 hermes-supervisor-collect.py [--windows 0,1,2,6,7] [--json]
"""

import argparse
import datetime
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys

VAULT = "/ix1/alee/LO_LAB/Personal/Alexander_Chang/alc376/vault"
PROJECTS_ROOT = "/ix1/alee/LO_LAB/Personal/Alexander_Chang/alc376"
# Graceful fallback: /ix1 NFS is often slow/unreachable
def _path(p):
    try:
        with open(p) as f:
            return p
    except (OSError, TimeoutError):
        return None

BUS_DB = _path(os.path.join(VAULT, "lab/agents/bus/agent-bus.db"))
GRAPH_DB = _path(os.path.join(VAULT, ".vault-ltm.db"))
PACING_PATH = _path(os.path.join(VAULT, "lab/agents/supervisor/pacing.json"))
HANDOFF_QUEUE = _path(os.path.join(VAULT, "lab/agents/handoff-queue"))
CLI_HEARTBEAT = _path(os.path.join(VAULT, "lab/agents/supervisor/cli_heartbeat"))

SESSION = "claude-gpu"
TMUX_SOCKET = os.environ.get("TMUX_SOCKET_PATH", "") or "/vast/alee/alc376/tunnel-runtime/tmux/tmux-157528/default"
TMUX_BIN = os.path.expanduser("~/tmux-build/install/bin/tmux") if os.path.exists(os.path.expanduser("~/tmux-build/install/bin/tmux")) else "tmux"

ALLOWED_WINDOWS = {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9"}
SUPERVISOR_WINDOW = "9"  # Hermes CLI window — DO NOT resolve actions on this window

# --- Session marker paths (for auto window-notes update) ---
MARKERS_DIR = os.path.expanduser("~/.claude/session_markers")

# --- Pacing state file ---
PACING_PATH = os.path.join(VAULT, "lab/agents/supervisor/pacing.json")

# --- Handoff queue ---
HANDOFF_QUEUE = os.path.join(VAULT, "lab/agents/handoff-queue")

# --- CLI heartbeat path (for autodeliver detection) ---
CLI_HEARTBEAT = os.path.join(VAULT, "lab/agents/supervisor/cli_heartbeat")
CLI_PRESENCE_THRESHOLD = 15  # minutes -- if heartbeat older than this, user is away



# ==============================================================================
# Core collectors (unchanged from v1)
# ==============================================================================

def tmux_capture(window, lines=50):
    """Capture last N lines from a pane.

    Always 50 lines — recent enough to see current state, small enough
    to avoid ancient scrollback polluting detection.
    """
    try:
        result = subprocess.run(
            [TMUX_BIN, "-S", TMUX_SOCKET, "capture-pane", "-t", f"{SESSION}:{window}", "-p", "-S", "-"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            all_lines = result.stdout.split("\n")
            trimmed = "\n".join(all_lines[-lines:])
            return trimmed
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def tmux_window_name(window):
    """Get window name."""
    try:
        result = subprocess.run(
            [TMUX_BIN, "-S", TMUX_SOCKET, "list-windows", "-t", SESSION, "-F", "#{window_index}:#{window_name}"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split("\n"):
            if line.startswith(f"{window}:"):
                return line.split(":", 1)[1]
    except:
        pass
    return ""


def detect_pane_state(raw):
    """Detect agent activity state from pane content."""
    if not raw:
        return {"state": "empty", "confidence": 0.5, "ctx_used": None,
                "goal_active": False, "goal_pending": False, "goal_deadline": None,
                "goal_elapsed_seconds": None, "details": "empty pane",
                "last_lines": [], "pending_tasks": [], "has_subagent_menu": False,
                "line_count": 0, "last_question": None, "stacked_goals": 0, "has_spinner": False}

    lines = raw.strip().split("\n")
    footer = "\n".join(lines[-3:])
    has_spinner = False
    ctx_match = re.search(r'(?:Context used:|Input:)\s*(\d+)%', footer)
    ctx_used = int(ctx_match.group(1)) if ctx_match else None

    # Goal state: active vs pending vs idle
    # Check last 10 lines (not just footer) — the goal indicator may be a few lines up
    # when there's conversation content between the goal line and the status bar.
    recent_footer = "\n".join(lines[-10:])
    goal_active = "◎ /goal active" in recent_footer
    pending_match = re.search(r'◎\s+/goal.+pending\b|\|\s+goal pending\b|◎ /goal pending', recent_footer)
    goal_pending = bool(pending_match)
    goal_deadline = None
    if goal_active:
        dl_match = re.search(r'◎ /goal active \(([^)]+)\)', recent_footer)
        goal_deadline = dl_match.group(1) if dl_match else None

    state = "idle"
    confidence = 0.5
    details = []

    # CRITICAL FIX: Only consider activity markers that appear AFTER the last prompt line.
    # A session can be idle at ❯ with ● markers from hours ago in the buffer.
    # Find the last prompt (❯ or $) and check if there's any real activity BELOW it.
    last_prompt_pos = max(
        (m.start() for m in re.finditer(r'(❯\s*$|(^|\n)\s*\$)', raw, re.MULTILINE)),
        default=-1,
    )
    # Extract content AFTER the last prompt (the "current" state)
    current_content = raw[last_prompt_pos + 1:] if last_prompt_pos >= 0 else None

    # If no prompt found in the buffer at all, the visible area scrolled past it.
    # This means the buffer is entirely old output — treat as idle unless the status
    # bar (last 3 lines) clearly shows active state.
    if last_prompt_pos < 0:
        # No prompt line visible — check if the status bar shows active work
        is_actively_working = bool(re.search(
            r'\w+[ing]….*\(\d+[sm]',  # "Kneading… (5m 52s"
            raw
        ))
        if not is_actively_working:
            # Check footer for active indicators
            footer_status = bool(re.search(
                r'◎ /goal active',
                "\n".join(lines[-5:])
            ))
            is_actively_working = is_actively_working or footer_status
        if is_actively_working:
            state = "working"
            confidence = 0.7
            details.append("no prompt visible but spinning indicator present")
        else:
            state = "idle"
            confidence = 0.6
            details.append("no prompt in buffer (scrolled) — likely idle")
        # Still check goal_state, subagent_menu, etc. below

    # Also check for active processing indicators ANYWHERE in the recent buffer.
    # These indicate the agent is currently thinking/working, regardless of prompt position.
    # We look at the last N lines (already trimmed) and require a recent timer.
    # Covers Claude Code spinning indicators:
    #   ✢ Cooking… (5m 32s)
    #   ✢ Submitting inference job… (8m 29s)
    #   · Kneading… (5m 52s · ↓ 6.2k tokens)
    #   ✽ Shimmying… (2m 21s)
    #   ✸ Fermenting… (3m 15s)
    #   ✻ Flambéing… (50s) <-- accented chars (é) break \w
    spinning_pattern = r'(✢|·|✻|✽|✸)\s+[^\n]*?…\s*?\(\d+[sm]'
    # Covers Claude Code generic "word+ing… (time)" without ornament
    generic_spinning = r'[^\n]+\w+ing…\s*?\(\d+[sm]'
    # Covers Hermes TUI: "⚕ model │ X/Y │ [██░░] XX% │" with a timer "⏲ Xm Xs"
    # or "Qwen reasoning" status line with active token progress
    hermes_tui_active = r'⚕\s*\S+.*(\d+/|\d+K/).*\[.*\]'
    hermes_tui_thinking = r'\d+[mM].*\d+[sS]'  # timer adjacent to TUI line

    is_actively_working = (
        bool(re.search(spinning_pattern, raw)) or
        bool(re.search(generic_spinning, raw)) or
        bool(re.search(hermes_tui_active, "\n".join(lines[-5:])))
    )

    # Extra: if hermes_tui_active matched and there's a timer nearby, very confident
    if re.search(hermes_tui_active, "\n".join(lines[-5:])):
        is_actively_working = True

    # Active shells = subprocesses still running (e.g. Open Code background review).
    # This is definitive evidence the agent has in-progress work and should NOT be idle.
    has_active_shells_anywhere = bool(re.search(r'\d+ shells? still running', raw))

    # Past-tense marker: agent finished a turn (✻ Sautéed/Baked/etc for N[m/s]).
    # "Cogitated" is the same pattern — the agent finished thinking and sat down.
    # The key signal is not the VERB but what happened AFTER: did it stand up again?
    has_past_tense_marker = bool(re.search(r'✻\s+\w+ed\s+for\s+\d+[sm]', raw))

    # Active todo/task list = evidence of active work, not idle
    # Pattern: "6 tasks (1 done, 1 in progress, 4 open)" or similar
    has_active_tasklist = bool(re.search(r'\d+ tasks \(.*in progress', raw))
    # Recap line means the agent genuinely paused its turn (not just mid-prompt)
    has_recap_line = bool(re.search(r'^※\s+recap:', raw, re.MULTILINE))
    # Combined: agent "sat down" — finished a turn and is now idle at the prompt
    agent_sat_down = has_past_tense_marker and has_recap_line
    # Past-tense marker ALONE (without recap) still means the agent finished a turn.
    # The recap is unreliable — Claude Code doesn't always output it.
    # A past-tense marker by itself should at least flag for review.
    agent_paused = has_past_tense_marker  # broader: just the past-tense marker, no recap required

    # Modern Claude Code indicators of active work (show in footer/status)
    has_active_indicator = bool(re.search(
        r'◎ /goal active \(\d+[smh]+\)',  # goal running (matches m, h, min units)
        footer
    ))

    # Bullet markers after last prompt = active
    has_bullet_after_prompt = bool(current_content and re.search(r'(●|◐|◑)', current_content))

    # Active processing indicators
    has_processing_after_prompt = bool(current_content and re.search(
        r'(Thinking|Processing).*\d+[sm]',
        current_content
    ))

    # FIX #1: Detect user-typed commands/instructions below the ❯ prompt.
    # When user types text after the prompt (e.g., "stage the data and run
    # the build on htc"), Claude Code hasn't processed it yet but WILL —
    # this is an active task, NOT idle.
    has_user_input_after_prompt = False
    _SKIP_FOOTER_PATTERNS = [
        r'^(❯|\$|\s)',               # another prompt or indented
        r'|context\s+used',           # context bar
        r'|\d+\s*tokens?\s+in',       # token count
        r'|◎\s',                      # goal marker
        r'|⚕\s',                      # hermes TUI
        r'|\d+\s*(shell|monitor)',     # shell/monitor count
        r'|←\s+for\s+agents',         # status bar
        r'|↓\s+\d+',                  # token delta
        r'|\d+ tasks?\s*\(',         # task summary
        r'|goal\s*(active|pending|stopped)',  # goal state
        r'|^[\u2500\u2501]+$',         # box-drawing separator
        r'|^\s*[\u2500\u2501]+',       # leading indent + box-drawing
        r'|^\xa0\s*$',               # non-breaking space only
        r'|^\xa0\s*[\u2500\u2501]+',    # non-breaking space + box-drawing
        r'|^\s*Input:\s*\d+%',      # "Input: 42%"
        r'|\s*5hr\s+used',          # "5hr used: 14%"
        r'|\s*Weekly\s+used',       # "Weekly used: 98%"
        r'|^\s*⏵⏵\s+auto\s+mode',   # "⏵⏵ auto mode on"
    ]
    _SKIP_FOOTER_RE = re.compile(''.join(_SKIP_FOOTER_PATTERNS), re.I)
    if current_content:
        for cl in current_content.strip().split('\n'):
            stripped = cl.strip()
            if not stripped or len(stripped) <= 2:
                continue
            if _SKIP_FOOTER_RE.search(cl):
                continue
            has_user_input_after_prompt = True
            break

    # FIX #2: Detect active monitoring (babysitting, monitor events, etc.)
    # This is active work — agent is supervising a long-running process
    has_babysitting = bool(re.search(r'[Bb]abysit|monitoring\s+(job|event|slurm|long.?run)', raw))
    has_monitor_event_text = bool(re.search(r'Monitor event', raw))
    is_monitoring_active = has_babysitting or has_monitor_event_text

    # PRIORITY: if the agent "sat down" (past-tense + recap) with goal active,
    # it's stopped/waiting — do NOT let tasklist/goal_timer override this.
    # The "agent_sat_down" combo is definitive evidence the agent finished a turn and is now sitting at the prompt.
    if agent_sat_down and goal_active:
        has_active_shells = bool(re.search(r'\d+ shells? still running', raw))
        has_monitor_active = bool(re.search(r'\d+ monitor', " ".join(lines[-5:])))
        if has_active_shells or has_monitor_active:
            reasons = []
            if has_active_shells:
                reasons.append("shells active")
            if has_monitor_active:
                reasons.append("monitor active")
            state = "blocked_waiting"
            confidence = 0.6
            details.append(f"waiting for external event ({', '.join(reasons)}) - verify and nudge if done")
        else:
            state = "goal_stopped"
            confidence = 0.95
            details.append("STOPPED - agent sat down with unresolved goal, no active shells/monitors")
            visible_errors = [m.group() for m in re.finditer(r'Error|error|failed|FAIL|FAIL|hook error', raw, re.I)]
            if visible_errors:
                details.append(f"visible errors: {', '.join(set(visible_errors[:5]))}")
    # Else: no sat_down detected, use standard working check.
    # WORKING check
    # agent_paused check overrides `has_active_tasklist` or `goal_active` → `goal_suspicious` if no spinning.
    # But if `is_actively_working` (spinning), the agent is genuinely working → stay `working`.
    # Also: if there's a monitor/shell active (babysitting GRIDSS/jobs), past-tense + no spinner is NORMAL — agent is monitoring, not stalled.
    has_monitor_in_status = bool(re.search(r'\d+ monitor', "\n".join(lines[-5:])))
    # Check status bar for active shells/monitors ("· 1 shell" or "· 1 monitor")
    has_shell_in_status = bool(re.search(r'·\s*\d+\s*(shell|monitor)', "\n".join(lines[-5:])))
    if not is_actively_working and agent_paused and (has_active_tasklist or goal_active or has_active_indicator):
        # Monitor exception: agent is babysitting, not stalled
        if has_monitor_in_status or has_active_shells_anywhere or has_shell_in_status or is_monitoring_active:
            pass  # skip goal_suspicious — handled by working/monitoring check below
        else:
            state = "goal_suspicious"
            confidence = 0.5
            details.append("past-tense marker but no spinning — check if agent needs nudge")
    elif has_active_tasklist or goal_active or has_active_indicator or has_bullet_after_prompt or has_processing_after_prompt or is_actively_working or has_active_shells_anywhere or has_user_input_after_prompt or is_monitoring_active:
        state = "working"
        confidence = 0.8
        detail_parts = ["activity after last prompt (working)"]
        if has_user_input_after_prompt:
            detail_parts.append("user command waiting below prompt — active input")
        if is_monitoring_active:
            detail_parts.append("active monitoring (babysitting/monitor event) — NOT idle")
        details.extend(detail_parts)

        # Capture the spinning text for the details so the cron LLM can report WHAT it's doing
        spin_match = re.search(r'(✢|·|✻|✽|✸)\s+([\w\s,]+?)\s*…\s*\((\d+[sm])', raw)
        has_spinner = False
        if spin_match:
            has_spinner = True
            spin_text = spin_match.group(2).strip()
            spin_time = spin_match.group(3)
            details.append(f"spinning: {spin_text}… ({spin_time})")
        # Capture Hermes TUI reasoning line
        tui_match = re.search(r'⚕\s+(\S+).*?(\d+/[\dK]+)│.*?(\d+%)', "\n".join(lines[-5:]))
        if tui_match:
            details.append(f"Hermes TUI: {tui_match.group(1)} {tui_match.group(2)} {tui_match.group(3)}")

        # If working AND goal active, mark as goal_active for time-based tracking
        if goal_active:
            state = "goal_active"
            confidence = max(confidence, 0.9)
            details.append(f"working — /goal active ({goal_deadline or 'no deadline'})")

        # Also detect monitors running even when idle or suspicious (e.g. "1 monitor · ← for agents")
        # FIX #2: If babysitting/monitor event text was detected, stay "working" — don't downgrade to monitoring
    if has_monitor_in_status and state in ("idle", "working", "goal_suspicious") and not is_monitoring_active:
        if state == "idle":
            state = "monitoring"
            confidence = 0.8
            details.append("monitor active (status bar)")
        elif "monitor" not in ", ".join(details):
            details.append("monitor active")
    elif state == "idle" and is_monitoring_active:
        # Babysitting text detected but state fell through to idle — correct it
        state = "working"
        confidence = 0.85
        details.append("active monitoring (babysitting/monitor event) — NOT idle")

    # [agent_sat_down logic moved earlier via new if block]

    # Goal pending — goal accepted but agent hasn't started. Needs intervention.
    if goal_pending:
        state = "goal_pending"
        confidence = 0.95
        details.append("goal accepted but not running — needs Enter or /clear")

    if re.search(r'subagent.*running|subagent.* Xm Ys|\b[sub]agent\b.*elapsed', raw, re.I):
        state = "subagent"
        confidence = max(confidence, 0.9)
        details.append("subagent running")

    # Auto-mode subagent menu — "● main" + "◯ claude" or "◯ general-purpose"
    # This is ACTIVE work, NOT idle or needs_approval
    has_subagent_menu = bool(re.search(r'● main', raw) and re.search(r'◯ (claude|general-purpose|codex)', raw))

    # Claude Code multi-choice input menu — numbered options with "Enter to select"
    # CRITICAL: This looks like working text but is BLOCKED waiting for a keypress.
    # Matches patterns like:
    #   ❯ 1. Option A
    #   2. Option B
    #   Enter to select · ↑/↓ to navigate
    has_input_menu = bool(re.search(r'Enter to select', raw, re.I) and re.search(r'❯\s*\d+\.', raw))

    # Permission dialogs — ONLY if NOT a subagent menu
    # Must have BOTH "Bash command" and "Ask rule" + "Do you want" to avoid false positives from conversation text
    has_ask_rule = bool(re.search(r'Ask\s+rule\s+Bash\(', raw))
    has_dialog = bool(re.search(r'Do you want to proceed', raw))
    if has_ask_rule and has_dialog and not has_subagent_menu:
        state = "needs_approval"
        confidence = 0.95
        details.append("permission dialog — auto-class and auto-resolve safe ops, escalate risky")
    elif re.search(r'accept edits', raw) and not has_subagent_menu:
        state = "needs_approval"
        confidence = 0.95
        details.append("edit acceptance dialog")

    # Claude Code multi-choice input menu — BLOCKED waiting for keypress
    if has_input_menu:
        state = "blocked_waiting"
        confidence = 0.85
        details.append("Claude multi-choice input menu waiting for selection")

    # Bare prompt (idle) — only truly idle if NO subagent menu, NO user input, NO active monitoring
    if re.search(r'(❯\s*$|\$)', raw, re.MULTILINE):
        if has_subagent_menu:
            state = "subagent_menu"
            confidence = max(confidence, 0.95)
            details.append("subagent menu (● main active)")
        elif (state == "idle" or state == "empty") and not has_user_input_after_prompt and not is_monitoring_active:
            state = "idle"
            confidence = max(confidence, 0.7)
            details.append("bare prompt")
        elif has_user_input_after_prompt and state != "working":
            state = "working"
            confidence = 0.8
            details.append("bare prompt BUT user command waiting below — active input, not idle")

    # Survey overlay — Claude Code modal (box-drawing chars + "survey" OR "How is" + rating)
    # MUST match the MODAL shape to avoid FPs from task names like "benchmark survey"
    # CRITICAL: do NOT match Hermes TUI frame ("╭─ ⚕ Hermes ──") as a survey.
    # The Hermes border uses ╭─+ which looks like a Claude Code modal frame.
    # Also: old conversation text in scrollback may mention "survey" — don't match that.
    has_hermes_tui = bool(re.search(r'╭─.*⚕.*Hermes', raw) or re.search(r'⚕.*Hermes', "\n".join(lines[-5:])))
    if has_hermes_tui:
        has_survey_modal = False
    else:
        has_survey_modal = bool(re.search(r'╭.*survey', raw, re.DOTALL) or \
                                (re.search(r'survey', raw, re.I) and re.search(r'╭─+', raw)) or \
                                ("How is" in raw and re.search(r'(1|2|3|0)\s*(to rate|rating|\|)', raw)))
    if has_survey_modal:
        state = "blocked_survey"
        confidence = 0.95
        details.append("survey overlay blocking")

    # CRITICAL: Agent asking a QUESTION (waiting for human input)
    # Patterns: "Want me to...", "Should I...", "Which...", "What would you...", "Shall I..."
    # These look like "idle" (bare prompt) but are actually BLOCKED waiting for a decision.
    # This was the #1 misclassification that caused the old supervisor to miss stuck agents.
    agent_question_patterns = [
        r'Want\s+(me\s+)?to\b',
        r'Should\s+I\b',
        r'Shall\s+I\b',
        r'Which\s+approach',
        r'^\s*Which\?\s*$',
        r'Which\s+one\b',
        r'Which\s+do\s+you',
        r'What\s+would\s+you\s+(like\s+)?prefer',
        r'Would\s+you\s+(like\s+)?to\b',
        r'Go\s+ahead',
        r'Let\s+me\s+know\b',
        r'^\s*What\b.*\?\s*$',
        r'\bA\b.*\bB\b.*Which',  # "Option A... Option B... Which?"
        # Multi-choice question: proposes options with "confirm" or "pick" action
        r'confirm.*recommendation',
        r'pick[a-z/]+\b.*each',
        # Bare "pick" or "confirm" followed by a question-like structure
        r'pick\s+(a|b|c)\b',
        r'confirm\s+(both|my|the|this|that)\s+recommendation',
    ]
    is_question = False
    for pat in agent_question_patterns:
        if re.search(pat, raw, re.I):
            # Must be near the end (not from ancient output)
            # Check last 40 lines for the question — increased from 30 to catch
            # longer conversations where the question is separated from the prompt
            # by status bars, task lists, etc.
            recent_lines = lines[-min(40, len(lines)):]
            # Search LINE BY LINE so ^ and $ match line boundaries correctly
            matched = False
            for line in recent_lines:
                if re.search(pat, line, re.I):
                    matched = True
                    break
            if matched:
                is_question = True
                details.append(f"agent asking question (pattern: {pat})")
                break

    if is_question and state in ("idle", "empty"):
        # CRITICAL: do NOT override truly active work states (subagent_menu, subagent,
        # goal_stopped, monitoring) as blocked_waiting — question text may be from
        # old scrollback. However, when state is "working" but there's NO spinning indicator
        # (agent sat at prompt after past-tense marker), a recent question IS real.
        state = "blocked_waiting"
        confidence = 0.9
        details.append("STUCK — agent waiting for human decision, not truly idle")

    if is_question and state == "working" and not is_actively_working:
        # Agent is "working" (has goal/tasklist) but NOT actively spinning —
        # meaning it finished a turn and is now at the prompt with an unanswered question.
        # The "working" came from goal_active or tasklist, not from actual current activity.
        state = "blocked_waiting"
        confidence = 0.85
        details.append("BLOCKED — agent has active goal/task but stopped, waiting for decision")

    # Extract visible pending tasks (for nudge targeting)
    pending_tasks = []
    for line in raw.split("\n"):
        # Pattern: "◻ Task N: Description" or "○ Task N: Description"
        task_match = re.search(r'[◻○] Task (\d+): (.+)', line)
        if task_match:
            pending_tasks.append({"number": int(task_match.group(1)), "description": task_match.group(2).strip()[:150]})
        # Pattern: "◼ Task N: Description" (in-progress)
        task_in_progress = re.search(r'◼ Task (\d+): (.+)', line)
        if task_in_progress:
            pending_tasks.append({"number": int(task_in_progress.group(1)), "description": task_in_progress.group(2).strip()[:150], "status": "in_progress"})

    # Nudge safety: count visible /goal lines NOT yet showing as active
    goal_line_count = sum(1 for line in raw.split("\n") if re.search(r'/goal\b', line))
    # Subtract 1 if goal IS active (that's the footer marker, not a stacked command)
    stacked_goal_count = max(0, goal_line_count - (1 if goal_active else 0))

    # Parse goal_elapsed into seconds for frozen detection
    goal_elapsed_seconds = None
    if goal_active and goal_deadline:
        elapsed_str = goal_deadline.strip()
        hours = int(re.findall(r'(\d+)h', elapsed_str)[0]) if re.findall(r'(\d+)h', elapsed_str) else 0
        minutes = int(re.findall(r'(\d+)m', elapsed_str)[0]) if re.findall(r'(\d+)m', elapsed_str) else 0
        seconds_val = int(re.findall(r'(\d+)s', elapsed_str)[0]) if re.findall(r'(\d+)s', elapsed_str) else 0
        goal_elapsed_seconds = hours * 3600 + minutes * 60 + seconds_val

    # STATELESS SUSPICION — only if state is STILL "goal_active" after all blocking patterns checked
    # (i.e. the pane is clean of dialogs/surveys/questions but has been "active" too long)
    # Progressive alerting:
    #   > 15 min  → suspicious (flags for deeper review by supervisor)
    #   > 30 min  → stale (supervisor should escalate to user)
    #   > 60 min  → frozen (action engine intervenes with nudge)
    if state == "goal_active" and goal_elapsed_seconds and goal_elapsed_seconds > 900:  # > 15 min
        has_recent_activity = bool(re.search(r'(●|◐|◑|Cooking|Boogieing|Brewed|Fermenting|Roosting|Crunching|Forging|Created|Writing|Read \d+ file)', raw))
        if not has_recent_activity:
            if goal_elapsed_seconds > 3600:  # > 60 min
                state = "goal_frozen"
                confidence = 0.95
                details.append("FROZEN — goal active {} with no recent activity — action engine should intervene".format(goal_deadline))
            elif goal_elapsed_seconds > 1800:  # > 30 min
                state = "goal_stale"
                confidence = 0.9
                details.append("STALE — goal active {} with no recent activity — escalate to user".format(goal_deadline))
            else:  # > 15 min
                state = "goal_suspicious"
                confidence = 0.8
                details.append("SUSPICIOUS — goal active {} with no recent activity — needs deeper review".format(goal_deadline))

    return {
        "state": state,
        "confidence": confidence,
        "ctx_used": ctx_used,
        "goal_active": goal_active,
        "goal_pending": goal_pending,
        "goal_deadline": goal_deadline,
        "goal_elapsed_seconds": goal_elapsed_seconds,
        "details": details if details else ["raw inspection"],
        "last_lines": [l[:120] for l in lines[-5:] if l.strip()],
        "pending_tasks": pending_tasks,
        "has_subagent_menu": has_subagent_menu,
        "line_count": len(lines),
        "last_question": next((d for d in details if "asking question" in d), None),
        # Nudge safety: count visible /goal lines that are NOT active
        "stacked_goals": stacked_goal_count,
        # Stale working detection: True if there's a spinner
        "has_spinner": has_spinner,
    }


def agent_bus_status():
    """Query the agent bus SQLite DB directly."""
    try:
        conn = sqlite3.connect(BUS_DB)
        conn.row_factory = sqlite3.Row

        agents = conn.execute("SELECT * FROM agents ORDER BY window_id").fetchall()
        now = datetime.datetime.now(datetime.timezone.utc)

        result = []
        for a in agents:
            d = dict(a)
            try:
                lb = datetime.datetime.fromisoformat(d.get('last_heartbeat', ''))
                age_min = (now - lb).total_seconds() / 60
            except:
                age_min = -1
            d["heartbeat_age_min"] = round(age_min, 1)
            result.append(d)

        pending = conn.execute(
            "SELECT from_agent, to_agent, priority, content, created_at, id "
            "FROM messages WHERE status='pending' ORDER BY created_at"
        ).fetchall()
        d_pending = [dict(m) for m in pending]

        work = conn.execute(
            "SELECT * FROM work_queue ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        d_work = [dict(w) for w in work]

        conn.close()
        return {"agents": result, "pending_messages": d_pending, "work_queue": d_work}
    except Exception as e:
        return {"error": str(e)}


def slurm_jobs():
    """Get SLURM jobs for this user across ALL clusters (gpu + htc)."""
    combine_args = ["-M", "gpu,htc"]
    try:
        result = subprocess.run(
            ["squeue", "-u", "alc376"] + combine_args +
            ["-o", "%.18i %.10P %.30j %.6T %.20D %.10N %R"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout.strip()
        all_jobs = []
        for line in output.split("\n"):
            if "CLUSTER:" in line or not line.strip() or line.startswith("JOBID"):
                continue
            parts = line.split()
            if len(parts) < 6:
                continue
            if not parts[0].replace("_", "").replace("-", "").isdigit():
                continue
            reason = parts[7] if len(parts) > 7 else ""
            all_jobs.append({
                "job_id": parts[0],
                "partition": parts[1],
                "name": parts[2][:40],
                "state": parts[3],
                "nodes": parts[4],
                "node_list": parts[5],
                "reason": reason,
            })
        return all_jobs
    except:
        return []


def graph_summary():
    """Quick summary from knowledge graph."""
    try:
        conn = sqlite3.connect(GRAPH_DB)
        conn.row_factory = sqlite3.Row
        active = conn.execute(
            "SELECT project, COUNT(*) as c FROM notes WHERE status='active' GROUP BY project"
        ).fetchall()
        blocked = conn.execute(
            "SELECT project, COUNT(*) as c FROM notes WHERE status='blocked' GROUP BY project"
        ).fetchall()
        # NEW: find blocked notes whose dependencies are now all completed (unlocked)
        blocked_notes = conn.execute("""
            SELECT n.id, n.project, n.summary, n.type, n.source_path
            FROM notes n
            WHERE n.status = 'blocked'
            ORDER BY n.updated DESC
        """).fetchall()
        unlocked = []
        for bn in blocked_notes:
            deps = conn.execute("""
                SELECT e.target_id, n2.status, n2.summary, n2.project
                FROM graph_edges e
                LEFT JOIN notes n2 ON e.target_id = n2.id
                WHERE e.source_id = ? AND e.relation_type = 'depends_on'
            """, (bn["id"],)).fetchall()
            all_met = True
            blockers = []
            for dep in deps:
                dep_status = dep["status"] if dep["status"] else None
                if dep_status and dep_status != 'completed':
                    all_met = False
                    blockers.append({"id": dep['target_id'], "status": dep_status, "summary": dep['summary'][:100]})
            if all_met and blockers:  # was blocked, deps all resolved
                unlocked.append({
                    "id": bn["id"],
                    "project": bn["project"],
                    "summary": bn["summary"][:200],
                    "source_path": bn.get("source_path"),
                })

        # NEW: find completed notes (recently finished)
        recent_completed = conn.execute("""
            SELECT id, project, summary
            FROM notes WHERE status='completed'
            ORDER BY updated DESC LIMIT 10
        """).fetchall()

        conn.close()
        result = {
            "active_by_project": {dict(r)["project"]: dict(r)["c"] for r in active},
            "blocked_by_project": {dict(r)["project"]: dict(r)["c"] for r in blocked},
            "unlocked_items": unlocked,
            "recent_completed": [{"id": dict(r)["id"], "project": dict(r)["project"], "summary": dict(r)["summary"][:150]} for r in recent_completed],
        }
        return result
    except Exception as e:
        return {"error": str(e)}


# ==============================================================================
# NEW v2 collectors
# ==============================================================================

def slurm_job_transitions():
    """Detect SLURM job state transitions via sacct (last 12 hours).
    Returns jobs that recently COMPLETED, FAILED, or CANCELLED.
    
    NOTE: sacct --states is NOT supported in SLURM 23.11.10.
    Use sacct -S to get recent job history and filter client-side."""
    try:
        since = (datetime.datetime.now() - datetime.timedelta(hours=12)).strftime("%Y-%m-%d")
        result = subprocess.run(
            ["sacct", "-u", "alc376", "-M", "gpu,htc",
             "-o", "JobID,JobName,State,ExitCode,Elapsed,Start",
             f"--starttime={since}"],
            capture_output=True, text=True, timeout=5
        )
        transitions = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip() or line.strip().startswith("JobID"):
                continue
            parts = line.split()
            if len(parts) < 6:
                continue
            state = parts[2]
            # Client-side filter for terminal states
            if state not in ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "PREEMPTED"):
                continue
            # Skip step sub-jobs that are just batch/extern wrappers
            job_id = parts[0]
            if "+" in job_id and job_id.endswith(".+"):
                continue
            transitions.append({
                "job_id": job_id,
                "name": parts[1][:40],
                "state": state,
                "exit_code": parts[3] if len(parts) > 3 else "",
                "elapsed": parts[4] if len(parts) > 4 else "",
                "start": parts[5] if len(parts) > 5 else "",
            })
        # Cap transitions — 736 entries blows past vLLM context limit.
        # Fleet supervisor cares about tmux pane state, not SLURM history.
        # Keep only the most recent terminal-state transitions (max 30).
        # Sort by job_id descending (sacct already returns newest first,
        # but be explicit) and take the tail.
        MAX_TRANSITIONS = 30
        transitions = transitions[:MAX_TRANSITIONS]
        return transitions
    except:
        return []


def slurm_stderr_scan(job_ids=None):
    """Scan SLURM output files for recent errors (last 30 min).
    Check common output locations."""
    errors = []
    if not job_ids:
        return errors

    # Known output directories
    output_dirs = [
        f"{PROJECTS_ROOT}/slurm_output",
        f"{PROJECTS_ROOT}/Path2Space/slurm",
        f"{PROJECTS_ROOT}/TUSV-ext",
        f"{PROJECTS_ROOT}/PhyloSchlag",
        f"{PROJECTS_ROOT}/OvarianSuppressionProject",
        PROJECTS_ROOT,
    ]

    error_patterns = [
        re.compile(r"(?i)(Traceback|FATAL|ERROR.*Exception|KeyboardInterrupt)"),
        re.compile(r"(?i)(Segmentation fault|Out of memory|Killed by signal)"),
    ]

    now_ts = int(datetime.datetime.now().timestamp())
    cutoff = now_ts - 1800  # 30 min

    for job_id_short in job_ids:
        # Strip array indices for filename matching
        base = job_id_short.split("_")[0].split(".")[0]
        for d in output_dirs:
            if not os.path.isdir(d):
                continue
            for pattern in [f"slurm-{base}*.{out}", f"*_{base}*.{out}", f"*{base}*.{out}"]:
                for ext in ["out", "err"]:
                    for fp in glob.glob(os.path.join(d, pattern.replace("{out}", ext))):
                        try:
                            mtime = os.path.getmtime(fp)
                            if mtime > cutoff:
                                with open(fp, "r", errors="replace") as fh:
                                    for line in fh:
                                        for pat in error_patterns:
                                            if pat.search(line):
                                                errors.append({
                                                    "job_id": job_id_short,
                                                    "file": fp,
                                                    "line": line.strip()[:200],
                                                    "modified": datetime.datetime.fromtimestamp(mtime).isoformat(),
                                                })
                                                break  # one error per file is enough
                                                break
                        except:
                            pass
    return errors


def git_diff_summary(window_notes_map):
    """For each project dir with git, check if there are uncommitted changes.
    Returns quick git status for each project.
    
    IMPORTANT: Use `git status --porcelain` instead of `git diff --stat`.`
    `git diff` compares file contents and can take 3-13s on repos with large
    SVG/PNG diffs (CITEgeist, Path2Space). `git status` only checks the index
    metadata — instant (<0.1s). Timeout kept at 2s per repo — the overall
    collect script must finish in <20s total."""
    results = {}
    # Map project to git root
    known_projects = {
        "PhyloSchlag": f"{PROJECTS_ROOT}/PhyloSchlag",
        "PhyloSchlag-Macro": f"{PROJECTS_ROOT}/PhyloSchlag",
        "CITEgeist": f"{PROJECTS_ROOT}/CITEgeist",
        "OFS": f"{PROJECTS_ROOT}/OvarianSuppressionProject",
        "TUSV-ext": f"{PROJECTS_ROOT}/TUSV-ext",
        "Path2Space": f"{PROJECTS_ROOT}/Path2Space",
    }
    for proj, path in known_projects.items():
        try:
            # git status --porcelain is instant (metadata only, no content diff)
            result = subprocess.run(
                ["git", "status", "--porcelain", "--branch"],
                capture_output=True, text=True, timeout=2, cwd=path
            )
            if result.stdout.strip():
                lines = result.stdout.strip().splitlines()
                # Skip branch header line and count changes by type
                staged = untracked = other = 0
                for line in lines:
                    if line.startswith("## "):
                        continue
                    first_two = line[:2]
                    if first_two[0] in ("A", "M", "D", "R", "C"):  # staged
                        staged += 1
                    elif first_two[0] == "?" or first_two[1] == "?":  # untracked
                        untracked += 1
                    else:
                        other += 1
                parts = []
                if staged:
                    parts.append(f"{staged} staged")
                if other:
                    parts.append(f"{other} modified")
                if untracked:
                    parts.append(f"{untracked} untracked")
                results[proj] = ", ".join(parts)
        except:
            pass
    return results


def read_pacing():
    """Read pacing state (adaptive scheduling)."""
    default = {
        "consecutive_idle": 0,
        "current_interval_min": 5,
        "last_state_change": None,
        "last_updated": None,
    }
    try:
        if os.path.exists(PACING_PATH):
            with open(PACING_PATH) as f:
                data = json.load(f)
            # Merge with defaults for new fields
            for k, v in default.items():
                data.setdefault(k, v)
            return data
    except:
        pass
    return default


def write_pacing(pacing_data, state_changed):
    """Update pacing state after diffing."""
    if state_changed:
        pacing_data["consecutive_idle"] = 0
    else:
        pacing_data["consecutive_idle"] += 1

    now_iso = datetime.datetime.now().isoformat()
    if state_changed:
        pacing_data["last_state_change"] = now_iso

    # Compute recommended interval
    # 0-2 consecutive idle: 5 min (busy monitoring)
    # 3-5 consecutive idle: 15 min
    # 6+ consecutive idle: 30 min
    idle_count = pacing_data["consecutive_idle"]
    now_hour = datetime.datetime.now().hour
    if 1 <= now_hour <= 6:
        # Off-hours — relaxation
        pacing_data["current_interval_min"] = min(30, max(15, 5 + idle_count * 5))
    else:
        if idle_count >= 6:
            pacing_data["current_interval_min"] = 30
        elif idle_count >= 3:
            pacing_data["current_interval_min"] = 15
        else:
            pacing_data["current_interval_min"] = 5

    pacing_data["last_updated"] = now_iso
    try:
        with open(PACING_PATH, "w") as f:
            json.dump(pacing_data, f, indent=2)
    except:
        pass
    return pacing_data


def handoff_queue_aging():
    """Scan handoff queue for stale items."""
    if not os.path.isdir(HANDOFF_QUEUE):
        return {"stale": [], "dead": [], "total": 0}

    now_ts = int(datetime.datetime.now().timestamp())
    cutoff_24h = now_ts - 86400      # 24 hours
    cutoff_72h = now_ts - 259200     # 72 hours

    stale = []
    dead = []
    total = 0

    for fp in sorted(glob.glob(os.path.join(HANDOFF_QUEUE, "*.md"))):
        total += 1
        try:
            mtime = os.path.getmtime(fp)
            name = os.path.basename(fp)
            age_hours = (now_ts - mtime) / 3600
            entry = {"file": name, "age_hours": round(age_hours, 1)}
            if mtime < cutoff_72h:
                dead.append(entry)
            elif mtime < cutoff_24h:
                stale.append(entry)
        except:
            pass

    return {"stale": stale, "dead": dead, "total": total}


def session_markers():
    """Read session markers for auto window-notes reconciliation."""
    markers = {}
    if not os.path.isdir(MARKERS_DIR):
        return markers
    for fp in sorted(glob.glob(os.path.join(MARKERS_DIR, "window_*.session"))):
        idx = os.path.basename(fp).replace("window_", "").replace(".session", "")
        try:
            with open(fp) as f:
                content = f.read().strip()
            parts = content.split("|")
            markers[idx] = {"raw": content[:200], "path": parts[1] if len(parts) > 1 else ""}
        except:
            pass
    return markers


def detect_cli_presence():
    """Detect if user is at the keyboard (CLI session active).

    Returns:
        (present: bool, seconds_since_heartbeat: float, recommended_delivery: str)

    Recommended delivery:
        - "origin" if user is present (deliver to CLI/origin chat))
        - "telegram:8694703431" if user is away
    """
    now_ts = datetime.datetime.now().timestamp()

    # Primary: heartbeat file (written by CLI session every turn)
    heartbeat_age = None
    try:
        if os.path.exists(CLI_HEARTBEAT):
            mtime = os.path.getmtime(CLI_HEARTBEAT)
            heartbeat_age = now_ts - mtime
    except:
        pass

    # Fallback: check if TUI process is alive
    tui_alive = False
    try:
        result = subprocess.run(
            ["pgrep", "-f", "hermes$"],  # hermes TUI binary
            capture_output=True, text=True, timeout=3
        )
        tui_alive = bool(result.stdout.strip())
    except:
        pass

    present = False
    if heartbeat_age is not None and heartbeat_age < CLI_PRESENCE_THRESHOLD * 60:
        present = True
    elif tui_alive and heartbeat_age is not None and heartbeat_age < 60 * 30:
        # TUI alive + heartbeat within 30 min (grace period)
        present = True

    delivery = "origin" if present else "telegram:8694703431"

    return {
        "cli_present": present,
        "heartbeat_age_seconds": round(heartbeat_age or -1, 1),
        "tui_alive": tui_alive,
        "recommended_delivery": delivery,
    }


# ==============================================================================
# Main
# ==============================================================================

def discover_windows():
    """Auto-discover all open tmux windows in the session.

    Replaces the static window list. Callers can still override with
    --windows for targeted scans, but the default is now "everything open."
    """
    try:
        result = subprocess.run(
            [TMUX_BIN, "-S", TMUX_SOCKET, "list-windows", "-t", SESSION, "-F", "#{window_index}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            indices = [
                w.strip()
                for w in result.stdout.strip().split("\n")
                if w.strip() and w.strip() in ALLOWED_WINDOWS
            ]
            if indices:
                return sorted(indices, key=int)
    except Exception:
        pass
    # Fallback: nothing discovered — caller will handle the empty list
    return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows", default=None,
                        help="Window override — omit to auto-discover all open windows")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.windows:
        windows = [w.strip() for w in args.windows.split(",")]
    else:
        windows = discover_windows()

    # Read pacing state
    pacing = read_pacing()

    output = {
        "timestamp": datetime.datetime.now().isoformat(),
        "host": "gpu-n89.crc.pitt.edu",
        "fleet": {},
        "agent_bus": agent_bus_status(),
        "slurm": slurm_jobs(),
        "graph": graph_summary(),
        # v2 additions
        "slurm_transitions": slurm_job_transitions(),
        "git_diffs": git_diff_summary({}),
        "pacing": pacing,
        "handoff_queue": handoff_queue_aging(),
        "session_markers": session_markers(),
        # v3: CLI presence detection for autodeliver
        "cli_presence": detect_cli_presence(),
    }

    # --- Window change detection (new vs closed) ---
    prev_state_path = os.path.join(VAULT, "lab/agents/supervisor/_status.json")
    prev_windows = set()
    _prev = {}
    try:
        if os.path.exists(prev_state_path):
            with open(prev_state_path) as _f:
                _prev = json.load(_f)
            prev_windows = set(_prev.get("fleet", {}).keys())
    except Exception:
        pass

    current_window_set = set(windows)
    def _sort_key(w):
        try:
            return int(w)
        except (ValueError, TypeError):
            # Handle keys like "W8" by stripping the prefix
            return int(str(w).lstrip("W"))
    appeared_windows = sorted(current_window_set - prev_windows, key=_sort_key)
    disappeared_windows = sorted(prev_windows - current_window_set, key=_sort_key)

    # Load nudge counts from previous cycle (for safety gating)
    prev_nudge_counts = {}
    try:
        for pw, pinfo in _prev.get("fleet", {}).items():
            if isinstance(pinfo, dict):
                prev_nudge_counts[pw] = pinfo.get("nudge_count", 0)
    except Exception:
        pass

    output["window_changes"] = {
        "appeared": appeared_windows,
        "disappeared": disappeared_windows,
        "any_change": bool(appeared_windows or disappeared_windows),
    }

    # Collect per-window
    for w in windows:
        if w not in ALLOWED_WINDOWS:
            output["fleet"][w] = {"error": "window not in allowed list"}
            continue

        wname = tmux_window_name(w)
        raw = tmux_capture(w)  # 50 lines: current state bar, prompt, spinner, recent output
        state = detect_pane_state(raw)

        # Nudge count: carry forward from previous cycle, increment if nudged
        prev_nudge_count = 0
        if prev_nudge_counts and w in prev_nudge_counts:
            prev_nudge_count = prev_nudge_counts[w]
        # Reset if goal became active (nudge worked)
        if state["goal_active"]:
            current_nudge_count = 0
        else:
            current_nudge_count = prev_nudge_count

        output["fleet"][w] = {
            "window": int(w),
            "name": wname,
            "state": state["state"],
            "confidence": state["confidence"],
            "ctx_used": state["ctx_used"],
            "goal_active": state["goal_active"],
            "goal_pending": state["goal_pending"],
            "goal_deadline": state["goal_deadline"],
            "details": state["details"],
            "last_lines": state["last_lines"],
            # For escalated windows, include deeper last_lines (last 30) for context
            # Covers: blocked states + stalled states (goal_stopped/frozen/stale/suspicious)
            "last_lines_deep": [l[:200] for l in (raw or "").split("\n")[-30:] if l.strip()] if state["state"] in ("needs_approval", "blocked_waiting", "goal_pending", "blocked_survey", "goal_stopped", "goal_frozen", "goal_stale", "goal_suspicious") else [],
            "raw": raw[:2000] if raw else "",
            "nudge_count": current_nudge_count,
            "stacked_goals": state.get("stacked_goals", 0),
        }

    if args.json:
        # Determine if state changed vs previous cycle
        prev_state_path = os.path.join(VAULT, "lab/agents/supervisor/_status.json")
        state_changed = True  # default to changed if no previous state

        # Compact representation for delta comparison
        current_compact = {
            "fleet_states": {},
            "slurm_running": [],
            "slurm_pending": [],
        }
        for w, info in output["fleet"].items():
            if isinstance(info, dict) and "state" in info:
                current_compact["fleet_states"][w] = {
                    "state": info["state"],
                    "ctx_used": info["ctx_used"],
                    "goal_active": info["goal_active"],
                }
        current_compact["slurm_running"] = [j["job_id"] for j in output["slurm"] if j.get("state") == "RUNNING"]
        current_compact["slurm_pending"] = [j["job_id"] for j in output["slurm"] if j.get("state") == "PENDING"]

        # Compare with previous
        try:
            if os.path.exists(prev_state_path):
                with open(prev_state_path) as f:
                    prev = json.load(f)
                prev_compact = prev.get("_compact", {})
                state_changed = (current_compact != prev_compact)
        except:
            state_changed = True

        # Update pacing
        pacing_out = write_pacing(pacing, state_changed)

        # Save full state + compact for next cycle
        save_output = dict(output)
        save_output["_compact"] = current_compact
        save_output["_pacing"] = pacing_out
        save_output["_state_changed"] = state_changed
        try:
            with open(prev_state_path, "w") as f:
                json.dump(save_output, f, indent=2, default=str)
        except Exception:
            pass

        # Add escalation summaries for windows needing user attention
        # ONLY escalate windows the collector explicitly flagged — do NOT add extra windows
        # based on keyword scanning. The collector has already run proper state detection.
        # Includes: blocked states (needs_approval, blocked_waiting, blocked_survey, goal_pending)
        # AND stalled states (goal_stopped, goal_frozen, goal_stale, goal_suspicious)
        # AND idle-with-errors (idle window that has visible error traces = job may have failed)
        escalated_windows = []
        for w, info in output["fleet"].items():
            if not isinstance(info, dict):
                continue
            state = info.get("state")
            is_escalated_state = state in ("needs_approval", "blocked_waiting", "blocked_survey", "goal_pending", "goal_stopped", "goal_frozen", "goal_stale", "goal_suspicious")
            # Also escalate idle windows with visible errors (job failed, agent sat idle without monitor)
            is_idle_with_errors = state == "idle" and bool(re.search(r"(?i)(error|failed|Traceback|Exception)", info.get("raw", "")))
            if not (is_escalated_state or is_idle_with_errors):
                continue

            # Use deep lines for escalated windows (last 30 lines of context)
            deep_lines = info.get("last_lines_deep", info.get("last_lines", []))
            summary_lines = []
            for line in deep_lines:
                if any(kw in line.lower() for kw in ["do you want", "proceed", "want me to", "which", "should i", "what would", "decide", "option", "yes", "no", "permission", "approve", "recommend"]):
                    summary_lines.append(line[:200])
            if not summary_lines and deep_lines:
                summary_lines = deep_lines[-5:]
            # Also include recent findings/results from the deep lines
            findings = []
            for line in deep_lines:
                line_lower = line.lower()
                if any(kw in line_lower for kw in ["result", "finding", "conclusion", "verdict", "no-go", "go", "spearman", "accuracy", "p-value", "recommendation", "next step", "phase"]):
                    findings.append(line[:200])
            escalated_windows.append({
                "window": int(w),
                "name": info.get("name", ""),
                "state": info["state"],
                "ctx_used": info.get("ctx_used"),
                "summary_lines": summary_lines,
                "recent_findings": findings[:10],
                "goal_active": info.get("goal_active"),
                "goal_pending": info.get("goal_pending"),
            })
        output["escalation_summaries"] = escalated_windows

        print(json.dumps(output, indent=2, default=str))
    else:
        # Human-readable summary
        for w, info in sorted(output["fleet"].items(), key=lambda x: x[0]):
            if isinstance(info, dict) and "state" in info:
                ctx_str = f"{info['ctx_used']}%" if info['ctx_used'] is not None else "?%"
                goal_str = f"◎{info['goal_deadline'] or ''}" if info['goal_active'] else ""
                print(f"  w{w} [{ctx_str} {goal_str}] {info['state']} ({info['details']})")
                if info.get('name'):
                    print(f"    name: {info['name']}")
                for line in info.get('last_lines', [])[-3:]:
                    print(f"    > {line}")

        print(f"\n  SLURM: {len(output['slurm'])} jobs")
        for j in output['slurm']:
            reason_str = f" ({j['reason']})" if j.get('reason') else ""
            print(f"    {j['job_id']} {j['state']} on {j['partition']} {j['name']} node={j.get('node_list','')}{reason_str}")

        # v2 extras
        if output['slurm_transitions']:
            print(f"\n  RECENT TRANSITIONS: {len(output['slurm_transitions'])}")
            for t in output['slurm_transitions']:
                print(f"    {t['job_id']} {t['name']} -> {t['state']} (exit={t['exit_code']}, elapsed={t['elapsed']})")

        if output['git_diffs']:
            print(f"\n  GIT CHANGES:")
            for proj, diff in output['git_diffs'].items():
                print(f"    {proj}: {diff}")

        hq = output.get('handoff_queue', {})
        if hq.get('stale') or hq.get('dead'):
            print(f"\n  HANDOFF QUEUE: {hq['total']} total, {len(hq.get('stale',[]))} stale(>24h), {len(hq.get('dead',[]))} dead(>72h)")

        p = output.get('pacing', {})
        print(f"\n  PACING: consecutive_idle={p.get('consecutive_idle',0)}, interval={p.get('current_interval_min',5)}min")

        g = output.get('graph', {})
        if g.get('unlocked_items'):
            print(f"\n  GRAPH UNLOCKED ({len(g['unlocked_items'])}):")
            for item in g['unlocked_items']:
                print(f"    [{item['project']}] {item['summary'][:120]}")
        if g.get('blocked_by_project'):
            total_blocked = sum(g['blocked_by_project'].values())
            if total_blocked > 0:
                print(f"\n  GRAPH BLOCKED: {total_blocked} notes ({', '.join(f'{k}={v}' for k,v in g['blocked_by_project'].items())})")


if __name__ == "__main__":
    main()
