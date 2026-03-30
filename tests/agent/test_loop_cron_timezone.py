from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.cron import CronTool
from nanobot.bus.queue import MessageBus
from nanobot.config.loader import save_config
from nanobot.config.schema import Config
from nanobot.cron.service import CronService


def test_agent_loop_registers_cron_tool_with_configured_timezone(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        cron_service=CronService(tmp_path / "cron" / "jobs.json"),
        timezone="Asia/Shanghai",
    )

    cron_tool = loop.tools.get("cron")

    assert isinstance(cron_tool, CronTool)
    assert cron_tool._default_timezone == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_agent_loop_runtime_reload_updates_workspace_and_timezone(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    config_path = tmp_path / "config.json"
    initial_workspace = tmp_path / "workspace-a"
    reloaded_workspace = tmp_path / "workspace-b"

    config = Config().bind_config_path(config_path)
    config.agents.defaults.workspace = str(initial_workspace)
    config.agents.defaults.timezone = "UTC"
    save_config(config, config_path)

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=initial_workspace,
        config_path=config_path,
        model="test-model",
        cron_service=CronService(initial_workspace / "cron" / "jobs.json"),
        timezone="UTC",
    )

    updated = Config().bind_config_path(config_path)
    updated.agents.defaults.workspace = str(reloaded_workspace)
    updated.agents.defaults.timezone = "Asia/Shanghai"
    save_config(updated, config_path)

    await loop.reload_runtime_config(force=True)

    cron_tool = loop.tools.get("cron")

    assert loop.workspace == reloaded_workspace
    assert loop.context.workspace == reloaded_workspace
    assert loop.context.skills.workspace == reloaded_workspace
    assert loop.sessions.workspace == reloaded_workspace
    assert loop.context.timezone == "Asia/Shanghai"
    assert isinstance(cron_tool, CronTool)
    assert cron_tool._default_timezone == "Asia/Shanghai"
