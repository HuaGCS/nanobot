"""Tests for /mcp slash command integration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
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


def _make_loop(workspace: Path, *, mcp_servers: dict | None = None, config_path: Path | None = None):
    """Create an AgentLoop with a real workspace and lightweight mocks."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    with patch("nanobot.agent.loop.SubagentManager"):
        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=workspace,
            config_path=config_path,
            mcp_servers=mcp_servers,
        )
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


@pytest.mark.asyncio
async def test_mcp_command_hot_reloads_servers_from_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"tools": {}}), encoding="utf-8")
    loop = _make_loop(tmp_path, mcp_servers={}, config_path=config_path)

    config_path.write_text(
        json.dumps(
            {
                "tools": {
                    "mcpServers": {
                        "docs": {
                            "command": "npx",
                            "args": ["-y", "@demo/docs"],
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with patch.object(loop, "_connect_mcp", AsyncMock()) as connect_mcp:
        response = await loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/mcp")
        )

    assert response is not None
    assert "Configured MCP servers:" in response.content
    assert "- docs" in response.content
    connect_mcp.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_config_reload_resets_connections_and_tools(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "tools": {
                    "mcpServers": {
                        "old": {
                            "command": "npx",
                            "args": ["-y", "@demo/old"],
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    loop = _make_loop(
        tmp_path,
        mcp_servers={"old": SimpleNamespace(model_dump=lambda: {"command": "npx", "args": ["-y", "@demo/old"]})},
        config_path=config_path,
    )
    stack = SimpleNamespace(aclose=AsyncMock())
    loop._mcp_stack = stack
    loop._mcp_connected = True
    loop.tools.register(_FakeTool("mcp_old_lookup"))

    config_path.write_text(
        json.dumps(
            {
                "tools": {
                    "mcpServers": {
                        "new": {
                            "command": "npx",
                            "args": ["-y", "@demo/new"],
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    await loop._reload_mcp_servers_if_needed(force=True)

    assert list(loop._mcp_servers) == ["new"]
    assert loop._mcp_connected is False
    assert loop.tools.get("mcp_old_lookup") is None
    stack.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_regular_messages_pick_up_reloaded_mcp_config(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"tools": {}}), encoding="utf-8")
    loop = _make_loop(tmp_path, mcp_servers={}, config_path=config_path)

    loop.provider.chat_with_retry = AsyncMock(
        return_value=SimpleNamespace(
            has_tool_calls=False,
            content="ok",
            finish_reason="stop",
            reasoning_content=None,
            thinking_blocks=None,
        )
    )

    config_path.write_text(
        json.dumps(
            {
                "tools": {
                    "mcpServers": {
                        "docs": {
                            "command": "npx",
                            "args": ["-y", "@demo/docs"],
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    connect_mcp_servers = AsyncMock()
    monkeypatch.setattr("nanobot.agent.tools.mcp.connect_mcp_servers", connect_mcp_servers)

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="hello")
    )

    assert response is not None
    assert response.content == "ok"
    assert list(loop._mcp_servers) == ["docs"]
    connect_mcp_servers.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_config_reload_updates_agent_and_tool_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "model": "initial-model",
                        "maxToolIterations": 4,
                        "contextWindowTokens": 4096,
                        "maxTokens": 1000,
                        "temperature": 0.2,
                        "reasoningEffort": "low",
                    }
                },
                "tools": {
                    "restrictToWorkspace": False,
                    "exec": {"timeout": 20, "pathAppend": ""},
                    "web": {
                        "proxy": "",
                        "search": {
                            "provider": "brave",
                            "apiKey": "",
                            "baseUrl": "",
                            "maxResults": 3,
                        }
                    },
                },
                "channels": {
                    "sendProgress": True,
                    "sendToolHints": False,
                },
            }
        ),
        encoding="utf-8",
    )
    loop = _make_loop(tmp_path, mcp_servers={}, config_path=config_path)

    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "model": "reloaded-model",
                        "maxToolIterations": 9,
                        "contextWindowTokens": 8192,
                        "maxTokens": 2222,
                        "temperature": 0.7,
                        "reasoningEffort": "high",
                    }
                },
                "tools": {
                    "restrictToWorkspace": True,
                    "exec": {"timeout": 45, "pathAppend": "/usr/local/bin"},
                    "web": {
                        "proxy": "http://127.0.0.1:7890",
                        "search": {
                            "provider": "searxng",
                            "apiKey": "demo-key",
                            "baseUrl": "https://search.example.com",
                            "maxResults": 7,
                        }
                    },
                },
                "channels": {
                    "sendProgress": False,
                    "sendToolHints": True,
                },
            }
        ),
        encoding="utf-8",
    )

    await loop._reload_runtime_config_if_needed(force=True)

    exec_tool = loop.tools.get("exec")
    web_search_tool = loop.tools.get("web_search")
    web_fetch_tool = loop.tools.get("web_fetch")
    read_tool = loop.tools.get("read_file")

    assert loop.model == "reloaded-model"
    assert loop.max_iterations == 9
    assert loop.context_window_tokens == 8192
    assert loop.provider.generation.max_tokens == 2222
    assert loop.provider.generation.temperature == 0.7
    assert loop.provider.generation.reasoning_effort == "high"
    assert loop.memory_consolidator.model == "reloaded-model"
    assert loop.memory_consolidator.context_window_tokens == 8192
    assert loop.channels_config.send_progress is False
    assert loop.channels_config.send_tool_hints is True
    loop.subagents.apply_runtime_config.assert_called_once_with(
        model="reloaded-model",
        brave_api_key="demo-key",
        web_proxy="http://127.0.0.1:7890",
        web_search_provider="searxng",
        web_search_base_url="https://search.example.com",
        web_search_max_results=7,
        exec_config=loop.exec_config,
        restrict_to_workspace=True,
    )
    assert exec_tool.timeout == 45
    assert exec_tool.path_append == "/usr/local/bin"
    assert exec_tool.restrict_to_workspace is True
    assert web_search_tool._init_provider == "searxng"
    assert web_search_tool._init_api_key == "demo-key"
    assert web_search_tool._init_base_url == "https://search.example.com"
    assert web_search_tool.max_results == 7
    assert web_search_tool.proxy == "http://127.0.0.1:7890"
    assert web_fetch_tool.proxy == "http://127.0.0.1:7890"
    assert read_tool._allowed_dir == tmp_path
