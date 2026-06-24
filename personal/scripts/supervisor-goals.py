#!/usr/bin/env python3
"""
Supervisor Goals — persistent goal tracking + auto-resolution for fleet supervisor.

Usage:
  python3 supervisor-goals.py --goals                           # list all goals
  python3 supervisor-goals.py --set-goal --window 4 --description "Finish NICn test" --auto-level medium
  python3 supervisor-goals.py --complete-goal --id goal-1
  python3 supervisor-goals.py --goal-info --id goal-1

Internal API (for fleet-supervisor-auto-resolver.py):
  from supervisor_goals import check_goals, list_goals, complete_goal
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone

GOALS_FILE = os.path.expanduser("~/.hermes/scripts/supervisor-goals.json")
RESOLUTION_FILE = os.path.expanduser("~/.hermes/scripts/_goal_resolution.json")
SYNC_FILE = os.path.expanduser("~/.hermes/scripts/_goal_sync.md")

# Window → project name mapping (auto-updated from collector)
WINDOW_PROJECTS = {
    1: "ERT-ESR1",
    3: "TUSV-ext",
    4: "EvanthiaRoussosTorresAnalysis",
    5: "pILC-Mix",
    6: "Path2Space",
    7: "PhyloSchlag-Macro",
    8: "pILC-CASCAM",
    9: "Hermes",
}


def _load_goals() -> dict:
    try:
        with open(GOALS_FILE) as f:
            return json.load(f)
    except:
        return {"goals": []}


def _save_goals(data: dict):
    with open(GOALS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _load_resolutions() -> dict:
    try:
        with open(RESOLUTION_FILE) as f:
            return json.load(f)
    except:
        return {"goal_resolutions": []}


def _save_resolutions(data: dict):
    with open(RESOLUTION_FILE, "w") as f:
        json.dump(data, f, indent=2)


def set_goal(window: int, description: str, auto_level: str = "medium") -> dict:
    """Create a new supervisor goal."""
    data = _load_goals()
    goal_id = f"goal-{uuid.uuid4().hex[:8]}"
    goal = {
        "id": goal_id,
        "window": window,
        "description": description,
        "auto_level": auto_level,  # light | medium | heavy
        "active": True,
        "created": datetime.now(timezone.utc).isoformat(),
        "last_progress": description,
        "progress_count": 0,
        "check_interval": 3,  # check every 3 cycles (15min)
        "completed": False,
        "stale_timer": 0,
        "last_state": "",
    }
    data["goals"].append(goal)
    _save_goals(data)
    return goal


def complete_goal(goal_id: str, resolution: str = "") -> dict | None:
    """Mark a goal as completed."""
    data = _load_goals()
    for goal in data["goals"]:
        if goal["id"] == goal_id:
            goal["active"] = False
            goal["completed"] = True
            goal["completed_at"] = datetime.now(timezone.utc).isoformat()
            goal["last_progress"] = resolution or "User marked complete"
            return goal

    print(f"Goal {goal_id} not found", file=sys.stderr)
    return None


def list_goals(active_only: bool = False) -> list:
    data = _load_goals()
    goals = data.get("goals", [])
    if active_only:
        goals = [g for g in goals if g.get("active")]
    return goals




def get_wrapup_state() -> dict:
    """Get the current wrapup state from the vault.
    
    Called from fleet-supervisor-auto-resolver.py track_natural_end().
    Returns dict keyed by window id (string), with 'wrapup_sent_at' timestamps.
    Returns empty dict if file doesn't exist or is invalid.
    """
    import os
    vault = os.path.expanduser("~/Documents/Obsidian Vault")
    wrapup_file = os.path.join(vault, "lab", "agents", "supervisor", "_wrapup_state.json")
    if not os.path.exists(wrapup_file):
        return {}
    try:
        with open(wrapup_file, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def check_goals(fleet: dict) -> list:
    """Check active goals against current fleet state. Returns list of actions to take.

    Called from fleet-supervisor-auto-resolver.py main() loop.
    Returns list of action dicts: {"window": 4, "action": "proceed", "reason": "idle"}
    """
    data = _load_goals()
    actions = []
    sync_entries = []

    for goal in data["goals"]:
        if not goal.get("active"):
            continue

        wid = goal["window"]
        window_key = str(wid)
        if window_key not in fleet:
            continue

        fleet_info = fleet[window_key]
        if not isinstance(fleet_info, dict):
            continue

        state = fleet_info.get("state", "unknown")
        project = WINDOW_PROJECTS.get(wid, f"W{wid}")

        # Track stale time (no progress changes)
        prev_state = goal.get("last_state", state)
        if state == prev_state:
            goal["stale_timer"] = goal.get("stale_timer", 0) + 5  # 5min per cycle
        else:
            goal["stale_timer"] = 0
            goal["last_progress"] = f"State changed: {prev_state} → {state}"
            goal["progress_count"] += 1

        goal["last_state"] = state

        # Auto-complete if goal_stopped or final_completion
        if state in ("goal_stopped", "empty"):
            resolution = f"Window completed (state={state}) after {goal['progress_count']} progress updates"
            complete_goal(goal["id"], resolution)
            sync_entries.append(f"[COMPLETED] {goal['id']} | W{wid}:{project} | {resolution} | {datetime.now(timezone.utc).isoformat()}")
            goal["completed"] = True  # update local copy
            continue

        # Auto-complete if stale >2hr with no action
        if goal["stale_timer"] > 120 and state != "working":
            resolution = f"Stale {goal['stale_timer']}min with no progress (state={state})"
            complete_goal(goal["id"], resolution)
            sync_entries.append(f"[STALE] {goal['id']} | W{wid}:{project} | {resolution} | {datetime.now(timezone.utc).isoformat()}")
            goal["completed"] = True
            continue

        # Auto-action based on state + auto_level
        auto = goal.get("auto_level", "light")
        
        # CRITICAL: cooldown per window — after auto-proceeding, don't hammer again
        auto_cooldown_path = os.path.join(os.path.dirname(GOALS_FILE), "_goal_auto_cooldown.json")
        try:
            with open(auto_cooldown_path) as f:
                cooldowns = json.load(f)
        except:
            cooldowns = {}
        
        now_min = datetime.now(timezone.utc).timestamp()
        wid_key = str(wid)
        last_auto = cooldowns.get(wid_key, 0)
        COOLDOWN_MIN = 15  # 15 min between auto-proceeds per window
        if (now_min - last_auto) < COOLDOWN_MIN * 60:
            sync_entries.append(f"[COOLDOWN] {goal['id']} | W{wid}:{project} | last auto {int((now_min - last_auto)/60)}min ago, skip")
            continue
        
        if state == "idle" and auto in ("medium", "heavy"):
            # Record cooldown
            cooldowns[wid_key] = now_min
            try:
                with open(auto_cooldown_path, 'w') as f:
                    json.dump(cooldowns, f)
            except:
                pass
            actions.append({
                "window": wid,
                "action": "proceed",
                "reason": f"Goal {goal['id']} active, window idle — sending proceed to resume",
            })

        elif state == "blocked_waiting" and auto in ("medium", "heavy"):
            # medium level: only proceed for simple gates (yes/no, proceed)
            # heavy level: allow strategic decisions too
            # CRITICAL: medium should NOT resolve complex multi-choice or strategic prompts
            # just send "proceed" and hope it's safe, OR escalate to user
            if auto == "medium":
                # Be conservative — only auto-proceed, no decisions
                action_type = "proceed"
            else:
                # heavy — let the cron agent decide
                action_type = "resolve"
            
            cooldowns[wid_key] = now_min
            try:
                with open(auto_cooldown_path, 'w') as f:
                    json.dump(cooldowns, f)
            except:
                pass
            actions.append({
                "window": wid,
                "action": action_type,
                "reason": f"Goal {goal['id']} active, blocked waiting — sending {action_type}",
            })

        elif state in ("blocked_survey", "monitoring"):
            # Monitoring/blocked_survey: no action needed, just track
            pass

    # Save state
    _save_goals(data)

    # Write sync file
    if sync_entries:
        with open(SYNC_FILE, "a") as f:
            for entry in sync_entries:
                f.write(entry + "\n")

    # Save resolutions
    resolutions = _load_resolutions()
    # (resolutions populated on explicit complete_goal)

    return actions


def main():
    parser = argparse.ArgumentParser(description="Supervisor Goals management")
    sub = parser.add_subparsers(dest="command")

    # List goals
    list_parser = sub.add_parser("goals", help="List all goals")
    list_parser.add_argument("--active-only", action="store_true")

    # Set goal
    set_parser = sub.add_parser("set-goal", help="Create a new supervisor goal")
    set_parser.add_argument("--window", type=int, required=True)
    set_parser.add_argument("--description", required=True)
    set_parser.add_argument("--auto-level", default="medium", choices=["light", "medium", "heavy"])

    # Complete goal
    complete_parser = sub.add_parser("complete-goal", help="Mark goal as complete")
    complete_parser.add_argument("--id", required=True)
    complete_parser.add_argument("--resolution", default="")

    # CLI aliases (--goals, --set-goal, etc.)
    parser.add_argument("--goals", action="store_true")
    parser.add_argument("--set-goal", action="store_true")
    parser.add_argument("--complete-goal", action="store_true")
    parser.add_argument("--goal-info", action="store_true")
    parser.add_argument("--window", type=int)
    parser.add_argument("--description")
    parser.add_argument("--auto-level", default="medium")
    parser.add_argument("--id")
    parser.add_argument("--resolution", default="")

    args = parser.parse_args()

    # Legacy flag aliases
    if args.goals:
        for g in list_goals(True):
            status = "✅ COMPLETE" if g.get("completed") else ("⏳ ACTIVE" if g.get("active") else "❌ INACTIVE")
            print(f"  [{status}] {g['id']} | W{g['window']} | {g['description']}")
            print(f"           level={g['auto_level']} | progress: {g.get('last_progress', '')}")
    elif args.set_goal:
        goal = set_goal(args.window, args.description, args.auto_level)
        print(f"Created: {goal['id']} — W{goal['window']}: {goal['description']}")
    elif args.complete_goal:
        goal = complete_goal(args.id, args.resolution)
        if goal:
            print(f"Completed: {goal['id']}")
        else:
            sys.exit(1)
    elif args.command == "goals":
        for g in list_goals(args.active_only):
            status = "✅" if g.get("completed") else ("⏳" if g.get("active") else "❌")
            print(f"  {status} {g['id']} | W{g['window']} | {g['description']}")
    elif args.command == "set-goal":
        goal = set_goal(args.window, args.description, args.auto_level)
        print(f"Created: {goal['id']} — W{goal['window']}: {goal['description']}")
    elif args.command == "complete-goal":
        goal = complete_goal(args.id, args.resolution)
        if goal:
            print(f"Completed: {goal['id']}")
        else:
            sys.exit(1)
    else:
        print("Usage: supervisor-goals.py --set-goal --window N --description '...'")
        print("       supervisor-goals.py --goals")
        print("       supervisor-goals.py --complete-goal --id <goal-id>")


if __name__ == "__main__":
    main()
