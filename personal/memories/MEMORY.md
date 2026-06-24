CITEgeist figure defects: NEVER whitelist figures to hide visual issues. Whitelists are false positives — always trace to root cause in assembly engine and fix it there. Delegate visual review to Codex, not automated tests. Code before exemptions.
§
User can't make decisions without context. Never escalate "approve Option A" without restating what Option A is. Always include the full decision context and pane capture before bothering them.
§
Fleet supervisor: `c16568fa305c` every 5min, healthcheck: `d47cbbbf9453` every 30min (shell). vLLM W0, gateway W2 — infra, exclude. `ALTERNATE_SILENT="LOGGED"`. Wraps: stale pane (9 cycles=45min). Idle timer RESETS on transition INTO waiting (prev_was_waiting=False). Delegate: Codex.
§
CITEgeist/Path2Space: handoff docs in `{project}/docs/handoffs/`. Restart: exit Qwen → `claude-standard` → `claude` → read handoff.
§
Claude Code: `/permissions` or `shift+tab` for auto mode. NEVER `--dangerously-skip-permissions`. **Delegation target: Codex for ALL complex reasoning and agent review** — agents ARE Claude Code, so use Codex as independent second brain. Claude Code: `claude -p` for print-mode queries.
§
Fleet supervisor cron: uses `ALTERNATE_SILENT = "LOGGED"` suppression. 15-min delivery race fix in `cron/scheduler.py` and `hermes-supervisor` skill. Cron healthcheck is shell-only (`cron-monitor.sh` + `cron-healthcheck.sh`, no Python).
§
Hermes fork: alexanderchang1/hermes-agent on GitHub, local checkout at /ihome/alee/alc376/.hermes/hermes-agent/. User pushes fixes to fork but does not open PRs upstream to NousResearch.
§
python3 hangs on this host (resource contention/GIL) — subprocess runs that depend on Python (including cron scripts) hit 120s timeouts. Use shell-only scripts for cron jobs when possible. Converting gateway-self-heal from Python to shell fixed the timeout.
§
FLEET SUPERVISOR DESIGN MANDATE: Agent MUST stay `no_agent=False` — intelligent agent operation is inviolable. All robustness comes from better infrastructure, NEVER from disabling the agent. Agent handles reasoning/escalation/decisions. Shell handles all I/O (subprocess spawning, network calls, file ops).