"""Tests for /mcp slash command integration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.bus.events import InboundMessage


class _FakeTool:
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        return ""


def _make_loop(workspace: Path, *, mcp_servers: dict | None = None):
    """Create an AgentLoop with a real workspace and lightweight mocks."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    with patch("nanobot.agent.loop.SubagentManager"):
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace, mcp_servers=mcp_servers)
    return loop


@pytest.mark.asyncio
async def test_mcp_lists_configured_servers_and_tools(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path, mcp_servers={"docs": object(), "search": object()})
    loop.tools.register(_FakeTool("mcp_docs_lookup"))
    loop.tools.register(_FakeTool("mcp_search_web"))
    loop.tools.register(_FakeTool("read_file"))

    with patch.object(loop, "_connect_mcp", AsyncMock()) as connect_mcp:
        response = await loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/mcp")
        )

    assert response is not None
    assert "Configured MCP servers:" in response.content
    assert "- docs" in response.content
    assert "- search" in response.content
    assert "docs: lookup" in response.content
    assert "search: web" in response.content
    connect_mcp.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_without_servers_returns_guidance(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/mcp list")
    )

    assert response is not None
    assert response.content == "No MCP servers are configured for this agent."


@pytest.mark.asyncio
async def test_help_includes_mcp_command(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/help")
    )

    assert response is not None
    assert "/mcp [list]" in response.content
