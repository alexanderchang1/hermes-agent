#!/usr/bin/env python3
"""
Fleet supervisor — structured collector with compact output for minimal LLM overhead.

Output modes:
  SILENT — nothing to report, no escalations
  REPORT — formatted fleet status (every 3 cycles)
  ESCALATION — escalation Windows needing cockpit action
  WRAPUP — window(s) at 60min idle, need in-cockpit verification
  IDLE_NUDGE — window(s) at 45min stuck, need in-cockpit nudge

The LLM agent reads this and takes minimal action:
  - SILENT: output LOGGED
  - REPORT: output the report verbatim
  - ESCALATION: capture panes and act
  - WRAPUP: capture panes and decide /wrap-up or skip
  - IDLE_NUDGE: capture panes and nudge or escalate
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import glob
from datetime import datetime, timezone

# Allow importing sibling scripts (supervisor-goals.py)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Paths
VAULT = "/ix1/alee/LO_LAB/Personal/Alexander_Chang/alc376/vault"
COLLECT_SCRIPT = os.path.join(VAULT, "lab/agents/supervisor/scripts/hermes-supervisor-collect.py")
PACING_PATH = os.path.join(VAULT, "lab/agents/supervisor/pacing.json")
COORD_LOG = os.path.join(VAULT, "lab/agents/supervisor/coordination-log.md")
CYCLE_COUNTER_FILE = os.path.join(VAULT, "lab/agents/supervisor/cycle-counter")
IDLE_STATE_FILE = os.path.join(VAULT, "lab/agents/supervisor/_idle_state.json")
WRAPUP_STATE_FILE = os.path.join(VAULT, "lab/agents/supervisor/_wrapup_state.json")
STALE_STATE_FILE = os.path.join(VAULT, "lab/agents/supervisor/_stale_state.json")
ESC_STATE_FILE = os.path.join(VAULT, "lab/agents/supervisor/_esc_state.json")
# Escalation suppression — don't re-fire same window for 30 min (circuit breaker)
ESC_SUPPRESS_MINUTES = 30

WRAPUP_STATE_FILE = os.path.join(VAULT, "lab/agents/supervisor/_wrapup_state.json")
STALE_STATE_FILE = os.path.join(VAULT, "lab/agents/supervisor/_stale_state.json")
STALE_WORKING_FILE = os.path.join(VAULT, "lab/agents/supervisor/_stale_working_state.json")
REPORT_EVERY_N = 3  # Report every 3 cycles (= every 15 min)
STALE_THRESHOLD = 9  # 9 consecutive unchanged panes = 45 min → fire wrap-up
# Stale working detection: working state + NO spinner for this many cycles → flag
STALE_WORKING_THRESHOLD = 3  # 3 cycles = 15 min without spinner while "working" → likely stuck

# Context refresh: when ctx > N%, trigger /wrap-up → /clear → /orient → /brainstorming
HIGH_CTX_THRESHOLD = 40  # ctx_used > 40% → trigger context refresh cycle
HIGH_CTX_COOLDOWN = 1800  # 30 min between refreshes per window (avoid hammering)

# Timeouts
CODEX_RESOLVE_AFTER_MIN = 15  # If window waiting >15 min with no user input → auto-resolve via Codex
CODEX_RESOLVE_LAST_ATTEMPT_JSON = os.path.join(VAULT, "lab/agents/supervisor/_codex_resolve_last.json")
CODEX_RESOLVE_COOLDOWN = 300  # 5 min cooldown between resolve attempts per window
WRAPUP_TIMEOUT_MINUTES = 60
IDLE_TIMEOUT_MINUTES = 45
# Transient completed states — task finished but still "working" (spinner done, sitting at prompt)
# These get the /wrap-up → /clear → /orient → /brainstorming refresh
NATURAL_END_THRESHOLD = 30  # Windows stuck in a task with pattern = "N tasks (all done/new…)" + spinner + ACTIVE_TASK = "Monitor event" + ACTIVE_GOAL = "brainstorm" + ACTIVE_PROJECT = "digital"
NATURAL_END_COOLDOWN = 3600  # Don't re-fire on the same window for 1 hour
WRAPUP_TIMEOUT_MINUTES = 60
IDLE_TIMEOUT_MINUTES = 45
TERMINAL_STATES = {"goal_stopped", "just_finished"}
# Transient waiting states — agent is temporarily blocked. Eligible for idle nudge.
NUDGEABLE_STATES = {"idle", "blocked_waiting", "blocked_survey", "needs_approval", "goal_suspicious", "empty"}
WAITING_STATES = TERMINAL_STATES | NUDGEABLE_STATES

# Tmux config
TMUX_SOCKET = "/vast/alee/alc376/tunnel-runtime/tmux/tmux-157528/default"
TMUX_BIN = os.path.expanduser("~/tmux-build/install/bin/tmux") if os.path.exists(os.path.expanduser("~/tmux-build/install/bin/tmux")) else "tmux"
SESSION = "claude-gpu"

# Gate settings
MAX_VLLM_WAIT = 60
VLLM_POLL = 5


# ─── Helpers ───────────────────────────────────────────────

def check_vllm():
    for i in range(MAX_VLLM_WAIT // VLLM_POLL):
        try:
            r = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "http://localhost:8000/v1/models",
                 "--connect-timeout", "10", "--max-time", "20"],
                capture_output=True, text=True, timeout=25,
            )
            if r.stdout.strip() == "200":
                return True
        except subprocess.TimeoutExpired:
            pass
        if i < MAX_VLLM_WAIT // VLLM_POLL - 1:
            time.sleep(VLLM_POLL)
    return False


def run_collector():
    """Run collector via shell wrapper with hard 60s timeout (avoids python3 hangs)."""
    shell_script = os.path.expanduser("~/.hermes/scripts/collect-fleet.sh")
    result = subprocess.run(
        ["bash", shell_script],
        capture_output=True, text=True, timeout=65,
    )
    out = result.stdout.strip()
    if not out:
        return None, "Collector returned empty output (timeout or failure)"
    try:
        return json.loads(out), None
    except json.JSONDecodeError:
        return None, f"Invalid JSON from collector"


def base_name(wname):
    if not wname:
        return "?"
    for suffix in ("-waiting", "-ing", "-approve", " - "):
        if wname.endswith(suffix):
            return wname[:-len(suffix)]
    return wname


def _load_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return {}


def _save_state(path: str, state: dict):
    try:
        with open(path, "w") as f:
            json.dump(state, f)
    except:
        pass


def _capture_pane_text(wid):
    """Capture visible pane content as text. Returns None on failure."""
    try:
        r = subprocess.run(
            [TMUX_BIN, "-L", TMUX_SOCKET.split("/")[-1], "capture-pane",
             "-t", f"{SESSION}:{wid}", "-p", "-q"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    except:
        pass
    return None


def _is_user_typing(pane_text):
    """Detect if user is actively typing at the Claude Code prompt.

    User typing indicators:
    - Scan ALL lines for '❯' prompt, take the LAST occurrence (most recent)
    - Typed content after prompt is NOT agent output markers (●, ✻, ⎿, ◻)
    """
    if not pane_text:
        return False
    lines = pane_text.split("\n")
    # Find the LAST prompt line (most recent interaction point), not just last 5 lines
    for line in reversed(lines):
        line = line.rstrip()
        if "❯" in line:
            after_prompt = line.split("❯", 1)[1].strip()
            # If there's content after the prompt that's NOT an agent marker
            if after_prompt and not after_prompt.startswith(("●", "✻", "⎿", "◻", "  ⎿")):
                return True
            # Found a prompt but no user content → user is NOT typing
            return False
    return False


_PANE_SNIPPET_SKIP = re.compile(r'^(❯|\xa0|\s*\u2500|\s*$|⚕|context used|Input:|❯$)')

def _pane_snippet(wid):
    """Return last 3 meaningful lines from a window pane."""
    text = _capture_pane_text(wid)
    if not text:
        return "[unreachable]"
    lines = text.split('\n')
    clean = []
    for line in reversed(lines[-20:]):
        stripped = line.strip()
        if not stripped or _PANE_SNIPPET_SKIP.search(stripped):
            continue
        if len(stripped) <= 2:
            continue
        clean.append(stripped)
        if len(clean) >= 3:
            break
    if not clean:
        return "[bare prompt or empty]"
    return ' | '.join(reversed(clean))


def _check_window_idle_activity():
    """Detect user activity by actually reading panes, not relying on stale timestamps.
    
    Returns set of window IDs where user appears to be actively typing.
    """
    typing_windows = set()
    for i in range(16):
        pane = _capture_pane_text(i)
        if _is_user_typing(pane):
            typing_windows.add(i)
    return typing_windows


def increment_cycle():
    try:
        count = 0
        if os.path.exists(CYCLE_COUNTER_FILE):
            with open(CYCLE_COUNTER_FILE) as f:
                count = int(f.read().strip())
        count += 1
        with open(CYCLE_COUNTER_FILE, "w") as f:
            f.write(str(count))
        return count, (count % REPORT_EVERY_N == 0)
    except:
        return 0, False


def log_coordination(summary):
    try:
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        with open(COORD_LOG, "a") as f:
            f.write(f"{now} {summary}\n")
    except:
        pass


# ─── State tracking ───────────────────────────────────────

def track_idle(fleet: dict) -> tuple:
    """Track idle/waiting state duration. Returns (idle_warnings, cur_state)."""
    now_epoch = time.time()
    prev_state = _load_state(IDLE_STATE_FILE)
    cur_state = {}
    idle_warnings = []
    NON_WORKING = WAITING_STATES | {"monitoring", "subagent_menu"}
    # States that, when a window transitions from them to idle, indicate "just finished"
    PREV_WORKING_STATES = {"working", "monitoring", "subagent_menu", "goal_active"}

    for wid, info in fleet.items():
        if not isinstance(info, dict) or "state" not in info:
            continue
        if str(wid) in {"0", "2"}:
            continue
        state = info["state"]
        wname = base_name(info.get("name", ""))
        state_label = state.replace("_", " ")
        is_waiting = state in WAITING_STATES
        prev_wid = prev_state.get(wid, {})
        prev_was_waiting = prev_wid.get("state") in WAITING_STATES

        # Terminal states (goal_stopped, just_finished): skip entirely — never nudge
        if state in TERMINAL_STATES:
            continue

        # Detect "just finished" transition: was working, now idle
        just_finished = False
        if is_waiting and not prev_was_waiting and state == "idle":
            prev_state_name = prev_wid.get("state", "")
            if prev_state_name in PREV_WORKING_STATES:
                just_finished = True

        if is_waiting:
            # Window is in a waiting state
            if prev_was_waiting:
                # Still waiting — preserve existing timer
                start_epoch = prev_wid.get("since", now_epoch)
            else:
                # JUST entered waiting — start timer now
                start_epoch = now_epoch

            cur_state[wid] = {"state": state, "since": start_epoch, "just_finished": just_finished}

            # TERMINAL states already skipped above.
            # NUDGEABLE states: fire idle warning after timeout
            if state in NUDGEABLE_STATES:
                elapsed_min = (now_epoch - start_epoch) / 60
                effective_timeout = IDLE_TIMEOUT_MINUTES * 2 if just_finished else IDLE_TIMEOUT_MINUTES
                if elapsed_min >= effective_timeout:
                    idle_warnings.append(f"W{wid} ({wname}) [{state_label}] idle {elapsed_min:.0f}min")
        elif prev_was_waiting:
            # Window left a waiting state — keep tracking but update state.
            cur_state[wid] = {"state": state, "since": prev_wid.get("since", now_epoch), "just_finished": False}
        else:
            # Non-waiting, non-terminal state — track for transition detection
            cur_state[wid] = {"state": state, "since": now_epoch, "just_finished": False}

    _save_state(IDLE_STATE_FILE, cur_state)
    return idle_warnings, cur_state


def _capture_pane_hash(wid):
    """Capture visible pane content and return md5 hash. Returns None on failure."""
    try:
        r = subprocess.run(
            [TMUX_BIN, "-L", TMUX_SOCKET.split("/")[-1], "capture-pane",
             "-t", f"{SESSION}:{wid}", "-p", "-q"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            return hashlib.md5(r.stdout.encode()).hexdigest()
    except:
        pass
    return None


def track_stale_working(fleet: dict) -> tuple:
    """Track 'working' windows with NO spinner — likely stuck on scrolled-away prompt.
    Returns (stale_working_flags, new_state).
    """
    now_epoch = time.time()
    prev_state = _load_state(STALE_WORKING_FILE)
    new_state = {}
    stale_flags = []

    for wid, info in fleet.items():
        if not isinstance(info, dict) or "state" not in info:
            continue
        if str(wid) in {"0", "2"}:
            continue

        state = info.get("state")
        has_spinner = info.get("has_spinner", True)
        wname = base_name(info.get("name", ""))

        # Only track windows genuinely in "working" state with NO spinner
        if state == "working" and not has_spinner:
            prev = prev_state.get(str(wid), {})
            count = prev.get("count", 0) + 1
            if count >= STALE_WORKING_THRESHOLD:
                stale_flags.append({
                    "window": wid,
                    "name": wname,
                    "cycles_without_spinner": count,
                    "description": "STALE — working but no spinner for {} cycles. Question/UI may have scrolled off-screen.".format(count),
                })
            new_state[str(wid)] = {"count": count, "since": prev.get("since", now_epoch)}
        elif state == "working" and has_spinner:
            # Agent is genuinely working — reset counter
            new_state[str(wid)] = {"count": 0, "since": now_epoch}
        # Non-working states: clear tracking
        # (they're handled by idle/wrapup tracking)

    _save_state(STALE_WORKING_FILE, new_state)
    return stale_flags, new_state


def track_high_ctx(fleet: dict) -> tuple:
    """Track windows approaching context limits. Returns (refresh_needed, new_state)."""
    now_epoch = time.time()
    prev_state = _load_state(STALE_WORKING_FILE + ".ctx")  # reuse name
    new_state = {}
    refresh_needed = []

    for wid, info in fleet.items():
        if not isinstance(info, dict) or "state" not in info:
            continue
        if str(wid) in {"0", "2"}:
            continue

        ctx = info.get("ctx_used")
        wname = base_name(info.get("name", ""))
        state = info.get("state")

        if ctx is None or ctx <= HIGH_CTX_THRESHOLD:
            new_state[str(wid)] = {"last_ctx": ctx, "last_refresh": prev_state.get(str(wid), {}).get("last_refresh", 0)}
            continue

        # CRITICAL: NEVER fire HIGH_CTX on windows that are blocked or monitoring.
        # blocked_waiting = waiting for user/external input. monitoring = babysitting SLURM.
        # The window is idle from the agent's perspective — no point wasting a /clear cycle.
        if state in ("blocked_waiting", "blocked_survey", "needs_approval", "monitoring", "goal_stopped"):
            new_state[str(wid)] = {"last_ctx": ctx, "last_refresh": prev_state.get(str(wid), {}).get("last_refresh", 0)}
            continue

        # CRITICAL: Also NEVER fire HIGH_CTX on actively working windows.
        # If the agent is spinning / babysitting / processing, high context is NORMAL — it's actively working.
        # Only fire on windows that are in a non-working state ('idle', 'empty', 'goal_suspicious') with high ctx.
        if state == "working":
            new_state[str(wid)] = {"last_ctx": ctx, "last_refresh": prev_state.get(str(wid), {}).get("last_refresh", 0)}
            continue

        refresh_needed.append({
            "window": wid,
            "name": wname,
            "ctx": ctx,
            "state": state,
            "description": "HIGH_CTX {}% — wrap up, /clear, /orient, restart /brainstorming".format(ctx),
        })
        new_state[str(wid)] = {"last_ctx": ctx, "last_refresh": now_epoch}

    _save_state(STALE_WORKING_FILE + ".ctx", new_state)
    return refresh_needed, new_state


def track_natural_end(fleet: dict) -> tuple:
    """Track windows that FINISHED their main task but still show 'working' due to monitors.
    Detects: working + spinner + task list says 'N done + 0 open + N monitoring only.'
    These should get /wrap-up → /clear → /orient → /brainstorming.
    Returns (natural_end_needed, new_state)."""
    now_epoch = time.time()
    prev_state = _load_state(CODEX_RESOLVE_LAST_ATTEMPT_JSON + ".nend")
    new_state = {}
    natural_end_needed = []

    for wid, info in fleet.items():
        if not isinstance(info, dict) or "state" not in info:
            continue
        if str(wid) in {"0", "2"}:
            continue

        state = info.get("state")
        wname = base_name(info.get("name", ""))
        pending = info.get("pending_tasks", [])
        details = info.get("details", [])
        detail_text = " ".join(details)

        # Must be working with monitoring
        if state != "working" and state != "monitoring":
            new_state[str(wid)] = prev_state.get(str(wid), {})
            continue

        # Check: task list shows done + monitoring only, no open tasks
        task_done = False
        if pending:
            all_done = all("done" in t or "monitor" in t or "monitoring" in t for t in pending)
            has_open = any("open" in t or "◻" in t for t in pending)
            if all_done and not has_open:
                task_done = True

        # Also check collector details for monitoring-only signals
        # CRITICAL: "NOT idle" means actively working — never classify as natural end
        if "NOT idle" in detail_text or "NOT idle" in str(details):
            new_state[str(wid)] = prev_state.get(str(wid), {})
            continue

        # True monitoring-only: has monitors but no actual work content
        monitor_only = ("monitor" in detail_text and "monitoring" in detail_text
                        and "actively" not in detail_text
                        and "NOT idle" not in detail_text
                        and "active monitoring" not in detail_text)

        if not (task_done or monitor_only):
            new_state[str(wid)] = prev_state.get(str(wid), {})
            continue

        # Check cooldown
        prev = prev_state.get(str(wid), {})
        last_end = prev.get("last_end", 0)
        elapsed = now_epoch - last_end
        if elapsed < NATURAL_END_COOLDOWN:
            new_state[str(wid)] = prev
            continue

        # Check if wrap-up has already been sent — if not, we need to send it first
        # CRITICAL: check the NATURAL_END tracking state for when we SENT /wrap-up,
        # NOT whether the window key exists in _wrapup_state.json (that's just idle tracking).
        prev_ne = prev.get("needs_wrapup", True)
        wrapup_sent_epoch = prev.get("wrapup_sent_epoch", 0)
        sitting_since = prev.get("sitting_since", now_epoch)
        sitting_min = (now_epoch - sitting_since) / 60

        # How long has it been in this state?
        if sitting_min < NATURAL_END_THRESHOLD:
            # First time seeing this — record start
            new_state[str(wid)] = {"sitting_since": sitting_since}
            continue

        needs_wrapup = False
        if prev_ne:
            # We haven't sent /wrap-up yet for this natural end
            needs_wrapup = True
        elif wrapup_sent_epoch == 0:
            # wrapup_sent_epoch not set — assume we need to send it first
            needs_wrapup = True
        else:
            # We sent /wrap-up — give the agent TIME to actually write handoffs
            # before we nuke the context with /clear
            # Grace period: 20 minutes (4 cycles) minimum
            GRACE_MIN = 20
            elapsed_since_wrapup = (now_epoch - wrapup_sent_epoch) / 60
            if elapsed_since_wrapup < GRACE_MIN:
                # Still in grace period — wait, don't fire /clear yet
                new_state[str(wid)] = {
                    "sitting_since": sitting_since,
                    "wrapup_sent_epoch": wrapup_sent_epoch,
                    "needs_wrapup": False,
                }
                continue
            needs_wrapup = False

        natural_end_needed.append({
            "window": wid,
            "name": wname,
            "state": state,
            "sitting_minutes": int(sitting_min),
            "needs_wrapup_first": needs_wrapup,
            "description": (
                f"NATURAL_END — sitting idle in monitor for {int(sitting_min)}min with all tasks done. "
                f"{'Needs: /wrap-up first' if needs_wrapup else 'Ready for: /clear → /orient → /brainstorming'}"
            ),
        })

        # Record that we're sending or have sent /wrap-up
        if needs_wrapup:
            new_state[str(wid)] = {
                "sitting_since": sitting_since,
                "wrapup_sent_epoch": now_epoch,  # Set NOW so we don't forget
                "needs_wrapup": False,  # Next cycle we'll check grace period
            }
        else:
            new_state[str(wid)] = {
                "sitting_since": sitting_since,
                "last_end": now_epoch,
                "wrapup_sent_epoch": prev.get("wrapup_sent_epoch", 0),
                "needs_wrapup": False,
            }

    _save_state(CODEX_RESOLVE_LAST_ATTEMPT_JSON + ".nend", new_state)
    return natural_end_needed, new_state



def track_codex_resolve(fleet: dict) -> tuple:
    """Track windows stuck in decision points with no user response.
    Returns (resolve_needed, new_state)."""
    now_epoch = time.time()
    prev_state = _load_state(CODEX_RESOLVE_LAST_ATTEMPT_JSON)
    new_state = {}
    resolve_needed = []

    for wid, info in fleet.items():
        if not isinstance(info, dict) or "state" not in info:
            continue
        if str(wid) in {"0", "2"}:
            continue

        state = info.get("state")
        wname = base_name(info.get("name", ""))
        last_question = info.get("last_question")

        if state not in {"blocked_waiting", "blocked_survey", "needs_approval", "subagent_menu"}:
            new_state[str(wid)] = prev_state.get(str(wid), {})
            continue

        if not last_question:
            new_state[str(wid)] = prev_state.get(str(wid), {})
            continue

        prev = prev_state.get(str(wid), {})
        last_resolve = prev.get("last_resolve", 0)
        elapsed = now_epoch - last_resolve
        if elapsed < CODEX_RESOLVE_COOLDOWN:
            new_state[str(wid)] = prev
            continue

        waiting_since = prev.get("blocked_since")
        if waiting_since is None:
            waiting_since = now_epoch
            new_state[str(wid)] = {
                "blocked_since": waiting_since,
                "last_resolve": 0,
                "question": last_question,
            }
            continue

        stuck_minutes = (now_epoch - waiting_since) / 60
        if stuck_minutes >= CODEX_RESOLVE_AFTER_MIN:
            resolve_needed.append({
                "window": wid,
                "name": wname,
                "state": state,
                "question": last_question,
                "stuck_minutes": int(stuck_minutes),
                "description": f"BLOCKED {int(stuck_minutes)}min — Codex resolve needed: {last_question[:80]}",
            })
            new_state[str(wid)] = {
                "blocked_since": waiting_since,
                "last_resolve": now_epoch,
                "question": last_question,
            }
        else:
            new_state[str(wid)] = {
                "blocked_since": waiting_since,
                "last_resolve": last_resolve,
                "question": last_question,
            }

    _save_state(CODEX_RESOLVE_LAST_ATTEMPT_JSON, new_state)
    return resolve_needed, new_state



def track_wrapup(fleet: dict) -> tuple:
    """Track windows that have FINISHED their main task but still show up as 'DONE' or 'completed' in the collector output.
    Detects: working/completed + spinner + task list shows N done + M open + Companion monitor event + Active goal = brainstorming or active project.
    These should get the /wrap-up → /clear → /orient → /brainstorming refresh sequence."""
    now_epoch = time.time()
    prev_state = _load_state(WRAPUP_STATE_FILE)
    new_state = {}
    wrapup_needed = []

    for wid, info in fleet.items():
        if not isinstance(info, dict) or "state" not in info:
            continue
        if str(wid) in {"0", "2"}:
            continue

        state = info.get("state")
        wname = base_name(info.get("name", ""))
        state_label = state.replace("_", " ")
        ctx = info.get("ctx_used")
        has_spinner = info.get("has_spinner", False)
        pending = info.get("pending_tasks", [])
        details = info.get("details", [])
        detail_text = " ".join(details).lower()

        # ── Signal 1: state is "working" with spinner ──
        # Working + spinner = genuine activity — skip, just log
        if state == "working" and has_spinner:
            # Still actively working, not a wrapup candidate
            new_state[str(wid)] = prev_state.get(str(wid), {})
            continue

        # ── Signal 2: task list shows all done/completed ──
        task_completed = False
        if pending:
            all_done_or_monitor = all(
                "done" in t.lower() or "completed" in t.lower() or
                "monitor" in t.lower() for t in pending
            )
            has_open = any(
                "open" in t.lower() for t in pending
            )
            if all_done_or_monitor and not has_open:
                task_completed = True

        # Also match pattern: "N tasks (all done" in task list text
        all_pending_text = " ".join(pending).lower()
        if "all done" in all_pending_text or "completed" in all_pending_text:
            task_completed = True

        # ── Signal 3: details contain "completed"/"done" + "monitor"/"active" ──
        done_signal = ("completed" in detail_text or "done" in detail_text)
        monitor_signal = ("monitor" in detail_text or "active" in detail_text or "actively" in detail_text)
        details_match = done_signal and monitor_signal

        # Must have at least one completion signal AND one monitor/active signal
        if not (task_completed or details_match):
            new_state[str(wid)] = prev_state.get(str(wid), {})
            continue

        # ── Check cooldown ──
        prev = prev_state.get(str(wid), {})
        last_wrapup = prev.get("last_wrapup", 0)
        elapsed = now_epoch - last_wrapup
        if elapsed < NATURAL_END_COOLDOWN:
            new_state[str(wid)] = prev
            continue

        # ── How long has been sitting in this completed state? ──
        sitting_since = prev.get("sitting_since", now_epoch)
        sitting_min = (now_epoch - sitting_since) / 60
        if sitting_min < NATURAL_END_THRESHOLD:
            # First time seeing this — record start
            new_state[str(wid)] = {"sitting_since": sitting_since}
            continue

        # ── Build description with optional high-ctx annotation ──
        ctx_note = ""
        if ctx is not None and ctx > HIGH_CTX_THRESHOLD:
            ctx_note = f" + HIGH_CTX {ctx}% "

        desc = (
            f"WRAPUP — sitting {int(sitting_min)}min with tasks done{ctx_note}. "
            f"Needs: /wrap-up → /clear → /orient → /brainstorming"
        )

        wrapup_needed.append({
            "window": wid,
            "name": wname,
            "state": state,
            "sitting_minutes": int(sitting_min),
            "ctx": ctx,
            "description": desc,
        })
        new_state[str(wid)] = {"sitting_since": sitting_since, "last_wrapup": now_epoch}

    _save_state(WRAPUP_STATE_FILE, new_state)
    return wrapup_needed, new_state


# ─── Main ─────────────────────────────────────────────────

def main():
    os.environ["TMUX"] = TMUX_SOCKET

    if not check_vllm():
        print("LOGGED")
        sys.exit(0)

    data, err = run_collector()
    if err or not data:
        print(f"LOGGED [collector error]")
        return

    fleet = data.get("fleet", {})
    slurm = data.get("slurm", [])
    esc_summaries = data.get("escalation_summaries", [])
    warnings = data.get("warnings", [])

    # Post-process fleet states: reclassify "idle" windows with completion signals as "just_finished"
    for wid, info in fleet.items():
        if isinstance(info, dict) and info.get("state") == "idle":
            pane_text = _capture_pane_text(str(wid))
            if pane_text and re.search(
                r'(all done|all complete|task.*done|complete.*summary|✓.*\d+ of \d+|Wrote .* files|All tests.*passed|Validation.*complete|Done!|Finished!|Task complete)',
                pane_text, re.I
            ):
                info["state"] = "just_finished"
                details = info.get("details", [])
                if isinstance(details, list):
                    details.append("task just completed — idle but finished, not stuck")
    
    # User activity
    recent_windows = _check_window_idle_activity()

    # State tracking
    idle_warnings, _ = track_idle(fleet)
    wrapup_needed, wrapup_state = track_wrapup(fleet)
    high_ctx_needed, _ = track_high_ctx(fleet)
    natural_end_needed, _ = track_natural_end(fleet)
    codex_resolve_needed, _ = track_codex_resolve(fleet)

    # Suppress idle nudge for windows where wrap-up was already sent (no point nudging a done session)
    if wrapup_state:
        suppressed = set()
        for wid_sent, wu in wrapup_state.items():
            if wu.get("wrapup_sent_at"):
                suppressed.add(str(wid_sent))
        cleaned_idle = []
        for w in idle_warnings:
            # Format: "W3 (TUSV-ext) [goal_stopped] idle 195min"
            win = w.split(":")[0].split()[0].lstrip("W").strip() if w else ""
            if win not in suppressed:
                cleaned_idle.append(w)
        idle_warnings = cleaned_idle

    # Cycle counter (called exactly once)
    cycle, is_report = increment_cycle()
    window_count = len(fleet)
    esc_count = len(esc_summaries)
    log_coordination(f"{cycle}c, {window_count}w, {esc_count}e, rpt={is_report}")

    # ─── Goal tracking ───
    # Check active supervisor goals and collect raw actions (filtering deferred until typing_set is built)
    goal_actions_raw = []
    try:
        from supervisor_goals import check_goals as check_supervisor_goals
        goal_actions_raw = check_supervisor_goals(fleet)
    except ImportError:
        pass

    # ─── Decision logic ───

    # Priority: ESCALATION > WRAPUP > IDLE_NUDGE > REPORT > SILENT

    # ─── Circuit breaker #2: escalation suppression ───
    # If same window already escalated recently, suppress to prevent death loop
    # ─── Circuit breaker #3: HARD user typing guard ───
    # If user is actively typing on a window, NEVER send any input to it.
    # This is the hard firewall — `recent_windows` alone is advisory only.
    INFRA = {"0", "2"}
    esc_summaries = [e for e in esc_summaries if str(e.get("window", "?")) not in INFRA]
    
    # Hard block: remove escalations for windows where user is typing
    esc_summaries = [e for e in esc_summaries if str(e.get("window")) not in recent_windows]
    
    if esc_summaries:
        now_epoch = time.time()
        esc_state = _load_state(ESC_STATE_FILE)
        new_esc_state = {}
        filtered_escalations = []
        
        for esc in esc_summaries:
            w = str(esc.get("window", "?"))
            prev_time = esc_state.get(w, {}).get("last_escalated", 0)
            elapsed = now_epoch - prev_time
            
            if elapsed < ESC_SUPPRESS_MINUTES * 60:
                # Recently escalated — suppress unless user is actively on it (they may be resolving)
                if w not in recent_windows:
                    continue  # Skip re-escalating
            
            filtered_escalations.append(esc)
            new_esc_state[w] = {"last_escalated": now_epoch, "last_state": esc.get("state", "?")}
        
        _save_state(ESC_STATE_FILE, new_esc_state)
        esc_summaries = filtered_escalations

    if esc_summaries:
        lines = [f"ESCALATION cycle={cycle}"]
        for esc in esc_summaries[:5]:
            w = esc.get("window", "?")
            s = esc.get("state", "?")
            summary = esc.get("summary_lines", [])[:2]
            lines.append(f"  W{w}: [{s}] {' | '.join(summary)}")
        if recent_windows:
            lines.append(f"  ⚠ USER ACTIVE ON W{sorted(recent_windows)}")
        log_coordination(f"escalations on {[e.get('window','?') for e in esc_summaries]}")
        print("\n".join(lines))
        return

    # Hard block: build typing window set for ALL action filters
    typing_set = {int(w) for w in recent_windows}

    # Filter goal actions by typing guard
    goal_actions = [a for a in goal_actions_raw if a["window"] not in typing_set]

    if goal_actions:
        # Goal actions take priority — auto-unblock goal-bound windows
        lines = [f"GOAL_ACTIONS cycle={cycle}"]
        for a in goal_actions:
            lines.append(f"  W{a['window']}: {a['action']} — {a['reason']}")
        print("\n".join(lines))
        return

    if natural_end_needed:
        # Windows finished their task but still showing "working" — need refresh
        ne_filtered = [c for c in natural_end_needed if c.get("window") not in typing_set]
        if ne_filtered:
            lines = [f"NATURAL_END cycle={cycle}"]
            for c in ne_filtered[:5]:
                lines.append(f"  W{c['window']} ({c['name']}): sitting {c['sitting_minutes']}min idle, action=/wrap-up->/clear->/orient->/brainstorming")
            if recent_windows:
                lines.append(f"  ⚠ USER ACTIVE ON W{sorted(recent_windows)}")
            print("\n".join(lines))
            return

    if codex_resolve_needed:
        # Windows stuck >CODEX_RESOLVE_AFTER_MIN with no user response
        # Hard block: don't auto-resolve if user is typing on that window
        ctx_filtered = []
        for c in codex_resolve_needed:
            if c.get("window") not in typing_set:
                ctx_filtered.append(c)
        if ctx_filtered:
            lines = [f"CODEX_RESOLVE cycle={cycle}"]
            for c in ctx_filtered[:5]:
                lines.append(f"  W{c['window']} ({c['name']}): {c['stuck_minutes']}min, question={c['question']}")
            if recent_windows:
                lines.append(f"  ⚠ USER ACTIVE ON W{sorted(recent_windows)}")
            print("\n".join(lines))
            return

    if high_ctx_needed:
        # Context refresh: wrap up, /clear, /orient, restart brainstorming
        # HARD block: don't refresh if user is typing on that window
        ctx_filtered = []
        for c in high_ctx_needed:
            if c.get("window") not in typing_set:
                ctx_filtered.append(c)
        if ctx_filtered:
            lines = [f"HIGH_CTX cycle={cycle}"]
            for c in ctx_filtered[:5]:
                lines.append(f"  W{c['window']} ({c['name']}): {c['ctx']}%, action=wrap→clear→orient→brainstorm")
            if recent_windows:
                lines.append(f"  ⚠ USER ACTIVE ON W{sorted(recent_windows)}")
            print("\n".join(lines))
            return

    if wrapup_needed:
        # HARD block: don't attempt wrap-up if user is typing on that window
        # Parse window ID from format "W3 (TUSV-ext) [state] stale 45min, ctx=32%"
        wrapup_filtered = []
        for w in wrapup_needed:
            try:
                wid = int(w.split()[0].lstrip("W"))
            except:
                wid = -1
            if wid not in typing_set:
                wrapup_filtered.append(w)
        if wrapup_filtered:
            lines = [f"WRAPUP cycle={cycle}"]
            for w in wrapup_filtered[:5]:
                lines.append(f"  {w}")
            if recent_windows:
                lines.append(f"  ⚠ USER ACTIVE ON W{sorted(recent_windows)}")
            print("\n".join(lines))
            return

    if idle_warnings:
        # HARD block: don't nudge if user is typing on that window
        idle_filtered = []
        for w in idle_warnings:
            try:
                wid = int(w.split(":")[0].split()[0].lstrip("W"))
            except:
                wid = -1
            if wid not in typing_set:
                idle_filtered.append(w)
        if idle_filtered:
            lines = [f"IDLE_NUDGE cycle={cycle}"]
            for w in idle_filtered[:5]:
                try:
                    wid = int(w.split(":")[0].split()[0].lstrip("W"))
                except:
                    wid = -1
                snippet = _pane_snippet(str(wid)) if wid >= 0 else "[unknown]"
                lines.append(f"  {w}")
                lines.append(f"    pane: {snippet}")
            if recent_windows:
                lines.append(f"  ⚠ USER ACTIVE ON W{sorted(recent_windows)}")
            print("\n".join(lines))
            return

    if is_report:
        # Periodic fleet report — enriched with available context
        running_slurm = len([j for j in slurm if j.get("state") == "RUNNING"])
        pending_slurm = len([j for j in slurm if j.get("state") == "PENDING"])
        lines = []
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        lines.append(f"Fleet Report — {now}")
        active_count = 0
        idle_count = 0
        NON_WORKING = WAITING_STATES | {"monitoring", "subagent_menu"}

        # Load idle and wrapup state for duration info
        idle_state = _load_state(IDLE_STATE_FILE)
        wrapup_state = _load_state(WRAPUP_STATE_FILE)

        for wid in sorted(fleet.keys(), key=lambda w: int(w) if w.isdigit() else 0):
            if str(wid) in {"0", "2"}:
                continue
            info = fleet[wid]
            if not isinstance(info, dict) or "state" not in info:
                continue
            state = info["state"]
            wname = base_name(info.get("name", ""))
            ctx = info.get("ctx_used")
            ctx_str = f"{ctx}%" if ctx is not None else "?"
            details = info.get("details", [])
            detail_str = "; ".join(details[:2]) if details else ""
            state_label = state.replace("_", " ")
            tokens = info.get("tokens_total")
            token_str = f" {tokens//1000}k tok" if tokens else ""

            # Idle duration tracking
            idle_info = idle_state.get(str(wid), {})
            idle_since = idle_info.get("since")
            idle_mins = None
            if idle_since and state in NON_WORKING:
                idle_mins = max(1, int((time.time() - idle_since) / 60))

            # Wrap-up status
            wu_info = wrapup_state.get(str(wid), {})
            wrapup_sent = wu_info.get("wrapup_sent_at")

            if state in NON_WORKING:
                idle_count += 1
                idle_time = f" ({idle_mins}m)" if idle_mins else ""
                line = f"  W{wid} {wname}: [{state_label}]{idle_time}, {ctx_str}%"
                lines.append(line)
                if detail_str:
                    lines.append(f"    — {detail_str}")
                status_items = []
                if token_str:
                    status_items.append(token_str)
                if wrapup_sent:
                    status_items.append("wrap-up sent")
                if status_items:
                    lines.append(f"    | {' | '.join(status_items)}")
            else:
                active_count += 1
                header = f"  W{wid} {wname} {ctx_str}%{token_str}: [{state_label}]"
                if detail_str:
                    header += f" — {detail_str}"
                lines.append(header)

        if lines == ["Fleet Report — " + now]:
            lines.append("  No user windows found")
        lines.append(f"  Active: {active_count}, Idle: {idle_count}")

        if recent_windows:
            lines.append(f"  ⚠ USER ACTIVE ON W{sorted(recent_windows)}")
        lines.append(f"  SLURM: {running_slurm} running, {pending_slurm} pending")
        print("\n".join(lines))
        return

    # Default: silent
    print("LOGGED")


if __name__ == "__main__":
    main()
