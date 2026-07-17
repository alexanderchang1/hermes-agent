# Telegram polling self-conflict — diagnosis & handoff (2026-07-16)

Symptom: the gateway's Telegram adapter loops forever on
`Telegram polling conflict (1/5) — terminated by other getUpdates request`,
plus (secondary) `Reconnect email error: email connect timed out after 30s`.

## Proven facts (do not re-litigate)

- **No external consumer.** With the gateway fully stopped, a manual
  `getUpdates` with the bot token returns HTTP 200 (session free). So the 409s
  are **self-inflicted within one gateway process** — not a second machine.
- **Network to Telegram is healthy.** `getMe` 0.48s via system DNS *and* via the
  fallback IP `149.154.166.110` with SNI override. The `httpx.ReadError` /
  "Primary api.telegram.org path unreachable" churn in the log is a *symptom* of
  the drain/stop cycling, not a real connectivity fault. (`telegram_network.py`
  transport is fine; no proxy is configured — the `:8010` listener is the vLLM
  proxy.)
- **Not the gateway watcher.** No `Reconnecting telegram`, `Disconnected`, or
  `Fatal telegram` events during the loop — `_platform_reconnect_watcher`
  (`gateway/run.py:7399`) is not recreating the adapter. The loop is entirely
  inside one adapter instance.

## Fixed & committed on branch `personal`

| Commit | Fix | Status |
|--------|-----|--------|
| `78c030b9c` | **Email** connect: defer SELECT/SEARCH + catch-up to a background task; connect() now does only IMAP+SMTP login (~0.4s) in an executor. Root cause: `select`(10s, contention) + `search ALL`(10s, Gmail-side) inline in the 30s connect budget. Added missing-host guard. | ✅ verified live |
| `d8605be8c` | **Telegram** conflict handler: fix `_notify_fatal_error(group_code=…)` TypeError; move counter increment into the handler; `get_running_loop()` reschedule. | ✅ 14 tests |
| `86f754171` | **Telegram** recovery-path race: single `_recovery_in_progress` guard + `_recovery_active` predicate so conflict/network/heartbeat/verify recoveries can't run concurrently. Eliminated `This Updater is already running!`. | ✅ verified live |
| `bf6cd2c89` | **Telegram** verify-before-reset: don't reset the conflict counter on `start_polling()`'s bare return; verify no fresh 409 in a 5s window first. Grow back-off 30/40/50/60/70s. | ✅ tests; insufficient (see below) |

## Remaining root cause (unsolved)

Instrumented run (PDBG logs, since reverted) showed, each ~30s cycle:

1. `errcb: conflict=True recovery_in_progress=False running=True` — a 409 hits a
   **healthy, running** updater. Something newer polled `getUpdates` and
   Telegram terminated our active poller (409 goes to the *older* getUpdates).
2. recovery: stop (`running=False`) → `start_polling` (`running=True`).
3. `verify: cb_received=False running=True` — **passes** (the re-conflict lands
   *after* the 5s verify window), so the counter resets to 0.
4. ~30s later (≈ one long-poll cycle), another 409 on the running updater →
   repeat. Counter never climbs, so verify-before-reset never escalates.

So: **a running single poller is repeatedly terminated by a competing
`getUpdates` originating within the same process, on a ~30s cadence.** Prime
suspects (need PTB-22.6-source-level confirmation):

- The `_drain_polling_connections()` shutdown/reinitialize abandons an in-flight
  `getUpdates` whose **server-side session lingers ~30–50s**; the next
  `start_polling` collides with it.
- PTB's internal polling task / `__polling_task_stop_event`
  (`_disarm_ptb_retry_loop`, adapter.py:2186 — the attr DOES exist on 22.6, so
  the disarm is not a no-op) interacting with our stop→drain→start_polling.
- The 5s verify window is shorter than the re-conflict latency (~30s), so
  verify-before-reset can't see it. Widening the window ≥ one long-poll cycle,
  or keying "recovered" off an actually-received update rather than time, is the
  next thing to try.

## Candidate fix directions (for a focused session)

1. **Widen/rework the recovery-success signal.** Don't accept recovery on a
   time-boxed silence; accept it only when the poller receives a real update or
   a clean `getUpdates` 200 (not 409) — or verify over ≥ one full long-poll
   timeout, not 5s.
2. **Stop the drain/restart churn.** On a 409 for a *running* updater, prefer
   *not* tearing down — a lone healthy poller that sees a transient 409 during
   restart churn may self-heal if left alone; the aggressive stop/drain/restart
   may be manufacturing the competing sessions.
3. **Cancel in-flight recovery on disconnect.** `disconnect()` (adapter.py:3266)
   does not cancel `_polling_error_task`/`_background_tasks` — a loose end that
   leaves zombie recovery tasks after teardown (mitigated by `_app=None`, but
   worth closing).
4. Consider reproducing against PTB 22.6 in isolation to characterize the
   server-side-session-linger vs. start_polling timing precisely.

## How to validate any fix

- Restart cleanly: `Ctrl-C` in tmux `claude-gpu:1`, wait for exit, then
  `hermes gateway run --replace` in that window. (Gateway is a foreground child
  of the window-1 shell; no watchdog/cron respawns it.)
- Watch the live pane for the conflict counter **climbing** and then
  `polling resumed` with no re-conflict — or `could not recover` (fatal) if
  genuinely stuck.
- External-consumer probe (gateway down): manual `getUpdates` with the token →
  200 = free, 409 = another instance somewhere.
- Count sessions: `ss -tnp | grep 149.154. | grep ESTAB` (note: 2 connections is
  normal — one getUpdates long-poll + one general request pool).
