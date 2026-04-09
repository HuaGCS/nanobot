"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import tempfile
import time
from contextlib import AsyncExitStack, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.commands import (
    LanguageCommandHandler,
    MCPCommandHandler,
    PersonaCommandHandler,
    SkillCommandHandler,
    SystemCommandHandler,
    build_agent_command_router,
)
from nanobot.agent.context import ContextBuilder
from nanobot.agent.hook import AgentHook, AgentHookContext, CompositeHook
from nanobot.agent.i18n import (
    DEFAULT_LANGUAGE,
    resolve_language,
    text,
)
from nanobot.agent.memory import Consolidator, Dream
from nanobot.agent.memory_backends.base import UserMemoryBackend
from nanobot.agent.memory_backends.file_backend import FileUserMemoryBackend
from nanobot.agent.memory_backends.mem0_backend import Mem0UserMemoryBackend
from nanobot.agent.memory_models import MemoryCommitRequest, MemoryScope
from nanobot.agent.memory_router import MemoryRouter
from nanobot.agent.personas import (
    build_persona_voice_instructions,
    load_persona_response_filter_tags,
    load_persona_voice_settings,
    strip_tagged_response_content,
)
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.history import HistoryExpandTool, HistorySearchTool
from nanobot.agent.tools.image_gen import ImageGenTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.search import GlobTool, GrepTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.command.router import CommandContext
from nanobot.providers.base import LLMProvider
from nanobot.providers.speech import (
    EdgeSpeechProvider,
    GPTSoVITSSpeechProvider,
    OpenAISpeechProvider,
)
from nanobot.session.manager import Session, SessionManager
from nanobot.utils.helpers import (
    ensure_dir,
    estimate_prompt_tokens_chain,
    image_placeholder_text,
    safe_filename,
)
from nanobot.utils.helpers import (
    truncate_text as truncate_text_value,
)
from nanobot.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, ExecToolConfig, ImageGenConfig, MemoryConfig
    from nanobot.cron.service import CronService


UNIFIED_SESSION_KEY = "unified:default"

class _LoopHook(AgentHook):
    """Core hook for the main loop."""

    def __init__(
        self,
        agent_loop: AgentLoop,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        *,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
    ) -> None:
        super().__init__(reraise=True)
        self._loop = agent_loop
        self._on_progress = on_progress
        self._on_stream = on_stream
        self._on_stream_end = on_stream_end
        self._channel = channel
        self._chat_id = chat_id
        self._message_id = message_id
        self._stream_buf = ""

    def wants_streaming(self) -> bool:
        return self._on_stream is not None

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        from nanobot.utils.helpers import strip_think

        prev_clean = strip_think(self._stream_buf)
        self._stream_buf += delta
        new_clean = strip_think(self._stream_buf)
        incremental = new_clean[len(prev_clean):]
        if incremental and self._on_stream:
            await self._on_stream(incremental)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        if self._on_stream_end:
            await self._on_stream_end(resuming=resuming)
        self._stream_buf = ""

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        if self._on_progress:
            if not self._on_stream:
                thought = self._loop._strip_think(
                    context.response.content if context.response else None
                )
                if thought:
                    await self._on_progress(thought)
            tool_hint = self._loop._strip_think(self._loop._tool_hint(context.tool_calls))
            await self._on_progress(tool_hint, tool_hint=True)
        for tc in context.tool_calls:
            args_str = json.dumps(tc.arguments, ensure_ascii=False)
            logger.info("Tool call: {}({})", tc.name, args_str[:200])
        self._loop._set_tool_context(self._channel, self._chat_id, self._message_id)

    async def after_iteration(self, context: AgentHookContext) -> None:
        u = context.usage or {}
        logger.debug(
            "LLM usage: prompt={} completion={} cached={}",
            u.get("prompt_tokens", 0),
            u.get("completion_tokens", 0),
            u.get("cached_tokens", 0),
        )

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        return self._loop._strip_think(content)


class _LoopHookChain(AgentHook):
    """Run the core hook before extra hooks."""

    __slots__ = ("_primary", "_extras")

    def __init__(self, primary: AgentHook, extra_hooks: list[AgentHook]) -> None:
        self._primary = primary
        self._extras = CompositeHook(extra_hooks)

    def wants_streaming(self) -> bool:
        return self._primary.wants_streaming() or self._extras.wants_streaming()

    def prepare_messages(
        self,
        context: AgentHookContext,
        tool_definitions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return self._primary.prepare_messages(context, tool_definitions)

    async def before_iteration(self, context: AgentHookContext) -> None:
        await self._primary.before_iteration(context)
        await self._extras.before_iteration(context)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        await self._primary.on_stream(context, delta)
        await self._extras.on_stream(context, delta)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        await self._primary.on_stream_end(context, resuming=resuming)
        await self._extras.on_stream_end(context, resuming=resuming)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        await self._primary.before_execute_tools(context)
        await self._extras.before_execute_tools(context)

    async def after_iteration(self, context: AgentHookContext) -> None:
        await self._primary.after_iteration(context)
        await self._extras.after_iteration(context)

    def normalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        return self._primary.normalize_content(context, content)

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        content = self._primary.finalize_content(context, content)
        return self._extras.finalize_content(context, content)


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
    _CLAWHUB_CACHE_ERROR_MARKERS = (
        "err_module_not_found",
        "cannot find module",
        "cannot find package",
    )
    _CLAWHUB_SEARCH_API_URL = "https://lightmake.site/api/skills"
    _CLAWHUB_SEARCH_TIMEOUT_SECONDS = 15.0
    _CLAWHUB_SEARCH_LIMIT = 5
    _CLAWHUB_NPM_CACHE_DIR = Path(tempfile.gettempdir()) / "nanobot-npm-cache"
    _MEMORIX_CONTEXT_MAX_CHARS = 4_000
    _PREFLIGHT_CONSOLIDATION_BUDGET_SECONDS = 1.5
    _CONTEXT_TOOL_RESULT_CHAR_STEPS = (16_000, 8_000, 4_000, 2_000, 1_000, 500, 200)
    _CONTEXT_TOOL_RESULT_OMIT = "[tool result omitted to stay within context window]"
    _CONTEXT_TOOL_RESULT_SUFFIX = "\n... (truncated to stay within context window)"
    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        config_path: Path | None = None,
        model: str | None = None,
        max_iterations: int = 40,
        context_window_tokens: int = 65_536,
        context_block_limit: int | None = None,
        max_tool_result_chars: int = _TOOL_RESULT_MAX_CHARS,
        provider_retry_mode: str = "standard",
        web_search_config: Any | None = None,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        web_search_provider: str = "brave",
        web_search_base_url: str | None = None,
        web_search_max_results: int = 5,
        exec_config: ExecToolConfig | None = None,
        image_gen_config: ImageGenConfig | None = None,
        memory_config: MemoryConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        timezone: str | None = None,
        hooks: list[AgentHook] | None = None,
        unified_session: bool = False,
    ):
        from nanobot.config.schema import ExecToolConfig, ImageGenConfig, MemoryConfig
        if web_search_config is not None:
            brave_api_key = getattr(web_search_config, "api_key", brave_api_key) or None
            web_search_provider = getattr(
                web_search_config,
                "provider",
                web_search_provider,
            )
            web_search_base_url = getattr(
                web_search_config,
                "base_url",
                web_search_base_url,
            ) or None
            web_search_max_results = getattr(
                web_search_config,
                "max_results",
                web_search_max_results,
            )
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.config_path = config_path
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.context_window_tokens = context_window_tokens
        self.context_block_limit = context_block_limit
        self.max_tool_result_chars = max_tool_result_chars
        self.provider_retry_mode = provider_retry_mode
        self.brave_api_key = brave_api_key
        self.web_proxy = web_proxy
        self.web_search_provider = web_search_provider
        self.web_search_base_url = web_search_base_url
        self.web_search_max_results = web_search_max_results
        self.exec_config = exec_config or ExecToolConfig()
        self.image_gen_config = image_gen_config or ImageGenConfig()
        self.memory_config = memory_config or MemoryConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self._extra_hooks: list[AgentHook] = list(hooks or [])
        self._clawhub_lock = asyncio.Lock()
        self._clawhub_npm_cache_dir = self._CLAWHUB_NPM_CACHE_DIR / str(os.getpid())
        self._language_commands = LanguageCommandHandler(self)
        self._mcp_commands = MCPCommandHandler(self)
        self._persona_commands = PersonaCommandHandler(self)
        self._skill_commands = SkillCommandHandler(self)
        self._system_commands = SystemCommandHandler(self)
        self._command_router = build_agent_command_router()

        self.context = ContextBuilder(workspace, timezone=timezone)
        self.sessions = session_manager or SessionManager(workspace)
        self.runner = AgentRunner(provider)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            max_tool_result_chars=self.max_tool_result_chars,
            model=self.model,
            brave_api_key=brave_api_key,
            web_proxy=web_proxy,
            web_search_provider=web_search_provider,
            web_search_base_url=web_search_base_url,
            web_search_max_results=web_search_max_results,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )
        self._unified_session = unified_session
        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._runtime_config_mtime_ns = (
            config_path.stat().st_mtime_ns if config_path and config_path.exists() else None
        )
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._mcp_connection_epoch = 0
        self._memorix_started_sessions: set[tuple[int, str, str, str]] = set()
        self._memorix_session_context: dict[tuple[int, str, str, str], str] = {}
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: set[asyncio.Task] = set()
        self._token_consolidation_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        # NANOBOT_MAX_CONCURRENT_REQUESTS: <=0 means unlimited; default 3.
        _max = int(os.environ.get("NANOBOT_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )
        self.memory_consolidator = Consolidator(
            store=self.context.memory,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
        )
        self.consolidator = self.memory_consolidator
        self.dream = Dream(
            store=self.context.memory,
            provider=provider,
            model=self.model,
        )
        self._configure_memory_router()
        self._register_default_tools()

    def _get_session_persona(self, session: Session) -> str:
        """Return the active persona name for a session."""
        return self.context.resolve_persona(session.metadata.get("persona"))

    def _get_session_language(self, session: Session) -> str:
        """Return the active language for a session."""
        metadata = getattr(session, "metadata", {})
        raw = metadata.get("language") if isinstance(metadata, dict) else DEFAULT_LANGUAGE
        return resolve_language(raw)

    def _memory_scope(
        self,
        session: Session,
        *,
        channel: str,
        chat_id: str,
        sender_id: str | None,
        persona: str | None = None,
        language: str | None = None,
        query: str | None = None,
    ) -> MemoryScope:
        """Build the normalized scope used by memory backends."""
        return MemoryScope(
            workspace=self.workspace,
            session_key=session.key,
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            persona=persona or self._get_session_persona(session),
            language=language or self._get_session_language(session),
            query=query,
        )

    async def _commit_memory_turn(
        self,
        *,
        scope: MemoryScope,
        inbound_content: Any | None,
        outbound_content: str | None,
        persisted_messages: list[dict[str, Any]],
    ) -> None:
        """Forward a completed turn to the memory router without blocking replies on failures."""
        try:
            await self.memory_router.commit_turn(
                MemoryCommitRequest(
                    scope=scope,
                    inbound_content=inbound_content,
                    outbound_content=outbound_content,
                    persisted_messages=tuple(persisted_messages),
                )
            )
        except Exception:
            logger.exception("Memory router commit failed for {}", scope.session_key)

    async def _flush_memory_session(
        self,
        session: Session,
        *,
        channel: str,
        chat_id: str,
        sender_id: str | None,
        persona: str | None = None,
        language: str | None = None,
    ) -> None:
        """Flush buffered memory state before persona/session transitions."""
        scope = self._memory_scope(
            session,
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            persona=persona,
            language=language,
        )
        try:
            await self.memory_router.flush_session(scope)
        except Exception:
            logger.exception("Memory router flush failed for {}", scope.session_key)

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

    def _mcp_tool_candidates(self, suffix: str) -> list[str]:
        """Return MCP tool names matching a logical tool suffix."""
        expected = f"_{suffix}"
        return sorted(
            name
            for name in self.tools.tool_names
            if name.startswith("mcp_") and name.endswith(expected)
        )

    def _preferred_mcp_tool(self, suffix: str) -> str | None:
        """Pick the most likely MCP tool wrapper for a logical tool name."""
        candidates = self._mcp_tool_candidates(suffix)
        if not candidates:
            return None
        for name in candidates:
            if name.startswith("mcp_memorix_"):
                return name
        return candidates[0]

    def _has_memorix_tools(self) -> bool:
        """Return whether a Memorix MCP server is currently available."""
        for suffix in (
            "memorix_session_start",
            "memorix_search",
            "memorix_detail",
            "memorix_store",
            "memorix_store_reasoning",
        ):
            if self._preferred_mcp_tool(suffix):
                return True
        return False

    def _runtime_skill_names(self) -> list[str]:
        """Return runtime-activated skills inferred from connected tools."""
        skill_names: list[str] = []
        if self._has_memorix_tools():
            skill_names.append("memorix")
        return skill_names

    @staticmethod
    def _tool_result_to_text(result: Any) -> str:
        """Collapse a tool result into plain text for prompt injection."""
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        if isinstance(result, list):
            parts: list[str] = []
            for block in result:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                else:
                    parts.append(str(block))
            return "\n".join(part for part in parts if part)
        return str(result)

    @staticmethod
    def _is_usable_memorix_context(text: str) -> bool:
        """Filter placeholder MCP outputs that should not enter the prompt."""
        stripped = text.strip()
        if not stripped or stripped == "(no output)":
            return False
        if stripped.startswith("Error"):
            return False
        if stripped.startswith("(MCP tool call"):
            return False
        return True

    def _truncate_memorix_context(self, text: str) -> str:
        """Keep injected Memorix startup context bounded."""
        stripped = text.strip()
        if len(stripped) <= self._MEMORIX_CONTEXT_MAX_CHARS:
            return stripped
        return self._truncate_prompt_text(stripped, self._MEMORIX_CONTEXT_MAX_CHARS)

    async def _maybe_start_memorix_session(self, session: Session) -> str:
        """Bind the current workspace to Memorix once per MCP connection and session."""
        tool_name = self._preferred_mcp_tool("memorix_session_start")
        if not self._mcp_connected or not tool_name:
            return ""

        project_root = str(self.workspace.expanduser().resolve(strict=False))
        state_key = (self._mcp_connection_epoch, session.key, project_root, tool_name)
        cached = self._memorix_session_context.get(state_key)
        if cached is not None:
            return cached
        if state_key in self._memorix_started_sessions:
            return ""

        result = await self.tools.execute(
            tool_name,
            {
                "agent": "nanobot",
                "projectRoot": project_root,
                "sessionId": session.key,
            },
        )
        rendered = self._tool_result_to_text(result)
        self._memorix_started_sessions.add(state_key)
        if not self._is_usable_memorix_context(rendered):
            if rendered.strip():
                logger.warning("Memorix session start returned non-context output: {}", rendered[:200])
            return ""

        context = self._truncate_memorix_context(rendered)
        self._memorix_session_context[state_key] = context
        return context

    @staticmethod
    def _append_system_section(messages: list[dict[str, Any]], title: str, content: str) -> None:
        """Append an extra section to the system prompt if present."""
        if not content or not messages:
            return
        system = messages[0]
        if system.get("role") != "system" or not isinstance(system.get("content"), str):
            return
        system["content"] += f"\n\n---\n\n# {title}\n\n{content}"

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
        self._memorix_started_sessions.clear()
        self._memorix_session_context.clear()

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

        for name in ("history_search", "history_expand"):
            if tool := self.tools.get(name):
                if hasattr(tool, "update_workspace"):
                    tool.update_workspace(self.workspace)

        if cron_tool := self.tools.get("cron"):
            if hasattr(cron_tool, "set_default_timezone"):
                cron_tool.set_default_timezone(self.context.timezone or "UTC")

        self._sync_image_gen_tool()

    def _rebind_runtime_workspace(self, workspace: Path) -> None:
        """Switch runtime-bound workspace references in place."""
        self.workspace = workspace
        self.context.rebind_runtime(workspace=workspace, timezone=self.context.timezone)
        self.sessions.rebind_workspace(workspace)
        self.memory_consolidator.rebind_runtime(workspace=workspace, sessions=self.sessions)
        self.memory_consolidator.store = self.context.memory
        self.consolidator = self.memory_consolidator
        self.dream = Dream(
            store=self.context.memory,
            provider=self.provider,
            model=self.model,
            max_batch_size=self.dream.max_batch_size,
            max_iterations=self.dream.max_iterations,
            max_tool_result_chars=self.dream.max_tool_result_chars,
        )

    def _configure_memory_router(self) -> None:
        """Build the current memory router from runtime config."""
        user_backend = self._build_user_memory_backend(self.memory_config)
        fallback_backend = self._build_memory_fallback_backend(self.memory_config, user_backend)
        shadow_backends = self._build_shadow_memory_backends(self.memory_config, user_backend)
        self.memory_router = MemoryRouter(
            user_backend=user_backend,
            fallback_backend=fallback_backend,
            shadow_backends=shadow_backends,
        )

    def _build_user_memory_backend(self, config: MemoryConfig) -> UserMemoryBackend:
        """Create the configured primary user-memory backend."""
        if config.user.backend == "mem0":
            return Mem0UserMemoryBackend(config.user.mem0)
        return FileUserMemoryBackend()

    def _build_memory_fallback_backend(
        self,
        config: MemoryConfig,
        primary: UserMemoryBackend,
    ) -> UserMemoryBackend | None:
        """Keep file-backed memory as the conservative fallback when Mem0 is primary."""
        if config.user.backend == "mem0":
            return FileUserMemoryBackend()
        return None

    def _build_shadow_memory_backends(
        self,
        config: MemoryConfig,
        primary: UserMemoryBackend,
    ) -> list[UserMemoryBackend]:
        """Create optional shadow backends that receive writes in parallel."""
        if config.user.shadow_write_mem0 and not isinstance(primary, Mem0UserMemoryBackend):
            return [Mem0UserMemoryBackend(config.user.mem0)]
        return []

    def _apply_runtime_config(self, config) -> bool:
        """Apply hot-reloadable config to the current agent instance."""
        from nanobot.providers.base import GenerationSettings

        defaults = config.agents.defaults
        tools_cfg = config.tools
        web_cfg = tools_cfg.web
        search_cfg = web_cfg.search
        next_workspace = config.workspace_path
        next_timezone = defaults.timezone

        if next_workspace.resolve(strict=False) != self.workspace.resolve(strict=False):
            self._rebind_runtime_workspace(next_workspace)

        self.context.rebind_runtime(workspace=self.workspace, timezone=next_timezone)

        self.model = defaults.model
        self.max_iterations = defaults.max_tool_iterations
        self.context_window_tokens = defaults.context_window_tokens
        self.context_block_limit = defaults.context_block_limit
        self.max_tool_result_chars = defaults.max_tool_result_chars
        self.provider_retry_mode = defaults.provider_retry_mode
        self.exec_config = tools_cfg.exec
        self.image_gen_config = tools_cfg.image_gen
        self.memory_config = config.memory
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
        self.memory_consolidator.max_completion_tokens = defaults.max_tokens
        self.subagents.apply_runtime_config(
            workspace=self.workspace,
            model=self.model,
            brave_api_key=self.brave_api_key,
            web_proxy=self.web_proxy,
            web_search_provider=self.web_search_provider,
            web_search_base_url=self.web_search_base_url,
            web_search_max_results=self.web_search_max_results,
            exec_config=self.exec_config,
            restrict_to_workspace=self.restrict_to_workspace,
        )
        self._configure_memory_router()
        self._apply_runtime_tool_config()

        mcp_changed = self._dump_mcp_servers(config.tools.mcp_servers) != self._dump_mcp_servers(
            self._mcp_servers
        )
        self._mcp_servers = config.tools.mcp_servers
        return mcp_changed

    async def reload_runtime_config(self, config=None, *, force: bool = False) -> None:
        """Public wrapper for applying hot-reloadable runtime config."""
        if config is not None:
            if self.config_path and self.config_path.exists():
                self._runtime_config_mtime_ns = self.config_path.stat().st_mtime_ns
            if self._apply_runtime_config(config):
                await self._reset_mcp_connections()
            return
        await self._reload_runtime_config_if_needed(force=force)

    def _sync_image_gen_tool(self) -> None:
        """Register, update, or remove the optional image generation tool."""
        config = self.image_gen_config
        existing = self.tools.get("image_gen")
        if not config.enabled:
            if existing:
                self.tools.unregister("image_gen")
            return

        proxy = config.proxy or self.web_proxy
        if isinstance(existing, ImageGenTool):
            existing.update_config(
                workspace=self.workspace,
                api_key=config.api_key,
                base_url=config.base_url,
                model=config.model,
                proxy=proxy,
                timeout=config.timeout,
                reference_image=config.reference_image,
                restrict_to_workspace=self.restrict_to_workspace,
            )
            return

        self.tools.register(
            ImageGenTool(
                workspace=self.workspace,
                api_key=config.api_key,
                base_url=config.base_url,
                model=config.model,
                proxy=proxy,
                timeout=config.timeout,
                reference_image=config.reference_image,
                restrict_to_workspace=self.restrict_to_workspace,
            )
        )

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
    def _skill_subcommand(parts: list[str]) -> str | None:
        if len(parts) < 2:
            return None
        return parts[1].lower()

    @staticmethod
    def _skill_search_query(content: str) -> str | None:
        query_parts = content.strip().split(None, 2)
        if len(query_parts) < 3:
            return None
        query = query_parts[2].strip()
        return query or None

    @staticmethod
    def _skill_argument(parts: list[str]) -> str | None:
        if len(parts) < 3:
            return None
        value = parts[2].strip()
        return value or None

    def _command_context(
        self,
        msg: InboundMessage,
        *,
        session: Session | None = None,
        key: str | None = None,
    ) -> CommandContext:
        return CommandContext(
            msg=msg,
            session=session,
            key=key or msg.session_key,
            raw=msg.content.strip(),
            loop=self,
        )

    async def _handle_skill_command(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        """Handle ClawHub skill management commands for the active workspace."""
        language = self._get_session_language(session)
        parts = msg.content.strip().split()
        subcommand = self._skill_subcommand(parts)
        if not subcommand:
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=text(language, "skill_usage"))

        if subcommand == "search":
            query = self._skill_search_query(msg.content)
            if not query:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=text(language, "skill_search_missing_query"),
                )
            return await self._skill_commands.search(msg, language, query)

        if subcommand == "install":
            slug = self._skill_argument(parts)
            if not slug:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=text(language, "skill_install_missing_slug"),
                )
            return await self._skill_commands.install(msg, language, slug)

        if subcommand == "uninstall":
            slug = self._skill_argument(parts)
            if not slug:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=text(language, "skill_uninstall_missing_slug"),
                )
            return await self._skill_commands.uninstall(msg, language, slug)

        if subcommand == "list":
            return await self._skill_commands.list(msg, language)

        if subcommand == "update":
            return await self._skill_commands.update(msg, language)

        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=text(language, "skill_usage"))

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

        return await self._mcp_commands.list(msg, language)

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if (self.restrict_to_workspace or self.exec_config.sandbox) else None
        extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None
        self.tools.register(ReadFileTool(workspace=self.workspace, allowed_dir=allowed_dir, extra_allowed_dirs=extra_read))
        for cls in (WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        for cls in (GlobTool, GrepTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        if self.exec_config.enable:
            self.tools.register(ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
                sandbox=self.exec_config.sandbox,
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
        self.tools.register(HistorySearchTool(workspace=self.workspace))
        self.tools.register(HistoryExpandTool(workspace=self.workspace))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        self._sync_image_gen_tool()
        if self.cron_service:
            self.tools.register(
                CronTool(self.cron_service, default_timezone=self.context.timezone or "UTC")
            )

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
            self._mcp_connection_epoch += 1
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

    def _set_tool_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        persona: str | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron", "history_search", "history_expand"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    if name == "message":
                        tool.set_context(channel, chat_id, message_id)
                    elif name in ("history_search", "history_expand"):
                        tool.set_context(channel, chat_id, persona)
                    else:
                        tool.set_context(channel, chat_id)
        if image_tool := self.tools.get("image_gen"):
            if hasattr(image_tool, "set_persona"):
                image_tool.set_persona(persona)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        from nanobot.utils.helpers import strip_think
        return strip_think(text) or None

    def _filter_persona_response(self, text: str | None, persona: str | None) -> str | None:
        """Apply persona-level response filtering for user-visible output only."""
        if text is None:
            return None
        tags = load_persona_response_filter_tags(self.workspace, persona)
        if not tags:
            return text
        return strip_tagged_response_content(text, tags)

    def _visible_response_text(self, text: str | None, persona: str | None) -> str:
        """Return the user-visible version of a model response."""
        clean = self._strip_think(text) or ""
        return self._filter_persona_response(clean, persona) or ""

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hints with smart abbreviation."""
        from nanobot.utils.tool_hints import format_tool_hints

        return format_tool_hints(tool_calls)

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
    ) -> dict[str, Any]:
        """Resolve provider-specific voice settings for the active persona."""
        cfg = getattr(self.channels_config, "voice_reply", None)
        persona_voice = load_persona_voice_settings(self.workspace, persona)
        provider_name = persona_voice.provider or getattr(cfg, "provider", "openai")

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
        speed = (
            persona_voice.speed
            if persona_voice.speed is not None
            else getattr(cfg, "speed", None) if cfg is not None else None
        )
        if provider_name == "edge":
            voice = persona_voice.voice or getattr(cfg, "edge_voice", "zh-CN-XiaoxiaoNeural")
        else:
            voice = persona_voice.voice or getattr(cfg, "voice", "alloy")

        return {
            "provider": provider_name,
            "voice": voice,
            "instructions": instructions,
            "speed": speed,
            "api_base": persona_voice.api_base or getattr(cfg, "api_base", ""),
            "rate": persona_voice.rate or getattr(cfg, "edge_rate", "+0%"),
            "volume": persona_voice.volume or getattr(cfg, "edge_volume", "+0%"),
            "sovits_api_url": persona_voice.api_base or getattr(cfg, "sovits_api_url", ""),
            "sovits_refer_wav_path": persona_voice.refer_wav_path
            or getattr(cfg, "sovits_refer_wav_path", ""),
            "sovits_prompt_text": persona_voice.prompt_text or getattr(cfg, "sovits_prompt_text", ""),
            "sovits_prompt_language": persona_voice.prompt_language
            or getattr(cfg, "sovits_prompt_language", "zh"),
            "sovits_text_language": persona_voice.text_language
            or getattr(cfg, "sovits_text_language", "zh"),
            "sovits_cut_punc": persona_voice.cut_punc or getattr(cfg, "sovits_cut_punc", "，。"),
            "sovits_top_k": persona_voice.top_k
            if persona_voice.top_k is not None
            else getattr(cfg, "sovits_top_k", 5),
            "sovits_top_p": persona_voice.top_p
            if persona_voice.top_p is not None
            else getattr(cfg, "sovits_top_p", 1.0),
            "sovits_temperature": persona_voice.temperature
            if persona_voice.temperature is not None
            else getattr(cfg, "sovits_temperature", 1.0),
        }

    @staticmethod
    def _voice_reply_response_format(provider_name: str, configured_format: str) -> str:
        """Resolve the final output format for the selected voice provider."""
        if provider_name == "edge":
            return "mp3"
        if provider_name == "sovits" and configured_format == "opus":
            return "wav"
        return configured_format

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

        profile = self._voice_reply_profile(persona)
        provider_name = profile["provider"]
        response_format = self._voice_reply_response_format(
            provider_name,
            getattr(cfg, "response_format", "opus"),
        )
        model = getattr(cfg, "model", "gpt-4o-mini-tts")
        media_dir = ensure_dir(self.workspace / "out" / "voice")
        filename = safe_filename(
            f"{outbound.channel}_{outbound.chat_id}_{int(time.time() * 1000)}"
        ) + self._voice_reply_extension(response_format)
        output_path = media_dir / filename

        try:
            if provider_name == "edge":
                provider = EdgeSpeechProvider(
                    voice=profile["voice"],
                    rate=profile["rate"],
                    volume=profile["volume"],
                )
                await provider.synthesize_to_file(outbound.content, output_path=output_path)
            elif provider_name == "sovits":
                provider = GPTSoVITSSpeechProvider(
                    api_url=(profile["sovits_api_url"] or "http://127.0.0.1:9880").strip(),
                    refer_wav_path=profile["sovits_refer_wav_path"],
                    prompt_text=profile["sovits_prompt_text"],
                    prompt_language=profile["sovits_prompt_language"],
                    text_language=profile["sovits_text_language"],
                    cut_punc=profile["sovits_cut_punc"],
                    top_k=profile["sovits_top_k"],
                    top_p=profile["sovits_top_p"],
                    temperature=profile["sovits_temperature"],
                    speed=profile["speed"] or 1.0,
                )
                await provider.synthesize_to_file(outbound.content, output_path=output_path)
            else:
                api_key = (
                    getattr(cfg, "api_key", "") or getattr(self.provider, "api_key", "") or ""
                ).strip()
                if not api_key:
                    logger.warning(
                        "Voice reply enabled for {}, but no TTS api_key is configured",
                        outbound.channel,
                    )
                    return outbound
                api_base = (
                    profile["api_base"]
                    or getattr(self.provider, "api_base", "")
                    or "https://api.openai.com/v1"
                ).strip()
                provider = OpenAISpeechProvider(api_key=api_key, api_base=api_base)
                await provider.synthesize_to_file(
                    outbound.content,
                    model=model,
                    voice=profile["voice"],
                    instructions=profile["instructions"],
                    speed=profile["speed"],
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
        *,
        session: Session | None = None,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        persona: str | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming)*: called when a streaming session finishes.
        ``resuming=True`` means tool calls follow (spinner should restart);
        ``resuming=False`` means this is the final response.
        """
        loop_self = self

        class _LoopHook(AgentHook):
            def __init__(self) -> None:
                super().__init__(reraise=True)
                self._stream_buf = ""

            def wants_streaming(self) -> bool:
                return on_stream is not None

            def prepare_messages(
                self,
                context: AgentHookContext,
                tool_definitions: list[dict[str, Any]],
            ) -> list[dict[str, Any]]:
                return loop_self._prepare_request_messages(context.messages, tool_definitions)

            async def on_stream(self, context: AgentHookContext, delta: str) -> None:
                prev_clean = loop_self._visible_response_text(self._stream_buf, persona)
                self._stream_buf += delta
                new_clean = loop_self._visible_response_text(self._stream_buf, persona)
                incremental = new_clean[len(prev_clean):]
                if incremental and on_stream:
                    await on_stream(incremental)

            async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
                if on_stream_end:
                    await on_stream_end(resuming=resuming)
                self._stream_buf = ""

            async def before_execute_tools(self, context: AgentHookContext) -> None:
                if on_progress:
                    if not on_stream:
                        thought = loop_self._visible_response_text(
                            context.response.content if context.response else None,
                            persona,
                        )
                        if thought:
                            await on_progress(thought)
                    tool_hint = loop_self._strip_think(loop_self._tool_hint(context.tool_calls))
                    await on_progress(tool_hint, tool_hint=True)
                for tc in context.tool_calls:
                    args_str = json.dumps(tc.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tc.name, args_str[:200])
                loop_self._set_tool_context(channel, chat_id, message_id, persona)

            def normalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
                return loop_self._strip_think(content)

            def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
                visible = loop_self._filter_persona_response(content, persona)
                return visible or content

        loop_hook = _LoopHook()
        hook: AgentHook = (
            CompositeHook([loop_hook] + self._extra_hooks)
            if self._extra_hooks
            else loop_hook
        )

        async def _checkpoint(payload: dict[str, Any]) -> None:
            if session is None:
                return
            self._set_runtime_checkpoint(session, payload)

        result = await self.runner.run(AgentRunSpec(
            initial_messages=initial_messages,
            tools=self.tools,
            model=self.model,
            max_iterations=self.max_iterations,
            max_tool_result_chars=self.max_tool_result_chars,
            hook=hook,
            error_message="Sorry, I encountered an error calling the AI model.",
            max_iterations_message=(
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            ),
            concurrent_tools=True,
            workspace=self.workspace,
            session_key=session.key if session else None,
            context_window_tokens=self.context_window_tokens,
            context_block_limit=self.context_block_limit,
            provider_retry_mode=self.provider_retry_mode,
            progress_callback=on_progress,
            checkpoint_callback=_checkpoint,
        ))
        self._last_usage = result.usage
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", self.max_iterations)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", ((result.error or result.final_content) or "")[:200])

        return result.final_content, result.tools_used, result.messages

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

            ctx = self._command_context(
                msg,
                session=self.sessions.get_or_create(msg.session_key),
            )
            if self._command_router.is_priority(ctx.raw):
                result = await self._command_router.dispatch_priority(ctx)
                if result is not None:
                    await self.bus.publish_outbound(result)
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
        content = (
            text(language, "stopped_tasks", count=total)
            if total
            else text(language, "no_active_task")
        )
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=content,
        ))

    async def _handle_restart(self, msg: InboundMessage) -> None:
        """Restart the process in-place via os.execv."""
        session = self.sessions.get_or_create(msg.session_key)
        language = self._get_session_language(session)
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=text(language, "restarting"),
        ))

        async def _do_restart():
            await asyncio.sleep(1)
            # Use -m nanobot instead of sys.argv[0] for Windows compatibility
            # (sys.argv[0] may be just "nanobot" without full path on Windows)
            os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

        asyncio.create_task(_do_restart())

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message: per-session serial, cross-session concurrent."""
        if self._unified_session and not msg.session_key_override:
            msg = dataclasses.replace(msg, session_key_override=UNIFIED_SESSION_KEY)
        lock = self._session_locks.setdefault(msg.session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()
        async with lock, gate:
            try:
                on_stream = on_stream_end = None
                if msg.metadata.get("_wants_stream"):
                    # Split one answer into distinct stream segments.
                    stream_base_id = f"{msg.session_key}:{time.time_ns()}"
                    stream_segment = 0

                    def _current_stream_id() -> str:
                        return f"{stream_base_id}:{stream_segment}"

                    async def on_stream(delta: str) -> None:
                        meta = dict(msg.metadata or {})
                        meta["_stream_delta"] = True
                        meta["_stream_id"] = _current_stream_id()
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content=delta,
                            metadata=meta,
                        ))

                    async def on_stream_end(*, resuming: bool = False) -> None:
                        nonlocal stream_segment
                        meta = dict(msg.metadata or {})
                        meta["_stream_end"] = True
                        meta["_resuming"] = resuming
                        meta["_stream_id"] = _current_stream_id()
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="",
                            metadata=meta,
                        ))
                        stream_segment += 1

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
        parts = msg.content.strip().split()
        current = self._get_session_language(session)
        if len(parts) == 1 or parts[1].lower() == "current":
            return self._language_commands.current(msg, session)

        subcommand = parts[1].lower()
        if subcommand == "list":
            return self._language_commands.list(msg, session)

        if subcommand != "set" or len(parts) < 3:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self._language_usage(current),
            )

        return self._language_commands.set(msg, session, parts[2])

    async def _handle_persona_command(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        """Handle session-scoped persona management commands."""
        language = self._get_session_language(session)
        parts = msg.content.strip().split()
        if len(parts) == 1 or parts[1].lower() == "current":
            return self._persona_commands.current(msg, session)

        subcommand = parts[1].lower()
        if subcommand == "list":
            return self._persona_commands.list(msg, session)

        if subcommand != "set" or len(parts) < 3:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self._persona_usage(language),
            )

        return await self._persona_commands.set(msg, session, parts[2])

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
            self._set_tool_context(
                channel,
                chat_id,
                msg.metadata.get("message_id"),
                persona=persona,
            )
            history = session.get_history(max_messages=0)
            memorix_context = await self._maybe_start_memorix_session(session)
            current_role = "assistant" if msg.sender_id == "subagent" else "user"
            memory_scope = self._memory_scope(
                session,
                channel=channel,
                chat_id=chat_id,
                sender_id=msg.sender_id,
                persona=persona,
                language=language,
                query=msg.content,
            )
            resolved_memory = await self.memory_router.prepare_context(memory_scope)
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content,
                skill_names=self._runtime_skill_names(),
                channel=channel,
                chat_id=chat_id,
                persona=persona,
                language=language,
                current_role=current_role,
                memory_context=resolved_memory.block,
            )
            self._append_system_section(messages, "Workspace Memory (Memorix)", memorix_context)
            final_content, _, all_msgs = await self._run_agent_loop(
                messages, session=session, channel=channel, chat_id=chat_id,
                message_id=msg.metadata.get("message_id"),
                persona=persona,
            )
            persisted_messages = self._save_turn(session, all_msgs, 1 + len(history))
            self.sessions.save(session)
            await self._commit_memory_turn(
                scope=memory_scope,
                inbound_content=msg.content,
                outbound_content=final_content,
                persisted_messages=persisted_messages,
            )
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
        slash_response = await self._command_router.dispatch(
            self._command_context(msg, session=session, key=key)
        )
        if slash_response is not None:
            return slash_response
        await self._connect_mcp()
        await self._run_preflight_token_consolidation(session)

        self._set_tool_context(
            msg.channel,
            msg.chat_id,
            msg.metadata.get("message_id"),
            persona=persona,
        )
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=0)
        memorix_context = await self._maybe_start_memorix_session(session)
        memory_scope = self._memory_scope(
            session,
            channel=msg.channel,
            chat_id=msg.chat_id,
            sender_id=msg.sender_id,
            persona=persona,
            language=language,
            query=msg.content,
        )
        resolved_memory = await self.memory_router.prepare_context(memory_scope)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            skill_names=self._runtime_skill_names(),
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
            persona=persona,
            language=language,
            memory_context=resolved_memory.block,
        )
        self._append_system_section(
            initial_messages,
            "Workspace Memory (Memorix)",
            memorix_context,
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
            session=session,
            channel=msg.channel, chat_id=msg.chat_id,
            message_id=msg.metadata.get("message_id"),
            persona=persona,
        )

        if final_content is None or not final_content.strip():
            final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        persisted_messages = self._save_turn(session, all_msgs, 1 + len(history))
        self.sessions.save(session)
        await self._commit_memory_turn(
            scope=memory_scope,
            inbound_content=msg.content,
            outbound_content=final_content,
            persisted_messages=persisted_messages,
        )
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
                path = (block.get("_meta") or {}).get("path", "")
                filtered.append({"type": "text", "text": image_placeholder_text(path)})
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if truncate_text and len(text) > self.max_tool_result_chars:
                    text = truncate_text_value(text, self.max_tool_result_chars)
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _prompt_budget_tokens(self) -> int:
        """Return the current-turn prompt budget used for tool-result compaction."""
        return max(0, int(self.context_window_tokens))

    def _truncate_prompt_text(self, text: str, max_chars: int) -> str:
        """Trim text for in-flight prompt compaction."""
        if max_chars <= 0:
            return self._CONTEXT_TOOL_RESULT_OMIT
        if len(text) <= max_chars:
            return text
        if max_chars <= len(self._CONTEXT_TOOL_RESULT_SUFFIX):
            return text[:max_chars]
        return text[: max_chars - len(self._CONTEXT_TOOL_RESULT_SUFFIX)] + self._CONTEXT_TOOL_RESULT_SUFFIX

    def _compact_tool_result_for_prompt(self, content: Any, max_chars: int) -> Any:
        """Compact a tool result just enough to keep the current turn within budget."""
        if max_chars <= 0:
            return self._CONTEXT_TOOL_RESULT_OMIT

        if isinstance(content, str):
            return self._truncate_prompt_text(content, max_chars)

        if isinstance(content, list):
            remaining = max_chars
            compacted: list[dict[str, Any]] = []
            for block in self._sanitize_persisted_blocks(content):
                if remaining <= 0:
                    break
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    text = block["text"]
                    trimmed = self._truncate_prompt_text(text, remaining)
                    compacted.append({**block, "text": trimmed})
                    remaining -= len(trimmed)
                    if trimmed != text:
                        break
                    continue

                raw = json.dumps(block, ensure_ascii=False)
                trimmed = self._truncate_prompt_text(raw, remaining)
                compacted.append({"type": "text", "text": trimmed})
                remaining -= len(trimmed)
                if trimmed != raw:
                    break

            return compacted or [{"type": "text", "text": self._CONTEXT_TOOL_RESULT_OMIT}]

        if content is None:
            return None
        return self._truncate_prompt_text(json.dumps(content, ensure_ascii=False), max_chars)

    def _prepare_request_messages(
        self,
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Shrink older tool results on demand so system prompt and recent context still fit."""
        budget = self._prompt_budget_tokens()
        if budget <= 0:
            return messages

        estimated, source = estimate_prompt_tokens_chain(self.provider, self.model, messages, tool_defs)
        if estimated <= budget:
            return messages

        tool_indices = [idx for idx, message in enumerate(messages) if message.get("role") == "tool"]
        if not tool_indices:
            logger.warning(
                "Prompt over budget for current turn: {}/{} via {} (no tool results to compact)",
                estimated,
                self.context_window_tokens,
                source,
            )
            return messages

        prepared = list(messages)
        older_indices = tool_indices[:-1] if len(tool_indices) > 1 else tool_indices
        newest_indices = tool_indices[-1:] if len(tool_indices) > 1 else []

        for indices in (older_indices, newest_indices):
            for max_chars in self._CONTEXT_TOOL_RESULT_CHAR_STEPS:
                for idx in indices:
                    compacted = self._compact_tool_result_for_prompt(messages[idx].get("content"), max_chars)
                    if compacted == prepared[idx].get("content"):
                        continue
                    prepared[idx] = {**prepared[idx], "content": compacted}
                    estimated, source = estimate_prompt_tokens_chain(
                        self.provider,
                        self.model,
                        prepared,
                        tool_defs,
                    )
                    if estimated <= budget:
                        logger.info(
                            "Compacted tool results for current turn: {}/{} via {}",
                            estimated,
                            self.context_window_tokens,
                            source,
                        )
                        return prepared

        logger.warning(
            "Prompt still over budget after tool-result compaction: {}/{} via {}",
            estimated,
            self.context_window_tokens,
            source,
        )
        return prepared

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> list[dict[str, Any]]:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime
        persisted: list[dict[str, Any]] = []
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool":
                if isinstance(content, str) and len(content) > self.max_tool_result_chars:
                    entry["content"] = truncate_text_value(content, self.max_tool_result_chars)
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
            persisted.append(entry)
        session.updated_at = datetime.now()
        return persisted

    def _set_runtime_checkpoint(self, session: Session, payload: dict[str, Any]) -> None:
        """Persist the latest in-flight turn state into session metadata."""
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = payload
        self.sessions.save(session)

    def _clear_runtime_checkpoint(self, session: Session) -> None:
        if self._RUNTIME_CHECKPOINT_KEY in session.metadata:
            session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)

    @staticmethod
    def _checkpoint_message_key(message: dict[str, Any]) -> tuple[Any, ...]:
        return (
            message.get("role"),
            message.get("content"),
            message.get("tool_call_id"),
            message.get("name"),
            message.get("tool_calls"),
            message.get("reasoning_content"),
            message.get("thinking_blocks"),
        )

    def _restore_runtime_checkpoint(self, session: Session) -> bool:
        """Materialize an unfinished turn into session history before a new request."""
        from datetime import datetime

        checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return False

        assistant_message = checkpoint.get("assistant_message")
        completed_tool_results = checkpoint.get("completed_tool_results") or []
        pending_tool_calls = checkpoint.get("pending_tool_calls") or []

        restored_messages: list[dict[str, Any]] = []
        if isinstance(assistant_message, dict):
            restored = dict(assistant_message)
            restored.setdefault("timestamp", datetime.now().isoformat())
            restored_messages.append(restored)
        for message in completed_tool_results:
            if isinstance(message, dict):
                restored = dict(message)
                restored.setdefault("timestamp", datetime.now().isoformat())
                restored_messages.append(restored)
        for tool_call in pending_tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_id = tool_call.get("id")
            name = ((tool_call.get("function") or {}).get("name")) or "tool"
            restored_messages.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "name": name,
                "content": "Error: Task interrupted before this tool finished.",
                "timestamp": datetime.now().isoformat(),
            })

        overlap = 0
        max_overlap = min(len(session.messages), len(restored_messages))
        for size in range(max_overlap, 0, -1):
            existing = session.messages[-size:]
            restored = restored_messages[:size]
            if all(
                self._checkpoint_message_key(left) == self._checkpoint_message_key(right)
                for left, right in zip(existing, restored)
            ):
                overlap = size
                break
        session.messages.extend(restored_messages[overlap:])

        self._clear_runtime_checkpoint(session)
        return True

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
