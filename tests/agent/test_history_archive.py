from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.history_archive import HistoryArchiveStore
from nanobot.agent.memory import MemoryStore
from nanobot.agent.tools.history import HistoryExpandTool, HistorySearchTool
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings, LLMResponse, ToolCallRequest


def _messages() -> list[dict]:
    return [
        {
            "role": "user",
            "content": "Please review `nanobot/agent/loop.py` and providerPool behavior.",
            "timestamp": "2026-03-30T12:00:01",
        },
        {
            "role": "assistant",
            "content": "I will inspect /status and providerPool handling first.",
            "timestamp": "2026-03-30T12:00:05",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read_file"}}
            ],
        },
        {
            "role": "tool",
            "name": "read_file",
            "content": "contents of nanobot/agent/loop.py",
            "timestamp": "2026-03-30T12:00:10",
        },
        {
            "role": "assistant",
            "content": "The admin page and providerPool flow both need updates.",
            "timestamp": "2026-03-30T12:00:20",
        },
    ]


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _make_memory_tool_response(summary: str) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[
            ToolCallRequest(
                id="call_1",
                name="save_memory",
                arguments={
                    "history_entry": summary,
                    "memory_update": "# Memory\n- providerPool takes precedence when configured.",
                },
            )
        ],
    )


@pytest.mark.asyncio
async def test_memory_store_consolidate_writes_archive_sidecar(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    provider = AsyncMock()
    provider.chat_with_retry = AsyncMock(
        return_value=_make_memory_tool_response(
            "[2026-03-30 12:00] Reviewed providerPool handling in nanobot/agent/loop.py and "
            "admin flows."
        )
    )
    archive = HistoryArchiveStore(tmp_path)
    messages = _messages()

    result = await store.consolidate(
        messages,
        provider,
        "test-model",
        on_archive=lambda payload: archive.write_archive(
            session_key="cli:direct",
            messages=messages,
            history_entry=payload["history_entry"],
            source="token_consolidation",
            raw_archive=bool(payload.get("raw_archive")),
        ),
    )

    assert result is True
    index_path = tmp_path / "memory" / "archive" / "index.jsonl"
    assert index_path.exists()
    entries = _read_jsonl(index_path)
    assert len(entries) == 1
    assert entries[0]["sessionKey"] == "cli:direct"
    assert entries[0]["source"] == "token_consolidation"
    assert "providerPool" in entries[0]["summary"]

    chunk_path = tmp_path / "memory" / "archive" / entries[0]["chunkPath"]
    chunk = json.loads(chunk_path.read_text(encoding="utf-8"))
    assert len(chunk["messages"]) == 4
    assert chunk["messages"][2]["name"] == "read_file"


@pytest.mark.asyncio
async def test_history_search_prioritizes_current_session(tmp_path: Path) -> None:
    store = HistoryArchiveStore(tmp_path)
    store.write_archive(
        session_key="cli:other",
        messages=_messages(),
        history_entry="[2026-03-30 12:00] providerPool discussion from another session.",
        source="token_consolidation",
    )
    current_id = store.write_archive(
        session_key="cli:direct",
        messages=_messages(),
        history_entry="[2026-03-30 12:01] providerPool discussion in the active session.",
        source="token_consolidation",
    )

    tool = HistorySearchTool(tmp_path)
    tool.set_context("cli", "direct", "default")
    output = await tool.execute("providerPool", limit=2)

    assert output.startswith('Archived history matches for "providerPool":')
    first_id = re.search(r"1\. ID: ([^\n]+)", output)
    assert first_id is not None
    assert first_id.group(1).strip() == current_id


@pytest.mark.asyncio
async def test_history_expand_survives_new_session_clear(tmp_path: Path) -> None:
    from nanobot.agent.loop import AgentLoop

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=256)
    provider.chat_with_retry = AsyncMock(
        return_value=_make_memory_tool_response(
            "[2026-03-30 12:00] Reviewed providerPool handling in nanobot/agent/loop.py."
        )
    )
    provider.chat_stream_with_retry = AsyncMock(
        return_value=SimpleNamespace(
            has_tool_calls=False,
            content="ok",
            finish_reason="stop",
            reasoning_content=None,
            thinking_blocks=None,
            tool_calls=[],
            usage=None,
        )
    )

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=1024,
    )

    session = loop.sessions.get_or_create("cli:test")
    session.messages = _messages()
    loop.sessions.save(session)

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="test", content="/new")
    )
    assert response is not None
    assert "new session started" in response.content.lower()

    session_after = loop.sessions.get_or_create("cli:test")
    assert session_after.messages == []

    await loop.close_mcp()

    search_tool = HistorySearchTool(tmp_path)
    search_tool.set_context("cli", "test", "default")
    search_output = await search_tool.execute("providerPool", limit=1)
    match = re.search(r"1\. ID: ([^\n]+)", search_output)
    assert match is not None

    expand_tool = HistoryExpandTool(tmp_path)
    expand_tool.set_context("cli", "test", "default")
    expand_output = await expand_tool.execute(match.group(1).strip(), maxMessages=10)

    assert "Archived transcript" in expand_output
    assert "providerPool behavior" in expand_output
    assert "TOOL(read_file)" in expand_output


@pytest.mark.asyncio
async def test_history_expand_reports_missing_chunk(tmp_path: Path) -> None:
    store = HistoryArchiveStore(tmp_path)
    archive_id = store.write_archive(
        session_key="cli:direct",
        messages=_messages(),
        history_entry="[2026-03-30 12:00] providerPool discussion.",
        source="token_consolidation",
    )
    entries = _read_jsonl(tmp_path / "memory" / "archive" / "index.jsonl")
    chunk_path = tmp_path / "memory" / "archive" / entries[0]["chunkPath"]
    chunk_path.unlink()

    tool = HistoryExpandTool(tmp_path)
    tool.set_context("cli", "direct", "default")
    output = await tool.execute(archive_id or "")

    assert output.startswith("Error:")
    assert "missing" in output.lower()
