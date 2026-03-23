"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot import __version__
from nanobot.agent.context import ContextBuilder
from nanobot.agent.i18n import (
    DEFAULT_LANGUAGE,
    help_lines,
    language_label,
    list_languages,
    normalize_language_code,
    resolve_language,
    text,
)
from nanobot.agent.memory import MemoryConsolidator
from nanobot.agent.personas import build_persona_voice_instructions, load_persona_voice_settings
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.providers.speech import OpenAISpeechProvider
from nanobot.session.manager import Session, SessionManager
from nanobot.utils.helpers import build_status_content, ensure_dir, safe_filename

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, ExecToolConfig
    from nanobot.cron.service import CronService


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _TOOL_RESULT_MAX_CHARS = 16_000
    _CLAWHUB_TIMEOUT_SECONDS = 60
    _CLAWHUB_INSTALL_TIMEOUT_SECONDS = 180
    _CLAWHUB_NETWORK_ERROR_MARKERS = (
        "eai_again",
        "enotfound",
        "etimedout",
        "econnrefused",
        "econnreset",
        "fetch failed",
        "network request failed",
        "registry.npmjs.org",
    )
    _CLAWHUB_NPM_CACHE_DIR = Path(tempfile.gettempdir()) / "nanobot-npm-cache"
    _PREFLIGHT_CONSOLIDATION_BUDGET_SECONDS = 1.5

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        config_path: Path | None = None,
        model: str | None = None,
        max_iterations: int = 40,
        context_window_tokens: int = 65_536,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        web_search_provider: str = "brave",
        web_search_base_url: str | None = None,
        web_search_max_results: int = 5,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
    ):
        from nanobot.config.schema import ExecToolConfig
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.config_path = config_path
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.context_window_tokens = context_window_tokens
        self.brave_api_key = brave_api_key
        self.web_proxy = web_proxy
        self.web_search_provider = web_search_provider
        self.web_search_base_url = web_search_base_url
        self.web_search_max_results = web_search_max_results
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            brave_api_key=brave_api_key,
            web_proxy=web_proxy,
            web_search_provider=web_search_provider,
            web_search_base_url=web_search_base_url,
            web_search_max_results=web_search_max_results,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._runtime_config_mtime_ns = (
            config_path.stat().st_mtime_ns if config_path and config_path.exists() else None
        )
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: set[asyncio.Task] = set()
        self._token_consolidation_tasks: dict[str, asyncio.Task[None]] = {}
        self._processing_lock = asyncio.Lock()
        self.memory_consolidator = MemoryConsolidator(
            workspace=workspace,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
        )
        self._register_default_tools()

    @staticmethod
    def _command_name(content: str) -> str:
        """Return the normalized slash command name."""
        parts = content.strip().split(None, 1)
        return parts[0].lower() if parts else ""

    def _get_session_persona(self, session: Session) -> str:
        """Return the active persona name for a session."""
        return self.context.resolve_persona(session.metadata.get("persona"))

    def _get_session_language(self, session: Session) -> str:
        """Return the active language for a session."""
        metadata = getattr(session, "metadata", {})
        raw = metadata.get("language") if isinstance(metadata, dict) else DEFAULT_LANGUAGE
        return resolve_language(raw)

    def _set_session_persona(self, session: Session, persona: str) -> None:
        """Persist the selected persona for a session."""
        if persona == "default":
            session.metadata.pop("persona", None)
        else:
            session.metadata["persona"] = persona

    def _set_session_language(self, session: Session, language: str) -> None:
        """Persist the selected language for a session."""
        if language == DEFAULT_LANGUAGE:
            session.metadata.pop("language", None)
        else:
            session.metadata["language"] = language

    def _persona_usage(self, language: str) -> str:
        """Return persona command help text."""
        return "\n".join([
            text(language, "cmd_persona_current"),
            text(language, "cmd_persona_list"),
            text(language, "cmd_persona_set"),
        ])

    def _language_usage(self, language: str) -> str:
        """Return language command help text."""
        return "\n".join([
            text(language, "cmd_lang_current"),
            text(language, "cmd_lang_list"),
            text(language, "cmd_lang_set"),
        ])

    def _mcp_usage(self, language: str) -> str:
        """Return MCP command help text."""
        return text(language, "mcp_usage")

    def _group_mcp_tool_names(self) -> dict[str, list[str]]:
        """Group registered MCP tool names by configured server name."""
        grouped = {name: [] for name in self._mcp_servers}
        server_names = sorted(self._mcp_servers, key=len, reverse=True)

        for tool_name in self.tools.tool_names:
            if not tool_name.startswith("mcp_"):
                continue

            for server_name in server_names:
                prefix = f"mcp_{server_name}_"
                if tool_name.startswith(prefix):
                    grouped[server_name].append(tool_name.removeprefix(prefix))
                    break

        return {name: sorted(tools) for name, tools in grouped.items()}

    def _remove_registered_mcp_tools(self) -> None:
        """Remove all dynamically registered MCP tools from the registry."""
        for tool_name in list(self.tools.tool_names):
            if tool_name.startswith("mcp_"):
                self.tools.unregister(tool_name)

    @staticmethod
    def _dump_mcp_servers(servers: dict) -> dict:
        """Normalize MCP server config for value-based comparisons."""
        dumped = {}
        for name, cfg in servers.items():
            dumped[name] = cfg.model_dump() if hasattr(cfg, "model_dump") else cfg
        return dumped

    async def _reset_mcp_connections(self) -> None:
        """Drop MCP tool registrations and close active MCP connections."""
        self._remove_registered_mcp_tools()
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass
            self._mcp_stack = None
        self._mcp_connected = False
        self._mcp_connecting = False

    def _apply_runtime_tool_config(self) -> None:
        """Apply runtime-configurable settings to already-registered tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None

        if read_tool := self.tools.get("read_file"):
            read_tool._workspace = self.workspace
            read_tool._allowed_dir = allowed_dir
            read_tool._extra_allowed_dirs = extra_read

        for name in ("write_file", "edit_file", "list_dir"):
            if tool := self.tools.get(name):
                tool._workspace = self.workspace
                tool._allowed_dir = allowed_dir
                tool._extra_allowed_dirs = None

        if exec_tool := self.tools.get("exec"):
            exec_tool.timeout = self.exec_config.timeout
            exec_tool.working_dir = str(self.workspace)
            exec_tool.restrict_to_workspace = self.restrict_to_workspace
            exec_tool.path_append = self.exec_config.path_append

        if web_search_tool := self.tools.get("web_search"):
            web_search_tool._init_provider = self.web_search_provider
            web_search_tool._init_api_key = self.brave_api_key
            web_search_tool._init_base_url = self.web_search_base_url
            web_search_tool.max_results = self.web_search_max_results
            web_search_tool.proxy = self.web_proxy

        if web_fetch_tool := self.tools.get("web_fetch"):
            web_fetch_tool.proxy = self.web_proxy

    def _apply_runtime_config(self, config) -> bool:
        """Apply hot-reloadable config to the current agent instance."""
        from nanobot.providers.base import GenerationSettings

        defaults = config.agents.defaults
        tools_cfg = config.tools
        web_cfg = tools_cfg.web
        search_cfg = web_cfg.search

        self.model = defaults.model
        self.max_iterations = defaults.max_tool_iterations
        self.context_window_tokens = defaults.context_window_tokens
        self.exec_config = tools_cfg.exec
        self.restrict_to_workspace = tools_cfg.restrict_to_workspace
        self.brave_api_key = search_cfg.api_key or None
        self.web_proxy = web_cfg.proxy or None
        self.web_search_provider = search_cfg.provider
        self.web_search_base_url = search_cfg.base_url or None
        self.web_search_max_results = search_cfg.max_results
        self.channels_config = config.channels

        self.provider.generation = GenerationSettings(
            temperature=defaults.temperature,
            max_tokens=defaults.max_tokens,
            reasoning_effort=defaults.reasoning_effort,
        )
        if hasattr(self.provider, "default_model"):
            self.provider.default_model = self.model
        self.memory_consolidator.model = self.model
        self.memory_consolidator.context_window_tokens = self.context_window_tokens
        self.subagents.apply_runtime_config(
            model=self.model,
            brave_api_key=self.brave_api_key,
            web_proxy=self.web_proxy,
            web_search_provider=self.web_search_provider,
            web_search_base_url=self.web_search_base_url,
            web_search_max_results=self.web_search_max_results,
            exec_config=self.exec_config,
            restrict_to_workspace=self.restrict_to_workspace,
        )
        self._apply_runtime_tool_config()

        mcp_changed = self._dump_mcp_servers(config.tools.mcp_servers) != self._dump_mcp_servers(
            self._mcp_servers
        )
        self._mcp_servers = config.tools.mcp_servers
        return mcp_changed

    async def _reload_runtime_config_if_needed(self, *, force: bool = False) -> None:
        """Reload hot-reloadable config from the active config file when it changes."""
        if self.config_path is None:
            return

        try:
            mtime_ns = self.config_path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime_ns = None

        if not force and mtime_ns == self._runtime_config_mtime_ns:
            return

        self._runtime_config_mtime_ns = mtime_ns

        from nanobot.config.loader import load_config

        if mtime_ns is None:
            await self._reset_mcp_connections()
            self._mcp_servers = {}
            return

        reloaded = load_config(self.config_path)
        if self._apply_runtime_config(reloaded):
            await self._reset_mcp_connections()

    async def _reload_mcp_servers_if_needed(self, *, force: bool = False) -> None:
        """Backward-compatible wrapper for runtime config reloads."""
        await self._reload_runtime_config_if_needed(force=force)

    @staticmethod
    def _decode_subprocess_output(data: bytes) -> str:
        """Decode subprocess output conservatively for CLI surfacing."""
        return data.decode("utf-8", errors="replace").strip()

    @classmethod
    def _is_clawhub_network_error(cls, output: str) -> bool:
        lowered = output.lower()
        return any(marker in lowered for marker in cls._CLAWHUB_NETWORK_ERROR_MARKERS)

    def _format_clawhub_error(self, language: str, code: int, output: str) -> str:
        if output and self._is_clawhub_network_error(output):
            return "\n\n".join([text(language, "skill_command_network_failed"), output])
        return output or text(language, "skill_command_failed", code=code)

    def _clawhub_env(self) -> dict[str, str]:
        """Configure npm so ClawHub fails fast and uses a writable cache directory."""
        env = os.environ.copy()
        env.setdefault("NO_COLOR", "1")
        env.setdefault("FORCE_COLOR", "0")
        env.setdefault("npm_config_cache", str(self._CLAWHUB_NPM_CACHE_DIR))
        env.setdefault("npm_config_update_notifier", "false")
        env.setdefault("npm_config_audit", "false")
        env.setdefault("npm_config_fund", "false")
        env.setdefault("npm_config_fetch_retries", "0")
        env.setdefault("npm_config_fetch_timeout", "5000")
        env.setdefault("npm_config_fetch_retry_mintimeout", "1000")
        env.setdefault("npm_config_fetch_retry_maxtimeout", "5000")
        return env

    async def _run_clawhub(
        self, language: str, *args: str, timeout_seconds: int | None = None,
    ) -> tuple[int, str]:
        """Run the ClawHub CLI and return (exit_code, combined_output)."""
        npx = shutil.which("npx")
        if not npx:
            return 127, text(language, "skill_npx_missing")

        env = self._clawhub_env()

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                npx,
                "--yes",
                "clawhub@latest",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds or self._CLAWHUB_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            return 127, text(language, "skill_npx_missing")
        except asyncio.TimeoutError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.communicate()
            return 124, text(language, "skill_command_timeout")
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.communicate()
            raise

        output_parts = [
            self._decode_subprocess_output(stdout),
            self._decode_subprocess_output(stderr),
        ]
        output = "\n".join(part for part in output_parts if part).strip()
        return proc.returncode or 0, output

    async def _handle_skill_command(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        """Handle ClawHub skill management commands for the active workspace."""
        language = self._get_session_language(session)
        parts = msg.content.strip().split()
        search_query: str | None = None
        if len(parts) == 1:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=text(language, "skill_usage"),
            )

        subcommand = parts[1].lower()
        workspace = str(self.workspace)

        if subcommand == "search":
            query_parts = msg.content.strip().split(None, 2)
            if len(query_parts) < 3 or not query_parts[2].strip():
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=text(language, "skill_search_missing_query"),
                )
            search_query = query_parts[2].strip()
            code, output = await self._run_clawhub(
                language,
                "search",
                search_query,
                "--limit",
                "5",
            )
        elif subcommand == "install":
            if len(parts) < 3:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=text(language, "skill_install_missing_slug"),
                )
            code, output = await self._run_clawhub(
                language,
                "install",
                parts[2],
                "--workdir",
                workspace,
                timeout_seconds=self._CLAWHUB_INSTALL_TIMEOUT_SECONDS,
            )
        elif subcommand == "uninstall":
            if len(parts) < 3:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=text(language, "skill_uninstall_missing_slug"),
                )
            code, output = await self._run_clawhub(
                language,
                "uninstall",
                parts[2],
                "--yes",
                "--workdir",
                workspace,
            )
        elif subcommand == "list":
            code, output = await self._run_clawhub(language, "list", "--workdir", workspace)
        elif subcommand == "update":
            code, output = await self._run_clawhub(
                language,
                "update",
                "--all",
                "--workdir",
                workspace,
                timeout_seconds=self._CLAWHUB_INSTALL_TIMEOUT_SECONDS,
            )
        else:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=text(language, "skill_usage"),
            )

        if code != 0:
            content = self._format_clawhub_error(language, code, output)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)

        if subcommand == "search" and not output:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=text(language, "skill_search_no_results", query=search_query or ""),
            )

        notes: list[str] = []
        if output:
            notes.append(output)
        if subcommand in {"install", "uninstall", "update"}:
            notes.append(text(language, "skill_applied_to_workspace", workspace=workspace))
        content = "\n\n".join(notes) if notes else text(language, "skill_command_completed", command=subcommand)
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)

    async def _handle_mcp_command(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        """Handle MCP inspection commands."""
        language = self._get_session_language(session)
        parts = msg.content.strip().split()

        if len(parts) > 1 and parts[1].lower() != "list":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self._mcp_usage(language),
            )

        await self._reload_mcp_servers_if_needed()

        if not self._mcp_servers:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=text(language, "mcp_no_servers"),
            )

        await self._connect_mcp()

        server_lines = "\n".join(f"- {name}" for name in self._mcp_servers)
        sections = [text(language, "mcp_servers_list", items=server_lines)]

        grouped_tools = self._group_mcp_tool_names()
        tool_lines = "\n".join(
            f"- {server}: {', '.join(tools)}"
            for server, tools in grouped_tools.items()
            if tools
        )
        sections.append(
            text(language, "mcp_tools_list", items=tool_lines)
            if tool_lines
            else text(language, "mcp_no_tools")
        )

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="\n\n".join(sections),
        )

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None
        self.tools.register(ReadFileTool(workspace=self.workspace, allowed_dir=allowed_dir, extra_allowed_dirs=extra_read))
        for cls in (WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        if self.exec_config.enable:
            self.tools.register(ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
                path_append=self.exec_config.path_append,
            ))
        self.tools.register(
            WebSearchTool(
                provider=self.web_search_provider,
                api_key=self.brave_api_key,
                base_url=self.web_search_base_url,
                max_results=self.web_search_max_results,
                proxy=self.web_proxy,
            )
        )
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        await self._reload_mcp_servers_if_needed()
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except BaseException as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        from nanobot.utils.helpers import strip_think
        return strip_think(text) or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    def _status_response(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        """Build an outbound status message for a session."""
        ctx_est = 0
        try:
            ctx_est, _ = self.memory_consolidator.estimate_session_prompt_tokens(session)
        except Exception:
            pass
        if ctx_est <= 0:
            ctx_est = self._last_usage.get("prompt_tokens", 0)
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=build_status_content(
                version=__version__, model=self.model,
                start_time=self._start_time, last_usage=self._last_usage,
                context_window_tokens=self.context_window_tokens,
                session_msg_count=len(session.get_history(max_messages=0)),
                context_tokens_estimate=ctx_est,
            ),
            metadata={"render_as": "text"},
        )

    @staticmethod
    def _voice_reply_extension(response_format: str) -> str:
        """Map TTS response formats to delivery file extensions."""
        return {
            "opus": ".ogg",
            "mp3": ".mp3",
            "aac": ".aac",
            "flac": ".flac",
            "wav": ".wav",
            "pcm": ".pcm",
            "silk": ".silk",
        }.get(response_format, f".{response_format}")

    @staticmethod
    def _channel_base_name(channel: str) -> str:
        """Normalize multi-instance channel routes such as telegram/main."""
        return channel.split("/", 1)[0].lower()

    def _voice_reply_enabled_for_channel(self, channel: str) -> bool:
        """Return True when voice replies are enabled for the given channel."""
        cfg = getattr(self.channels_config, "voice_reply", None)
        if not cfg or not getattr(cfg, "enabled", False):
            return False
        route_name = channel.lower()
        base_name = self._channel_base_name(channel)
        enabled_channels = {
            name.lower() for name in getattr(cfg, "channels", []) if isinstance(name, str)
        }
        if route_name not in enabled_channels and base_name not in enabled_channels:
            return False
        if base_name == "qq":
            return getattr(cfg, "response_format", "opus") == "silk"
        return base_name in {"telegram", "qq"}

    def _voice_reply_profile(
        self,
        persona: str | None,
    ) -> tuple[str, str | None, float | None]:
        """Resolve voice, instructions, and speed for the active persona."""
        cfg = getattr(self.channels_config, "voice_reply", None)
        persona_voice = load_persona_voice_settings(self.workspace, persona)

        extra_instructions = [
            value.strip()
            for value in (
                getattr(cfg, "instructions", "") if cfg is not None else "",
                persona_voice.instructions or "",
            )
            if isinstance(value, str) and value.strip()
        ]
        instructions = build_persona_voice_instructions(
            self.workspace,
            persona,
            extra_instructions=" ".join(extra_instructions) if extra_instructions else None,
        )
        voice = persona_voice.voice or getattr(cfg, "voice", "alloy")
        speed = (
            persona_voice.speed
            if persona_voice.speed is not None
            else getattr(cfg, "speed", None) if cfg is not None else None
        )
        return voice, instructions, speed

    async def _maybe_attach_voice_reply(
        self,
        outbound: OutboundMessage | None,
        *,
        persona: str | None = None,
    ) -> OutboundMessage | None:
        """Optionally synthesize the final text reply into a voice attachment."""
        if (
            outbound is None
            or not outbound.content
            or not self._voice_reply_enabled_for_channel(outbound.channel)
        ):
            return outbound

        cfg = getattr(self.channels_config, "voice_reply", None)
        if cfg is None:
            return outbound

        api_key = (getattr(cfg, "api_key", "") or getattr(self.provider, "api_key", "") or "").strip()
        if not api_key:
            logger.warning(
                "Voice reply enabled for {}, but no TTS api_key is configured",
                outbound.channel,
            )
            return outbound

        api_base = (
            getattr(cfg, "api_base", "")
            or getattr(self.provider, "api_base", "")
            or "https://api.openai.com/v1"
        ).strip()
        response_format = getattr(cfg, "response_format", "opus")
        model = getattr(cfg, "model", "gpt-4o-mini-tts")
        voice, instructions, speed = self._voice_reply_profile(persona)
        media_dir = ensure_dir(self.workspace / "out" / "voice")
        filename = safe_filename(
            f"{outbound.channel}_{outbound.chat_id}_{int(time.time() * 1000)}"
        ) + self._voice_reply_extension(response_format)
        output_path = media_dir / filename

        try:
            provider = OpenAISpeechProvider(api_key=api_key, api_base=api_base)
            await provider.synthesize_to_file(
                outbound.content,
                model=model,
                voice=voice,
                instructions=instructions,
                speed=speed,
                response_format=response_format,
                output_path=output_path,
            )
        except Exception:
            logger.exception(
                "Failed to synthesize voice reply for {}:{}",
                outbound.channel,
                outbound.chat_id,
            )
            return outbound

        return OutboundMessage(
            channel=outbound.channel,
            chat_id=outbound.chat_id,
            content=outbound.content,
            reply_to=outbound.reply_to,
            media=[*(outbound.media or []), str(output_path)],
            metadata=dict(outbound.metadata or {}),
        )

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming)*: called when a streaming session finishes.
        ``resuming=True`` means tool calls follow (spinner should restart);
        ``resuming=False`` means this is the final response.
        """
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []

        # Wrap on_stream with stateful think-tag filter so downstream
        # consumers (CLI, channels) never see <think> blocks.
        _raw_stream = on_stream
        _stream_buf = ""

        async def _filtered_stream(delta: str) -> None:
            nonlocal _stream_buf
            from nanobot.utils.helpers import strip_think
            prev_clean = strip_think(_stream_buf)
            _stream_buf += delta
            new_clean = strip_think(_stream_buf)
            incremental = new_clean[len(prev_clean):]
            if incremental and _raw_stream:
                await _raw_stream(incremental)

        while iteration < self.max_iterations:
            iteration += 1

            tool_defs = self.tools.get_definitions()

            if on_stream:
                response = await self.provider.chat_stream_with_retry(
                    messages=messages,
                    tools=tool_defs,
                    model=self.model,
                    on_content_delta=_filtered_stream,
                )
            else:
                response = await self.provider.chat_with_retry(
                    messages=messages,
                    tools=tool_defs,
                    model=self.model,
                )

            usage = getattr(response, "usage", None) or {}
            self._last_usage = {
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            }

            if response.has_tool_calls:
                if on_stream and on_stream_end:
                    await on_stream_end(resuming=True)
                    _stream_buf = ""

                if on_progress:
                    if not on_stream:
                        thought = self._strip_think(response.content)
                        if thought:
                            await on_progress(thought)
                    tool_hint = self._tool_hint(response.tool_calls)
                    tool_hint = self._strip_think(tool_hint)
                    await on_progress(tool_hint, tool_hint=True)

                tool_call_dicts = [
                    tc.to_openai_tool_call()
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                if on_stream and on_stream_end:
                    await on_stream_end(resuming=False)
                    _stream_buf = ""

                clean = self._strip_think(response.content)
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    break
                messages = self.context.add_assistant_message(
                    messages, clean, reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                final_content = clean
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        return final_content, tools_used, messages

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            cmd = self._command_name(msg.content)
            if cmd == "/stop":
                await self._handle_stop(msg)
            elif cmd == "/restart":
                await self._handle_restart(msg)
            elif cmd == "/status":
                session = self.sessions.get_or_create(msg.session_key)
                await self.bus.publish_outbound(self._status_response(msg, session))
            else:
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(lambda t, k=msg.session_key: self._active_tasks.get(k, []) and self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None)

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        total = cancelled + sub_cancelled
        session = self.sessions.get_or_create(msg.session_key)
        language = self._get_session_language(session)
        content = text(language, "stopped_tasks", count=total) if total else text(language, "no_active_task")
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    async def _handle_restart(self, msg: InboundMessage) -> None:
        """Restart the process in-place via os.execv."""
        session = self.sessions.get_or_create(msg.session_key)
        language = self._get_session_language(session)
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=text(language, "restarting"),
        ))

        async def _do_restart():
            await asyncio.sleep(1)
            # Use -m nanobot instead of sys.argv[0] for Windows compatibility
            # (sys.argv[0] may be just "nanobot" without full path on Windows)
            os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

        asyncio.create_task(_do_restart())

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message under the global lock."""
        async with self._processing_lock:
            try:
                on_stream = on_stream_end = None
                if msg.metadata.get("_wants_stream"):
                    async def on_stream(delta: str) -> None:
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content=delta, metadata={"_stream_delta": True},
                        ))

                    async def on_stream_end(*, resuming: bool = False) -> None:
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="", metadata={"_stream_end": True, "_resuming": resuming},
                        ))

                response = await self._process_message(
                    msg, on_stream=on_stream, on_stream_end=on_stream_end,
                )
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content=text(self._get_session_language(self.sessions.get_or_create(msg.session_key)), "generic_error"),
                ))

    async def _handle_language_command(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        """Handle session-scoped language switching commands."""
        current = self._get_session_language(session)
        parts = msg.content.strip().split()
        if len(parts) == 1 or parts[1].lower() == "current":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=text(current, "current_language", language_name=language_label(current, current)),
            )

        subcommand = parts[1].lower()
        if subcommand == "list":
            items = "\n".join(
                f"- {language_label(code, current)}"
                + (f" ({text(current, 'current_marker')})" if code == current else "")
                for code in list_languages()
            )
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=text(current, "available_languages", items=items),
            )

        if subcommand != "set" or len(parts) < 3:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self._language_usage(current),
            )

        target = normalize_language_code(parts[2])
        if target is None:
            languages = ", ".join(language_label(code, current) for code in list_languages())
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=text(current, "unknown_language", name=parts[2], languages=languages),
            )

        if target == current:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=text(current, "language_already_active", language_name=language_label(target, current)),
            )

        self._set_session_language(session, target)
        self.sessions.save(session)
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=text(target, "switched_language", language_name=language_label(target, target)),
        )

    async def _handle_persona_command(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        """Handle session-scoped persona management commands."""
        language = self._get_session_language(session)
        parts = msg.content.strip().split()
        if len(parts) == 1 or parts[1].lower() == "current":
            current = self._get_session_persona(session)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=text(language, "current_persona", persona=current),
            )

        subcommand = parts[1].lower()
        if subcommand == "list":
            current = self._get_session_persona(session)
            marker = text(language, "current_marker")
            personas = [
                f"{name} ({marker})" if name == current else name
                for name in self.context.list_personas()
            ]
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=text(language, "available_personas", items="\n".join(f"- {name}" for name in personas)),
            )

        if subcommand != "set" or len(parts) < 3:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self._persona_usage(language),
            )

        target = self.context.find_persona(parts[2])
        if target is None:
            personas = ", ".join(self.context.list_personas())
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=text(
                    language,
                    "unknown_persona",
                    name=parts[2],
                    personas=personas,
                    path=self.workspace / "personas" / parts[2],
                ),
            )

        current = self._get_session_persona(session)
        if target == current:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=text(language, "persona_already_active", persona=target),
            )

        try:
            if not await self.memory_consolidator.archive_unconsolidated(session):
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=text(language, "memory_archival_failed_persona"),
                )
        except Exception:
            logger.exception("/persona archival failed for {}", session.key)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=text(language, "memory_archival_failed_persona"),
            )

        session.clear()
        self._set_session_persona(session, target)
        self.sessions.save(session)
        self.sessions.invalidate(session.key)
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=text(language, "switched_persona", persona=target),
        )

    async def close_mcp(self) -> None:
        """Drain pending background archives, then close MCP connections."""
        if self._background_tasks:
            await asyncio.gather(*list(self._background_tasks), return_exceptions=True)
            self._background_tasks.clear()
        self._token_consolidation_tasks.clear()
        await self._reset_mcp_connections()

    def _track_background_task(self, task: asyncio.Task) -> asyncio.Task:
        """Track a background task until completion."""
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _schedule_background(self, coro) -> asyncio.Task:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        return self._track_background_task(task)

    def _ensure_background_token_consolidation(self, session: Session) -> asyncio.Task[None]:
        """Ensure at most one token-consolidation task runs per session."""
        existing = self._token_consolidation_tasks.get(session.key)
        if existing and not existing.done():
            return existing

        task = asyncio.create_task(self.memory_consolidator.maybe_consolidate_by_tokens(session))
        self._token_consolidation_tasks[session.key] = task
        self._track_background_task(task)

        def _cleanup(done: asyncio.Task[None]) -> None:
            if self._token_consolidation_tasks.get(session.key) is done:
                self._token_consolidation_tasks.pop(session.key, None)

        task.add_done_callback(_cleanup)
        return task

    async def _run_preflight_token_consolidation(self, session: Session) -> None:
        """Give token consolidation a short head start, then continue in background if needed."""
        task = self._ensure_background_token_consolidation(session)
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._PREFLIGHT_CONSOLIDATION_BUDGET_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Token consolidation still running for {} after {:.1f}s; continuing in background",
                session.key,
                self._PREFLIGHT_CONSOLIDATION_BUDGET_SECONDS,
            )
        except Exception:
            logger.exception("Preflight token consolidation failed for {}", session.key)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        await self._reload_runtime_config_if_needed()

        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            persona = self._get_session_persona(session)
            language = self._get_session_language(session)
            await self._connect_mcp()
            await self._run_preflight_token_consolidation(session)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = session.get_history(max_messages=0)
            current_role = "assistant" if msg.sender_id == "subagent" else "user"
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content,
                channel=channel,
                chat_id=chat_id,
                persona=persona,
                language=language,
                current_role=current_role,
            )
            final_content, _, all_msgs = await self._run_agent_loop(messages)
            self._save_turn(session, all_msgs, 1 + len(history))
            self.sessions.save(session)
            self._ensure_background_token_consolidation(session)
            return await self._maybe_attach_voice_reply(
                OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content=final_content or "Background task completed.",
                ),
                persona=persona,
            )

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)
        persona = self._get_session_persona(session)
        language = self._get_session_language(session)

        # Slash commands
        cmd = self._command_name(msg.content)
        if cmd == "/new":
            snapshot = session.messages[session.last_consolidated:]
            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)

            if snapshot:
                self._schedule_background(self.memory_consolidator.archive_messages(session, snapshot))

            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content=text(language, "new_session_started"))
        if cmd == "/status":
            return self._status_response(msg, session)
        if cmd in {"/lang", "/language"}:
            return await self._handle_language_command(msg, session)
        if cmd == "/persona":
            return await self._handle_persona_command(msg, session)
        if cmd == "/skill":
            return await self._handle_skill_command(msg, session)
        if cmd == "/mcp":
            return await self._handle_mcp_command(msg, session)
        if cmd == "/help":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="\n".join(help_lines(language)),
                metadata={"render_as": "text"},
            )
        await self._connect_mcp()
        await self._run_preflight_token_consolidation(session)

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=0)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
            persona=persona,
            language=language,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        final_content, _, all_msgs = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        self._save_turn(session, all_msgs, 1 + len(history))
        self.sessions.save(session)
        self._ensure_background_token_consolidation(session)

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        outbound = await self._maybe_attach_voice_reply(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=final_content,
                metadata=msg.metadata or {},
            ),
            persona=persona,
        )
        if outbound is None:
            return None

        meta = dict(outbound.metadata or {})
        content = outbound.content
        if on_stream is not None:
            if outbound.media:
                content = ""
            else:
                meta["_streamed"] = True
        return OutboundMessage(
            channel=outbound.channel,
            chat_id=outbound.chat_id,
            content=content,
            reply_to=outbound.reply_to,
            media=list(outbound.media or []),
            metadata=meta,
        )

    @staticmethod
    def _image_placeholder(block: dict[str, Any]) -> dict[str, str]:
        """Convert an inline image block into a compact text placeholder."""
        path = (block.get("_meta") or {}).get("path", "")
        return {"type": "text", "text": f"[image: {path}]" if path else "[image]"}

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip volatile multimodal payloads before writing session history."""
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            if (
                block.get("type") == "image_url"
                and block.get("image_url", {}).get("url", "").startswith("data:image/")
            ):
                filtered.append(self._image_placeholder(block))
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if truncate_text and len(text) > self._TOOL_RESULT_MAX_CHARS:
                    text = text[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool":
                if isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                    entry["content"] = content[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
                elif isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, truncate_text=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # Strip the runtime-context prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, drop_runtime=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload."""
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        return await self._process_message(
            msg, session_key=session_key, on_progress=on_progress,
            on_stream=on_stream, on_stream_end=on_stream_end,
        )
