"""Tests for session-scoped persona switching."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.bus.events import InboundMessage


def _make_loop(workspace: Path, provider: MagicMock | None = None):
    """Create an AgentLoop with a real workspace and lightweight mocks."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = provider or MagicMock()
    provider.get_default_model.return_value = "test-model"

    with patch("nanobot.agent.loop.SubagentManager"):
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace)
    return loop, provider


def _make_persona(workspace: Path, name: str, soul: str) -> None:
    persona_dir = workspace / "personas" / name
    persona_dir.mkdir(parents=True)
    (persona_dir / "SOUL.md").write_text(soul, encoding="utf-8")


class TestPersonaCommands:

    @pytest.mark.asyncio
    async def test_persona_switch_clears_session_and_persists_selection(self, tmp_path: Path) -> None:
        _make_persona(tmp_path, "coder", "You are coder persona.")
        loop, _provider = _make_loop(tmp_path)
        loop.memory_consolidator.archive_unconsolidated = AsyncMock(return_value=True)

        session = loop.sessions.get_or_create("cli:direct")
        session.add_message("user", "hello")
        session.add_message("assistant", "hi")
        loop.sessions.save(session)

        response = await loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/persona set coder")
        )

        assert response is not None
        assert response.content == "Switched persona to coder. New session started."
        loop.memory_consolidator.archive_unconsolidated.assert_awaited_once()

        switched = loop.sessions.get_or_create("cli:direct")
        assert switched.metadata["persona"] == "coder"
        assert switched.messages == []

        current = await loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/persona current")
        )
        listing = await loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/persona list")
        )

        assert current is not None
        assert current.content == "Current persona: coder"
        assert listing is not None
        assert "- default" in listing.content
        assert "- coder (current)" in listing.content

    @pytest.mark.asyncio
    async def test_help_includes_persona_commands(self, tmp_path: Path) -> None:
        loop, _provider = _make_loop(tmp_path)

        response = await loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/help")
        )

        assert response is not None
        assert "/persona current" in response.content
        assert "/persona set <name>" in response.content

    @pytest.mark.asyncio
    async def test_language_switch_localizes_help(self, tmp_path: Path) -> None:
        loop, _provider = _make_loop(tmp_path)

        switched = await loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/lang set zh")
        )
        help_response = await loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/help")
        )

        assert switched is not None
        assert "已切换语言为" in switched.content
        assert help_response is not None
        assert "/lang current — 查看当前语言" in help_response.content
        assert "/persona current — 查看当前人格" in help_response.content

    @pytest.mark.asyncio
    async def test_active_persona_changes_prompt_memory_scope(self, tmp_path: Path) -> None:
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"
        provider.chat_with_retry = AsyncMock(
            return_value=SimpleNamespace(
                has_tool_calls=False,
                content="ok",
                finish_reason="stop",
                reasoning_content=None,
                thinking_blocks=None,
            )
        )

        (tmp_path / "SOUL.md").write_text("root soul", encoding="utf-8")
        persona_dir = tmp_path / "personas" / "coder"
        persona_dir.mkdir(parents=True)
        (persona_dir / "SOUL.md").write_text("coder soul", encoding="utf-8")
        (persona_dir / "memory").mkdir()
        (persona_dir / "memory" / "MEMORY.md").write_text("coder memory", encoding="utf-8")

        loop, provider = _make_loop(tmp_path, provider)
        session = loop.sessions.get_or_create("cli:direct")
        session.metadata["persona"] = "coder"
        loop.sessions.save(session)

        response = await loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="hello")
        )

        assert response is not None
        assert response.content == "ok"

        messages = provider.chat_with_retry.await_args.kwargs["messages"]
        assert "Current persona: coder" in messages[0]["content"]
        assert "coder soul" in messages[0]["content"]
        assert "coder memory" in messages[0]["content"]
        assert "root soul" not in messages[0]["content"]
