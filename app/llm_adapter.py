"""LLM adapter layer — the ONLY place that talks to a concrete AI provider.

Business code (ai_service.py) calls the provider-independent interface:

    adapter.chat(system, messages, tools) -> {"text": str, "usage": {...}}
                                          |  {"tool_call": ToolCall, "usage": {...}}

Messages are normalized dicts, never provider formats:
    {"role": "user",      "text": str}
    {"role": "assistant", "text": str, "tool_calls": [ToolCall, ...]}
    {"role": "tool",      "tool_call_id": str, "name": str, "text": str}

Adding a new provider (e.g. your own model) = writing one more subclass of
LLMAdapter. Prompts, tools and business logic never change.
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

from .config import settings

log = logging.getLogger("bot.llm")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Claude can be slow on long knowledge bases / busy hours.
_client = httpx.Client(timeout=60)


@dataclass
class ToolCall:
    """A tool the model wants to run. `id` is provider-specific and must be
    echoed back with the tool result in the next chat() round."""
    id: str
    name: str
    arguments: dict = field(default_factory=dict)


class LLMAdapter(ABC):
    """Provider-independent chat interface."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider/model name for logs."""

    @abstractmethod
    def chat(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        """One chat round. `tools` is a list of
        {"name", "description", "input_schema"} specs (JSON Schema inputs).
        Returns {"text": str, "usage": {...}} or
        {"tool_call": ToolCall, "usage": {...}}.
        Raises on transport/provider errors — the caller decides the fallback."""


class ClaudeAdapter(LLMAdapter):
    """Anthropic Messages API adapter (tools = tool_use blocks)."""

    @property
    def name(self) -> str:
        return settings.CLAUDE_MODEL

    def chat(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        payload = {
            "model": settings.CLAUDE_MODEL,
            "max_tokens": settings.CLAUDE_MAX_TOKENS,
            "system": system,
            "messages": _to_claude_messages(messages),
        }
        if tools:
            payload["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("input_schema", {"type": "object"}),
                }
                for t in tools
            ]
        data = _post_messages(payload)
        if data.get("type") == "error":
            raise RuntimeError(f"Claude API error: {data.get('error')}")
        usage = data.get("usage") or {}
        usage_out = {
            "prompt_tokens": usage.get("input_tokens"),
            "completion_tokens": usage.get("output_tokens"),
        }
        if data.get("stop_reason") == "tool_use":
            for block in data.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    return {
                        "tool_call": ToolCall(
                            id=block.get("id") or "",
                            name=block.get("name") or "",
                            arguments=block.get("input") or {},
                        ),
                        "usage": usage_out,
                    }
            raise RuntimeError("stop_reason=tool_use but no tool_use block")
        text = "".join(
            block.get("text", "")
            for block in data.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        return {"text": text, "usage": usage_out}


def _to_claude_messages(messages: list[dict]) -> list[dict]:
    """Normalized messages -> Anthropic format (alternating user/assistant,
    tool results become user tool_result blocks, tool calls become
    assistant tool_use blocks)."""
    out: list[dict] = []
    for message in messages:
        role = message.get("role")
        text = (message.get("text") or "").strip()
        if role == "user":
            _append_merged(out, "user", text or "(bo'sh xabar)")
        elif role == "assistant":
            tool_calls = message.get("tool_calls") or []
            if not text and not tool_calls:
                continue
            content: list[dict] = []
            if text:
                content.append({"type": "text", "text": text})
            for call in tool_calls:
                content.append({
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments or {},
                })
            _append_blocks(out, "assistant", content)
        elif role == "tool":
            _append_blocks(out, "user", [{
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id") or "",
                "content": (message.get("text") or "").strip() or "(natija bo'sh)",
            }])
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _append_merged(out: list[dict], role: str, text: str) -> None:
    if out and out[-1]["role"] == role and isinstance(out[-1]["content"], str):
        out[-1]["content"] += "\n\n" + text
    else:
        out.append({"role": role, "content": text})


def _append_blocks(out: list[dict], role: str, blocks: list[dict]) -> None:
    if out and out[-1]["role"] == role and isinstance(out[-1]["content"], list):
        out[-1]["content"].extend(blocks)
    else:
        out.append({"role": role, "content": blocks})


def _post_messages(payload: dict) -> dict:
    """One POST to the Anthropic Messages API. Returns the parsed JSON body.

    Kept as a tiny separate function so tests can monkeypatch it.
    """
    response = _client.post(
        ANTHROPIC_URL,
        headers={
            "x-api-key": settings.ANTHROPIC_API_KEY,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json=payload,
    )
    return response.json()


_ADAPTERS = {
    "claude": ClaudeAdapter,
    # "own": OwnModelAdapter  # your own model goes here — one class, nothing else
}


def get_adapter() -> LLMAdapter | None:
    """The configured adapter, or None when the provider is unknown/disabled.

    None means the caller falls back to the built-in button flow.
    """
    if not settings.ANTHROPIC_API_KEY:
        return None
    adapter_cls = _ADAPTERS.get(settings.LLM_PROVIDER)
    if adapter_cls is None:
        log.warning("Unknown LLM_PROVIDER %r — falling back to the built-in flow",
                    settings.LLM_PROVIDER)
        return None
    return adapter_cls()


def is_enabled() -> bool:
    """True when AI answers are possible (provider configured and enabled)."""
    return get_adapter() is not None
