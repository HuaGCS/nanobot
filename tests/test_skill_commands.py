"""Tests for /skill slash command integration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.bus.events import InboundMessage


def _make_loop(workspace: Path):
    """Create an AgentLoop with a real workspace and lightweight mocks."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    with patch("nanobot.agent.loop.SubagentManager"):
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace)
    return loop


class _FakeProcess:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self._stdout = stdout.encode("utf-8")
        self._stderr = stderr.encode("utf-8")
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True


@pytest.mark.asyncio
async def test_skill_search_runs_clawhub_search(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    proc = _FakeProcess(stdout="skill-a\nskill-b")
    create_proc = AsyncMock(return_value=proc)

    with patch("nanobot.agent.loop.shutil.which", return_value="/usr/bin/npx"), \
         patch("nanobot.agent.loop.asyncio.create_subprocess_exec", create_proc):
        response = await loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/skill search web scraping")
        )

    assert response is not None
    assert response.content == "skill-a\nskill-b"
    assert create_proc.await_count == 1
    args = create_proc.await_args.args
    assert args == (
        "/usr/bin/npx",
        "--yes",
        "clawhub@latest",
        "search",
        "web scraping",
        "--limit",
        "5",
    )
    env = create_proc.await_args.kwargs["env"]
    assert env["npm_config_cache"].endswith("nanobot-npm-cache")
    assert env["npm_config_fetch_retries"] == "0"
    assert env["npm_config_fetch_timeout"] == "5000"


@pytest.mark.asyncio
async def test_skill_search_surfaces_npm_network_errors(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    proc = _FakeProcess(
        returncode=1,
        stderr=(
            "npm error code EAI_AGAIN\n"
            "npm error request to https://registry.npmjs.org/clawhub failed"
        ),
    )
    create_proc = AsyncMock(return_value=proc)

    with patch("nanobot.agent.loop.shutil.which", return_value="/usr/bin/npx"), \
         patch("nanobot.agent.loop.asyncio.create_subprocess_exec", create_proc):
        response = await loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/skill search test")
        )

    assert response is not None
    assert "could not reach the npm registry" in response.content
    assert "EAI_AGAIN" in response.content


@pytest.mark.asyncio
async def test_skill_search_empty_output_returns_no_results(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    proc = _FakeProcess(stdout="")
    create_proc = AsyncMock(return_value=proc)

    with patch("nanobot.agent.loop.shutil.which", return_value="/usr/bin/npx"), \
         patch("nanobot.agent.loop.asyncio.create_subprocess_exec", create_proc):
        response = await loop._process_message(
            InboundMessage(
                channel="cli",
                sender_id="user",
                chat_id="direct",
                content="/skill search selfimprovingagent",
            )
        )

    assert response is not None
    assert 'No skills found for "selfimprovingagent"' in response.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected_args", "expected_output"),
    [
        (
            "/skill install demo-skill",
            ("install", "demo-skill"),
            "Installed demo-skill",
        ),
        (
            "/skill uninstall demo-skill",
            ("uninstall", "demo-skill", "--yes"),
            "Uninstalled demo-skill",
        ),
        (
            "/skill list",
            ("list",),
            "demo-skill",
        ),
        (
            "/skill update",
            ("update", "--all"),
            "Updated 1 skill",
        ),
    ],
)
async def test_skill_commands_use_active_workspace(
    tmp_path: Path, command: str, expected_args: tuple[str, ...], expected_output: str,
) -> None:
    loop = _make_loop(tmp_path)
    proc = _FakeProcess(stdout=expected_output)
    create_proc = AsyncMock(return_value=proc)

    with patch("nanobot.agent.loop.shutil.which", return_value="/usr/bin/npx"), \
         patch("nanobot.agent.loop.asyncio.create_subprocess_exec", create_proc):
        response = await loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content=command)
        )

    assert response is not None
    assert expected_output in response.content
    args = create_proc.await_args.args
    assert args[:3] == ("/usr/bin/npx", "--yes", "clawhub@latest")
    assert args[3:] == (*expected_args, "--workdir", str(tmp_path))
    if command != "/skill list":
        assert f"Applied to workspace: {tmp_path}" in response.content


@pytest.mark.asyncio
async def test_skill_help_includes_skill_command(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/help")
    )

    assert response is not None
    assert "/skill <search|install|uninstall|list|update>" in response.content


@pytest.mark.asyncio
async def test_skill_missing_npx_returns_guidance(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)

    with patch("nanobot.agent.loop.shutil.which", return_value=None):
        response = await loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/skill list")
        )

    assert response is not None
    assert "npx is not installed" in response.content


@pytest.mark.asyncio
async def test_skill_usage_errors_are_user_facing(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)

    usage = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/skill")
    )
    missing_slug = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/skill install")
    )
    missing_uninstall_slug = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/skill uninstall")
    )

    assert usage is not None
    assert "/skill search <query>" in usage.content
    assert missing_slug is not None
    assert "Missing skill slug" in missing_slug.content
    assert missing_uninstall_slug is not None
    assert "/skill uninstall <slug>" in missing_uninstall_slug.content
