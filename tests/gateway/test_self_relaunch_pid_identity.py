"""Regression tests for the orphaned-gateway / duplicate-gateway incident.

A gateway that self-relaunches in place (``os.execvp`` — same PID) ends up with
a live ``/proc/<pid>/cmdline`` of bare ``hermes``: ``build_relaunch_argv``
rebuilds argv as ``[bin] + inherited_flags + extra_args`` and drops the
``gateway run`` positional. ``execve`` preserves the PID *and* start_time, so
the persisted pid record still matches.

Before the fix, ``get_running_pid`` → ``_record_matches_live_gateway_pid``
preferred that truncated live cmdline, failed the ``gateway run`` subcommand
test, and returned False — declaring a live gateway "not running". ``--replace``
then saw no gateway, skipped the reap, and spawned a duplicate that fought the
survivor over the bot token, mailbox, and state.db.

The fix trusts the persisted record's argv (already start_time-matched by the
caller) when the live image is still the ``hermes`` binary.
"""

import json
import os

from gateway import status


def _seed_gateway_pidfile(tmp_path, argv, start_time=123):
    pid_path = tmp_path / "gateway.pid"
    pid_path.write_text(json.dumps({
        "pid": os.getpid(),
        "kind": "hermes-gateway",
        "argv": argv,
        "start_time": start_time,
    }))
    return pid_path


class TestSelfRelaunchedGatewayIdentity:
    def test_running_pid_accepts_truncated_hermes_cmdline(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_gateway_pidfile(
            tmp_path,
            ["/venv/bin/python", "/venv/bin/hermes", "gateway", "run", "--replace"],
        )
        monkeypatch.setattr(status.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(status, "_get_process_start_time", lambda pid: 123)
        # Post-relaunch live cmdline: bare hermes, no ``gateway run`` subcommand.
        monkeypatch.setattr(
            status, "_read_process_cmdline",
            lambda pid: "/venv/bin/python /venv/bin/hermes",
        )
        assert status.acquire_gateway_runtime_lock() is True
        try:
            assert status.get_running_pid() == os.getpid()
        finally:
            status.release_gateway_runtime_lock()

    def test_running_pid_accepts_python_m_relaunch_cmdline(self, tmp_path, monkeypatch):
        # The ``python -m hermes_cli.main`` relaunch form (no ``hermes`` argv0).
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_gateway_pidfile(
            tmp_path, ["/venv/bin/python", "-m", "hermes_cli.main", "gateway", "run"],
        )
        monkeypatch.setattr(status.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(status, "_get_process_start_time", lambda pid: 123)
        monkeypatch.setattr(
            status, "_read_process_cmdline",
            lambda pid: "/venv/bin/python -m hermes_cli.main",
        )
        assert status.acquire_gateway_runtime_lock() is True
        try:
            assert status.get_running_pid() == os.getpid()
        finally:
            status.release_gateway_runtime_lock()

    def test_running_pid_rejects_recycled_pid_running_other_program(self, tmp_path, monkeypatch):
        # The rescue must NOT fire when the live image is not hermes: a recycled
        # PID running an unrelated program can't be vouched for by the record.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_gateway_pidfile(
            tmp_path, ["/venv/bin/python", "/venv/bin/hermes", "gateway", "run"],
        )
        monkeypatch.setattr(status.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(status, "_get_process_start_time", lambda pid: 123)
        monkeypatch.setattr(status, "_read_process_cmdline", lambda pid: "/usr/sbin/sshd -D")
        assert status.acquire_gateway_runtime_lock() is True
        try:
            assert status.get_running_pid() is None
        finally:
            status.release_gateway_runtime_lock()


class TestCommandLineIsHermesBinary:
    def test_matches_hermes_shim(self):
        assert status._command_line_is_hermes_binary("/venv/bin/python /venv/bin/hermes")

    def test_matches_module_form(self):
        assert status._command_line_is_hermes_binary("/venv/bin/python -m hermes_cli.main")

    def test_rejects_unrelated(self):
        assert not status._command_line_is_hermes_binary("/usr/sbin/sshd -D")

    def test_rejects_empty(self):
        assert not status._command_line_is_hermes_binary("")
        assert not status._command_line_is_hermes_binary(None)
