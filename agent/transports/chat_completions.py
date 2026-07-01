"""Transport layer for OpenAI / Chat Completions compatible providers.

Maps the generic agent gateway into the OpenAI Chat Completions protocol.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union

from agent.model_metadata import ModelMetadata, is_openai_compatible
from agent.transports.types import (
    GatewayConfig,
    GenericResponse,
    NormalizedResponse,
    SenderInfo,
    Transport,
)
from utils import env_float, env_int

logger = logging.getLogger(__name__)

# Defaults for chunk assembly
_CONTENT_KEY = "content"
_REASONING_KEY = "reasoning"
_TOOL_CALL_KEY = "tool_calls"

# Lock and counter for generating unique IDs without relying on uuid4 in hot loops
_id_lock = threading.Lock()
_id_counter: int = 0


def _unique_chunk_id() -> str:
    """Thread-safe monotonically increasing chunk ID (no uuid4 allocation)."""
    global _id_counter
    with _id_lock:
        _id_counter += 1
        return f"chunk_{_id_counter}_{time.monotonic_ns()}"


def _parse_delta(delta: Any, prev_tool_call_index: Optional[int]) -> dict:
    """Extract (content, reasoning, tool_calls) from a single SSE delta chunk."""
    result: dict = {}
    if delta is None:
        return result
    c = getattr(delta, _CONTENT_KEY, None)
    if c is not None:
        result[_CONTENT_KEY] = c
    r = getattr(delta, _REASONING_KEY, None)
    if r is not None:
        result[_REASONING_KEY] = r
    tc = getattr(delta, _TOOL_CALL_KEY, None)
    if tc is not None and isinstance(tc, (list, tuple)):
        result[_TOOL_CALL_KEY] = _normalize_tool_calls_delta(tc, prev_tool_call_index)
    return result


def _normalize_tool_calls_delta(
    deltas: Any, prev_index: Optional[int]
) -> list:
    """Collapse a delta's tool_calls list into a stable list of partials.

    The OpenAI streaming API emits one element per tool-call *segment*
    (index+id, index+function.name, index+function.arguments).  We group
    by index, accumulating the longest-seen name and coalescing arguments.
    """
    partials: dict = {}
    for tc in deltas:
        idx = tc.get("index", 0) if isinstance(tc, dict) else tc.index
        if idx not in partials:
            partials[idx] = {"index": idx, "id": "", "function": {"name": "", "arguments": ""}}
        entry = partials[idx]
        # id arrives on the first segment for this index
        raw_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
        if raw_id:
            entry["id"] = raw_id
        fn = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", {})
        if fn:
            fn_name = fn.get("name", "") if isinstance(fn, dict) else getattr(fn, "name", "")
            if fn_name:
                entry["function"]["name"] = fn_name
            fn_args = fn.get("arguments", "") if isinstance(fn, dict) else getattr(fn, "arguments", "")
            if fn_args:
                entry["function"]["arguments"] += fn_args
    return list(partials.values())


def _stream_field(msg: Any, name: str, default: Any = None) -> Any:
    """Thread-safe field access on a message-like that may run in a daemon thread

    Some providers (e.g. DeepSeek) mutate message objects from a streaming
    thread after the response is enqueued. Access via getattr so we don't
    crash on missing attributes.
    """
    try:
        return getattr(msg, name, default)
    except Exception:
        return default


class ChatCompletionsTransport(Transport):
    """Transport for OpenAI Chat Completions compatible endpoints."""

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self._streaming = True
        self._provider_data: dict = {}
        # Track the last tool call index seen across chunks
        self._last_tool_call_index: Optional[int] = None

    def normalize_response(self, response: Any, **kwargs: Any) -> NormalizedResponse:
        """Normalize a Chat Completions response into NormalizedResponse."""
        msg = response.choices[0].message if response.choices else None
        if msg is None:
            return NormalizedResponse(
                content=None, tool_calls=None, finish_reason=None, reasoning=None, usage=None, provider_data=None
            )

        finish_reason = _stream_field(response.choices[0], "finish_reason")
        reasoning = _stream_field(msg, "reasoning_content", _stream_field(msg, "reasoning", None))
        tool_calls_raw = _stream_field(msg, "tool_calls", None)
        usage = _stream_field(response, "usage", None)

        tool_calls = None
        if tool_calls_raw and isinstance(tool_calls_raw, (list, tuple)):
            tool_calls = []
            for tc in tool_calls_raw:
                tc_id = _stream_field(tc, "id", "")
                tc_type = _stream_field(tc, "type", "function")
                tc_fn = _stream_field(tc, "function", None)
                if tc_fn:
                    tool_calls.append({
                        "id": tc_id,
                        "type": tc_type,
                        "function": {
                            "name": _stream_field(tc_fn, "name", ""),
                            "arguments": _stream_field(tc_fn, "arguments", ""),
                        },
                    })

        provider_data = {}

        if reasoning is not None:
            provider_data["reasoning_content"] = reasoning
        rd = getattr(msg, "reasoning_details", None)
        if rd:
            provider_data["reasoning_details"] = rd

        # OpenAI structured-refusal field. When a model declines, the SDK
        # populates ``message.refusal`` with the explanation and leaves
        # ``content`` empty. OpenAI-compatible proxies that front Anthropic /
        # Bedrock (e.g. Nous Portal) surface a Claude refusal this way — or via
        # ``finish_reason=\"content_filter\"`` — instead of the native
        # ``stop_reason=\"refusal\"``. Without capturing it the refusal looks
        # like an empty response, so the agent loop retries a deterministic
        # refusal three times and gives up with "no content after retries".
        # Promote it to content + a ``content_filter`` finish reason so the
        # loop's refusal handler surfaces it clearly and stops. ``refusal`` is
        # ``None`` for normal responses, so this is a no-op in the common case.
        content = msg.content
        refusal = getattr(msg, "refusal", None)
        if refusal is None and hasattr(msg, "model_extra"):
            _msg_extra = getattr(msg, "model_extra", None) or {}
            if isinstance(_msg_extra, dict):
                refusal = _msg_extra.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            # Record the refusal explanation regardless — it's useful provider
            # metadata even when the model also returned a usable payload.
            provider_data["refusal"] = refusal
            _has_text = isinstance(content, str) and content.strip()
            _has_tool_calls = bool(tool_calls)
            # Only promote to a terminal ``content_filter`` when the refusal is
            # the *sole* payload — no visible text and no tool calls. A response
            # that carries real content (or tool calls) alongside a refusal note
            # is a normal, usable turn: surfacing it as a failed safety refusal
            # would discard the model's actual work. In the empty-payload case,
            # adopt the refusal as content so the loop has something to show.
            if not _has_text and not _has_tool_calls:
                content = refusal
                if finish_reason in (None, "stop"):
                    finish_reason = "content_filter"

        # Thinking models (Qwen, DeepSeek) put output in reasoning with content=null.
        # Fall back so the response is actually usable.
        if not content and reasoning:
            content = reasoning

        return NormalizedResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning=reasoning,
            usage=usage,
            provider_data=provider_data or None,
        )

    def validate_response(self, response: Any) -> bool:
        """Check that response has valid choices."""
        if response is None:
            return False
        if not hasattr(response, "choices") or response.choices is None:
            return False
        return True
