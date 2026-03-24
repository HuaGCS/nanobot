"""AgentLoop slash-command router registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanobot.command.router import CommandContext, CommandRouter

if TYPE_CHECKING:
    from nanobot.bus.events import OutboundMessage


def _session(ctx: CommandContext):
    return ctx.session or ctx.loop.sessions.get_or_create(ctx.key)


async def _cmd_status(ctx: CommandContext) -> OutboundMessage:
    session = _session(ctx)
    return ctx.loop._system_commands.status(ctx.msg, session)


async def _cmd_new(ctx: CommandContext) -> OutboundMessage:
    session = _session(ctx)
    language = ctx.loop._get_session_language(session)
    return ctx.loop._system_commands.new_session(ctx.msg, session, language)


async def _cmd_help(ctx: CommandContext) -> OutboundMessage:
    session = _session(ctx)
    language = ctx.loop._get_session_language(session)
    return ctx.loop._system_commands.help(ctx.msg, language)


async def _cmd_lang(ctx: CommandContext):
    return await ctx.loop._handle_language_command(ctx.msg, _session(ctx))


async def _cmd_persona(ctx: CommandContext):
    return await ctx.loop._handle_persona_command(ctx.msg, _session(ctx))


async def _cmd_skill(ctx: CommandContext):
    return await ctx.loop._handle_skill_command(ctx.msg, _session(ctx))


async def _cmd_mcp(ctx: CommandContext):
    return await ctx.loop._handle_mcp_command(ctx.msg, _session(ctx))


async def _cmd_stop_priority(ctx: CommandContext):
    await ctx.loop._handle_stop(ctx.msg)
    return None


async def _cmd_restart_priority(ctx: CommandContext):
    await ctx.loop._handle_restart(ctx.msg)
    return None


def build_agent_command_router() -> CommandRouter:
    """Create the slash-command router used by AgentLoop."""
    router = CommandRouter()

    router.priority("/stop", _cmd_stop_priority)
    router.priority("/restart", _cmd_restart_priority)
    router.priority("/status", _cmd_status)

    router.exact("/new", _cmd_new)
    router.exact("/status", _cmd_status)
    router.exact("/help", _cmd_help)

    router.exact("/lang", _cmd_lang)
    router.exact("/language", _cmd_lang)
    router.prefix("/lang ", _cmd_lang)
    router.prefix("/language ", _cmd_lang)

    router.exact("/persona", _cmd_persona)
    router.prefix("/persona ", _cmd_persona)

    router.exact("/skill", _cmd_skill)
    router.prefix("/skill ", _cmd_skill)

    router.exact("/mcp", _cmd_mcp)
    router.prefix("/mcp ", _cmd_mcp)

    return router
