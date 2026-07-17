"""Regression tests for /delegate while a gateway agent is running."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_runner():
    from gateway.run import GatewayRunner

    source = _make_source()
    session_key = build_session_key(source)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key=session_key,
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    running_agent = MagicMock()
    running_agent.get_activity_summary.return_value = {"seconds_since_activity": 0}
    runner._running_agents = {session_key: running_agent}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._queued_events = {}
    runner._pending_approvals = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner, adapter, running_agent, session_key


@pytest.mark.asyncio
async def test_delegate_queues_without_interrupting_running_agent():
    runner, adapter, running_agent, session_key = _make_runner()
    event = MessageEvent(
        text="/delegate inspect the failing shard",
        source=_make_source(),
        message_id="delegate-1",
    )

    result = await runner._handle_message(event)

    assert result is None
    running_agent.interrupt.assert_not_called()
    queued = adapter._pending_messages[session_key]
    assert queued.text == "/delegate inspect the failing shard"

