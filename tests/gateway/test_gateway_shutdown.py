import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gateway.run as gateway_run
from gateway.config import HomeChannel, Platform
from gateway.platforms.base import MessageEvent
from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE
from gateway.session import build_session_key
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


@pytest.mark.asyncio
async def test_start_gateway_failure_stops_cron_provider(monkeypatch, tmp_path):
    """A gateway failure exit must not leave its cron owner running."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    provider_started = threading.Event()
    provider_stopped = threading.Event()
    provider_exited = threading.Event()
    housekeeping_exited = threading.Event()
    observed_stop_events = []

    class _FailingRunner:
        def __init__(self, config):
            self.config = config
            self.adapters = {}
            self._running = True
            self._draining = False
            self._external_drain_active = False
            self.should_exit_cleanly = False
            self.should_exit_with_failure = True
            self.exit_reason = "simulated gateway failure"
            self.exit_code = None

        async def start(self):
            return True

        async def wait_for_shutdown(self):
            for _ in range(200):
                if provider_started.is_set():
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("cron provider did not start")

    class _CronProvider:
        def start(self, stop_event, **_kwargs):
            observed_stop_events.append(stop_event)
            provider_started.set()
            stop_event.wait()
            provider_exited.set()

        def stop(self):
            provider_stopped.set()

    def _housekeeping(stop_event, **_kwargs):
        observed_stop_events.append(stop_event)
        stop_event.wait()
        housekeeping_exited.set()

    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda hermes_home, mode: tmp_path)
    monkeypatch.setattr("hermes_logging._add_rotating_handler", lambda *args, **kwargs: None)
    monkeypatch.setattr("gateway.run.GatewayRunner", _FailingRunner)
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: _CronProvider())
    monkeypatch.setattr("gateway.run._start_gateway_housekeeping", _housekeeping)
    monkeypatch.setattr("hermes_cli.nous_auth_keepalive.start_nous_auth_keepalive", lambda: None)
    monkeypatch.setattr("hermes_cli.nous_auth_keepalive.stop_nous_auth_keepalive", lambda: None)
    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", lambda: None)
    monkeypatch.setattr("tools.mcp_tool.shutdown_mcp_servers", lambda: None)

    ok = await gateway_run.start_gateway(
        config=gateway_run.GatewayConfig(), replace=False, verbosity=None
    )

    # Let pytest exit cleanly on the pre-fix implementation, where the gateway
    # never signals these daemon threads itself. Preserve the observed provider
    # stop state so the assertion still proves the lifecycle bug.
    provider_stop_was_called = provider_stopped.is_set()
    assert observed_stop_events
    observed_stop_events[0].set()
    assert provider_exited.wait(2)
    assert housekeeping_exited.wait(2)

    assert ok is False
    assert provider_stop_was_called


@pytest.mark.asyncio
async def test_start_gateway_housekeeping_start_failure_stops_cron_provider(
    monkeypatch, tmp_path
):
    """A partial background-service startup must release the cron owner."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    provider_started = threading.Event()
    provider_stopped = threading.Event()
    provider_exited = threading.Event()
    observed_stop_events = []
    real_thread_class = threading.Thread

    class _RunningRunner:
        def __init__(self, config):
            self.config = config
            self.adapters = {}
            self._running = True
            self._draining = False
            self._external_drain_active = False
            self.should_exit_cleanly = False
            self.should_exit_with_failure = False
            self.exit_reason = None
            self.exit_code = None

        async def start(self):
            return True

        async def wait_for_shutdown(self):
            raise AssertionError("shutdown wait must not run after startup failure")

    class _CronProvider:
        def start(self, stop_event, **_kwargs):
            observed_stop_events.append(stop_event)
            provider_started.set()
            stop_event.wait()
            provider_exited.set()

        def stop(self):
            provider_stopped.set()

    class _UnstartableThread:
        def start(self):
            raise RuntimeError("simulated housekeeping thread start failure")

        def is_alive(self):
            return False

    def _thread_factory(*args, **kwargs):
        if kwargs.get("name") == "gateway-housekeeping":
            return _UnstartableThread()
        return real_thread_class(*args, **kwargs)

    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda hermes_home, mode: tmp_path)
    monkeypatch.setattr("hermes_logging._add_rotating_handler", lambda *args, **kwargs: None)
    monkeypatch.setattr("gateway.run.GatewayRunner", _RunningRunner)
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: _CronProvider())
    monkeypatch.setattr("gateway.run.threading.Thread", _thread_factory)
    monkeypatch.setattr("hermes_cli.nous_auth_keepalive.start_nous_auth_keepalive", lambda: None)
    monkeypatch.setattr("hermes_cli.nous_auth_keepalive.stop_nous_auth_keepalive", lambda: None)
    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", lambda: None)
    monkeypatch.setattr("tools.mcp_tool.shutdown_mcp_servers", lambda: None)

    with pytest.raises(RuntimeError, match="housekeeping thread start failure"):
        await gateway_run.start_gateway(
            config=gateway_run.GatewayConfig(), replace=False, verbosity=None
        )

    provider_stop_was_called = provider_stopped.is_set()
    assert provider_started.wait(2)
    assert observed_stop_events
    observed_stop_events[0].set()
    assert provider_exited.wait(2)
    assert provider_stop_was_called


@pytest.mark.asyncio
async def test_cancel_background_tasks_cancels_inflight_message_processing():
    _runner, adapter = make_restart_runner()
    release = asyncio.Event()

    async def block_forever(_event):
        await release.wait()
        return None

    adapter.set_message_handler(block_forever)
    event = MessageEvent(text="work", source=make_restart_source(), message_id="1")

    await adapter.handle_message(event)
    await asyncio.sleep(0)

    session_key = build_session_key(event.source)
    assert session_key in adapter._active_sessions
    assert adapter._background_tasks

    await adapter.cancel_background_tasks()

    assert adapter._background_tasks == set()
    assert adapter._active_sessions == {}
    assert adapter._pending_messages == {}


def test_cleanup_agent_resources_reaps_stale_aux_clients():
    runner, _adapter = make_restart_runner()
    agent = MagicMock()

    with patch("agent.auxiliary_client.cleanup_stale_async_clients") as cleanup_mock:
        runner._cleanup_agent_resources(agent)

    agent.shutdown_memory_provider.assert_called_once()
    agent.close.assert_called_once()
    cleanup_mock.assert_called_once()


@pytest.mark.asyncio
async def test_gateway_stop_interrupts_running_agents_and_cancels_adapter_tasks():
    runner, adapter = make_restart_runner()
    runner._pending_messages = {"session": "pending text"}
    runner._pending_approvals = {"session": {"command": "rm -rf /tmp/x"}}
    runner._restart_drain_timeout = 0.0

    release = asyncio.Event()

    async def block_forever(_event):
        await release.wait()
        return None

    adapter.set_message_handler(block_forever)
    event = MessageEvent(text="work", source=make_restart_source(), message_id="1")
    await adapter.handle_message(event)
    await asyncio.sleep(0)

    disconnect_mock = AsyncMock()
    adapter.disconnect = disconnect_mock

    session_key = build_session_key(event.source)
    running_agent = MagicMock()
    runner._running_agents = {session_key: running_agent}

    with (
        patch("gateway.status.remove_pid_file"),
        patch("gateway.status.write_runtime_status"),
        patch("agent.auxiliary_client.shutdown_cached_clients") as shutdown_cached_clients,
    ):
        await runner.stop()

    running_agent.interrupt.assert_called_once_with("Gateway shutting down")
    disconnect_mock.assert_awaited_once()
    shutdown_cached_clients.assert_called_once()
    assert runner.adapters == {}
    assert runner._running_agents == {}
    assert runner._pending_messages == {}
    assert runner._pending_approvals == {}
    assert runner._shutdown_event.is_set() is True


@pytest.mark.asyncio
async def test_gateway_stop_drains_running_agents_before_disconnect():
    runner, adapter = make_restart_runner()
    # Opt into a grace window (the default is 0 = interrupt immediately).
    # This exercises the path where an agent finishes within the drain
    # window and must NOT be interrupted.
    runner._restart_drain_timeout = 5.0
    disconnect_mock = AsyncMock()
    adapter.disconnect = disconnect_mock

    running_agent = MagicMock()
    runner._running_agents = {"session": running_agent}

    async def finish_agent():
        await asyncio.sleep(0.05)
        runner._running_agents.clear()

    asyncio.create_task(finish_agent())

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    running_agent.interrupt.assert_not_called()
    disconnect_mock.assert_awaited_once()
    assert runner._shutdown_event.is_set() is True


@pytest.mark.asyncio
async def test_gateway_stop_interrupts_after_drain_timeout():
    runner, adapter = make_restart_runner()
    runner._restart_drain_timeout = 0.05

    disconnect_mock = AsyncMock()
    adapter.disconnect = disconnect_mock

    running_agent = MagicMock()
    runner._running_agents = {"session": running_agent}

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    running_agent.interrupt.assert_called_once_with("Gateway shutting down")
    disconnect_mock.assert_awaited_once()
    assert runner._shutdown_event.is_set() is True


@pytest.mark.asyncio
async def test_gateway_stop_systemd_service_restart_uses_tempfail(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    monkeypatch.setenv("INVOCATION_ID", "systemd-test")
    runner._launch_systemd_restart_shortcut = MagicMock()

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop(restart=True, service_restart=True)

    runner._launch_systemd_restart_shortcut.assert_called_once_with()
    # Exit 75 (EX_TEMPFAIL) so RestartForceExitStatus=75 in the unit
    # file revives the gateway via Restart=on-failure, even when the
    # planned-restart helper fails (Polkit denial, missing user bus,
    # headless box, or operator-managed unit using on-failure instead
    # of always).  StartLimitBurst still bounds accidental loops.
    assert runner._exit_code == GATEWAY_SERVICE_RESTART_EXIT_CODE
    assert (tmp_path / ".restart_pending.json").exists()


@pytest.mark.asyncio
async def test_gateway_stop_launchd_service_restart_keeps_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()

    with patch("gateway.run.sys.platform", "darwin"), patch(
        "gateway.status.remove_pid_file"
    ), patch("gateway.status.write_runtime_status"):
        await runner.stop(restart=True, service_restart=True)

    assert runner._exit_code == GATEWAY_SERVICE_RESTART_EXIT_CODE


@pytest.mark.asyncio
async def test_restart_shutdown_warning_uses_restart_command_reply_anchor_for_active_session():
    runner, adapter = make_restart_runner()
    source = make_restart_source(thread_id="42")
    session_key = build_session_key(source)
    runner._running_agents = {session_key: MagicMock()}
    runner._cache_session_source(session_key, source)
    restart_source = make_restart_source(thread_id="42")
    restart_source.message_id = "restart-command"
    runner._restart_requested = True
    runner._restart_command_source = restart_source
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id=source.chat_id,
        name="Telegram",
        thread_id=source.thread_id,
    )

    await runner._notify_active_sessions_of_shutdown()

    assert len(adapter.sent_calls) == 1
    chat_id, message, metadata = adapter.sent_calls[0]
    assert chat_id == source.chat_id
    assert "Gateway restarting" in message
    assert metadata["thread_id"] == source.thread_id
    assert metadata["telegram_dm_topic_reply_fallback"] is True
    assert metadata["direct_messages_topic_id"] == source.thread_id
    assert metadata["telegram_reply_to_message_id"] == "restart-command"


@pytest.mark.asyncio
async def test_in_chat_restart_skips_home_shutdown_even_with_active_session():
    runner, adapter = make_restart_runner()
    source = make_restart_source(thread_id="42")
    session_key = build_session_key(source)
    runner._running_agents = {session_key: MagicMock()}
    runner._cache_session_source(session_key, source)
    restart_source = make_restart_source(thread_id="42")
    restart_source.message_id = "restart-command"
    runner._restart_requested = True
    runner._restart_command_source = restart_source
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="home-chat",
        name="Telegram Home",
    )

    await runner._notify_active_sessions_of_shutdown()

    assert len(adapter.sent_calls) == 1
    chat_id, message, metadata = adapter.sent_calls[0]
    assert chat_id == source.chat_id
    assert "Gateway restarting" in message
    assert metadata["telegram_reply_to_message_id"] == "restart-command"


@pytest.mark.asyncio
async def test_idle_in_chat_restart_does_not_send_interruption_warning():
    runner, adapter = make_restart_runner()
    source = make_restart_source(thread_id="42")
    source.message_id = "restart-command"
    runner._restart_requested = True
    runner._restart_command_source = source
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id=source.chat_id,
        name="Telegram",
        thread_id=source.thread_id,
    )

    await runner._notify_active_sessions_of_shutdown()

    assert adapter.sent_calls == []


@pytest.mark.asyncio
async def test_in_chat_restart_does_not_write_home_startup_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    source = make_restart_source(thread_id="42")
    source.message_id = "restart-command"
    runner._restart_command_source = source
    runner._launch_systemd_restart_shortcut = MagicMock()
    monkeypatch.setenv("INVOCATION_ID", "systemd-test")

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop(restart=True, service_restart=True)

    assert not (tmp_path / ".restart_pending.json").exists()


@pytest.mark.asyncio
async def test_drain_active_agents_throttles_status_updates():
    runner, _adapter = make_restart_runner()
    runner._update_runtime_status = MagicMock()

    runner._running_agents = {"a": MagicMock(), "b": MagicMock()}

    async def finish_agents():
        await asyncio.sleep(0.12)
        runner._running_agents.pop("a")
        await asyncio.sleep(0.12)
        runner._running_agents.clear()

    task = asyncio.create_task(finish_agents())
    await runner._drain_active_agents(1.0)
    await task

    # Start, one count-change update, and final update. Allow one extra update
    # if the loop observes the zero-agent state before exiting.
    assert 3 <= runner._update_runtime_status.call_count <= 4


@pytest.mark.asyncio
async def test_gateway_stop_kills_tool_subprocesses_before_adapter_disconnect_on_timeout(monkeypatch):
    """On drain timeout, tool subprocesses must be killed BEFORE adapter
    disconnect so systemd's TimeoutStopSec doesn't SIGKILL the cgroup with
    bash/sleep children still attached (#8202)."""
    runner, adapter = make_restart_runner()
    runner._restart_drain_timeout = 0.01  # force timeout path

    call_order: list[str] = []

    def _fake_kill_all(task_id=None):
        call_order.append("kill_all")
        return 2

    def _fake_cleanup_envs():
        call_order.append("cleanup_environments")

    def _fake_cleanup_browsers():
        call_order.append("cleanup_browsers")

    async def _disconnect():
        call_order.append("disconnect")

    # Patch the module-level names the stop() helper imports lazily.
    import tools.process_registry as _pr
    import tools.terminal_tool as _tt
    import tools.browser_tool as _bt
    monkeypatch.setattr(_pr.process_registry, "kill_all", _fake_kill_all)
    monkeypatch.setattr(_tt, "cleanup_all_environments", _fake_cleanup_envs)
    monkeypatch.setattr(_bt, "cleanup_all_browsers", _fake_cleanup_browsers)

    adapter.disconnect = _disconnect

    runner._running_agents = {"session": MagicMock()}

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    # First kill_all must precede the first disconnect.  (Both the eager
    # post-interrupt cleanup and the final catch-all call _kill_tool_
    # subprocesses, so we expect kill_all to appear twice total.)
    assert "kill_all" in call_order
    assert "disconnect" in call_order
    first_kill = call_order.index("kill_all")
    first_disconnect = call_order.index("disconnect")
    assert first_kill < first_disconnect, (
        f"Tool subprocesses must be killed before adapter disconnect on "
        f"drain timeout, got order: {call_order}"
    )
    # Defense-in-depth final cleanup still runs.
    assert call_order.count("kill_all") >= 2


@pytest.mark.asyncio
async def test_gateway_stop_kills_tool_subprocesses_on_graceful_path(monkeypatch):
    """Graceful shutdown (no drain timeout) must still kill tool subprocesses
    exactly once via the final catch-all — regression guard against
    accidentally removing that call when refactoring."""
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()

    kill_count = 0

    def _fake_kill_all(task_id=None):
        nonlocal kill_count
        kill_count += 1
        return 0

    import tools.process_registry as _pr
    import tools.terminal_tool as _tt
    import tools.browser_tool as _bt
    monkeypatch.setattr(_pr.process_registry, "kill_all", _fake_kill_all)
    monkeypatch.setattr(_tt, "cleanup_all_environments", lambda: None)
    monkeypatch.setattr(_bt, "cleanup_all_browsers", lambda: None)

    # No running agents → drain returns immediately, no timeout, no eager cleanup.
    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    # Only the final catch-all fires on the graceful path.
    assert kill_count == 1


# ---------------------------------------------------------------------------
# gateway_state persistence on shutdown (issue #42675)
#
# On Docker/s6, container_boot.py only auto-starts gateways whose last
# persisted gateway_state was "running". An unexpected external signal
# (the SIGTERM s6/Docker sends on `docker compose up --force-recreate`,
# OOM, bare kill) must NOT persist "stopped" — otherwise the gateway
# stays down after every container restart. An operator-initiated stop
# writes a planned-stop marker first, so it is NOT signal-initiated and
# DOES persist "stopped", respecting the explicit intent.
# ---------------------------------------------------------------------------


def _persisted_states(runner) -> list:
    """All gateway_state values passed to _update_runtime_status, in order."""
    states = []
    for call in runner._update_runtime_status.call_args_list:
        args, kwargs = call
        state = kwargs.get("gateway_state", args[0] if args else None)
        states.append(state)
    return states


def _stopped_state_persisted(runner) -> bool:
    """True iff _update_runtime_status was called with gateway_state='stopped'."""
    return "stopped" in _persisted_states(runner)


@pytest.mark.asyncio
async def test_signal_initiated_shutdown_persists_running_not_stopped(tmp_path, monkeypatch):
    """Unexpected SIGTERM (container restart / OOM / kill) must persist
    gateway_state=running — NOT stopped, and NOT leave the mid-shutdown
    'draining' marker — so container_boot auto-starts on next boot (#42675)."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner._signal_initiated_shutdown = True  # set by handler on unmarked signal

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    assert not _stopped_state_persisted(runner), (
        "signal-initiated shutdown must NOT persist gateway_state=stopped"
    )
    # The FINAL terminal write must be 'running' so container_boot's
    # _AUTOSTART_STATES check passes (it only auto-starts 'running').
    assert _persisted_states(runner)[-1] == "running", (
        f"final state must be 'running', got: {_persisted_states(runner)}"
    )


@pytest.mark.asyncio
async def test_operator_initiated_stop_persists_stopped(tmp_path, monkeypatch):
    """A planned stop (marker written → not signal-initiated) must persist
    gateway_state=stopped so an explicit `hermes gateway stop` stays down."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner._signal_initiated_shutdown = False  # planned stop classification

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    assert _stopped_state_persisted(runner), (
        "operator-initiated stop must persist gateway_state=stopped"
    )


@pytest.mark.asyncio
async def test_signal_initiated_restart_still_persists_stopped(tmp_path, monkeypatch):
    """A restart is not a 'stay down' — it persists normally (the new
    process/container brings the gateway back up itself). The suppression
    only applies to a terminal signal-initiated stop, not a restart."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner._signal_initiated_shutdown = True
    runner._launch_systemd_restart_shortcut = MagicMock()

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop(restart=True, service_restart=True)

    assert _stopped_state_persisted(runner), (
        "a restart must persist gateway_state=stopped via the normal path"
    )


# ── #42126: zombie PID must be treated as dead in _pid_exists ────────────────
# Under systemd Restart=always, the old gateway becomes a zombie (still in the
# process table, not yet reaped) when the replacement starts. _pid_exists must
# report it dead so --replace proceeds instead of waiting on it and aborting
# with exit 1 (a silent crash loop).


def test_pid_exists_zombie_via_psutil_returns_false(monkeypatch):
    """The live path is psutil. psutil.pid_exists() returns True for a zombie,
    so _pid_exists must additionally check Process.status() == STATUS_ZOMBIE."""
    import sys
    import types

    from gateway import status

    fake_psutil = types.SimpleNamespace()
    fake_psutil.STATUS_ZOMBIE = "zombie"

    class NoSuchProcess(Exception):
        pass

    class PsutilError(Exception):
        pass

    fake_psutil.NoSuchProcess = NoSuchProcess
    fake_psutil.Error = PsutilError

    class _Proc:
        def __init__(self, pid):
            self.pid = pid

        def status(self):
            return "zombie"

    fake_psutil.Process = _Proc
    # Without the zombie guard, this True would make the caller treat the
    # zombie as a live gateway.
    fake_psutil.pid_exists = lambda pid: True

    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert status._pid_exists(4242) is False


def test_pid_exists_live_via_psutil_returns_true(monkeypatch):
    """A genuinely running (non-zombie) process is still reported alive."""
    import sys
    import types

    from gateway import status

    fake_psutil = types.SimpleNamespace()
    fake_psutil.STATUS_ZOMBIE = "zombie"
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.Error = type("Error", (Exception,), {})

    class _Proc:
        def __init__(self, pid):
            self.pid = pid

        def status(self):
            return "running"

    fake_psutil.Process = _Proc
    fake_psutil.pid_exists = lambda pid: True

    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert status._pid_exists(4242) is True


def test_pid_exists_zombie_via_proc_fallback_returns_false(monkeypatch):
    """When psutil is unavailable, the POSIX fallback reads /proc/<pid>/stat
    and must treat state 'Z' as dead before reaching os.kill."""
    import builtins
    import sys

    from gateway import status

    monkeypatch.setitem(sys.modules, "psutil", None)  # force ImportError
    real_import = builtins.__import__

    def _no_psutil(name, *a, **k):
        if name == "psutil":
            raise ImportError("psutil disabled for test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    monkeypatch.setattr(status, "_IS_WINDOWS", False)

    fake_stat = "4242 (defunct) Z 1 0 0 0 -1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
    fake_path = MagicMock()
    fake_path.read_text.return_value = fake_stat
    monkeypatch.setattr(status, "Path", lambda *_a, **_k: fake_path)

    kill = MagicMock()
    monkeypatch.setattr(status.os, "kill", kill)

    assert status._pid_exists(4242) is False
    kill.assert_not_called()
