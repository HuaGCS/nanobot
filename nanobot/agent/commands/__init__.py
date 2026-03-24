"""Command handlers for AgentLoop slash commands."""

from nanobot.agent.commands.language import LanguageCommandHandler
from nanobot.agent.commands.mcp import MCPCommandHandler
from nanobot.agent.commands.persona import PersonaCommandHandler
from nanobot.agent.commands.router import build_agent_command_router
from nanobot.agent.commands.skill import SkillCommandHandler
from nanobot.agent.commands.system import SystemCommandHandler

__all__ = [
    "LanguageCommandHandler",
    "MCPCommandHandler",
    "PersonaCommandHandler",
    "SkillCommandHandler",
    "SystemCommandHandler",
    "build_agent_command_router",
]
