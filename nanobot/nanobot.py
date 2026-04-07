"""High-level programmatic interface to nanobot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanobot.agent.hook import AgentHook
from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus


@dataclass(slots=True)
class RunResult:
    """Result of a single agent run."""

    content: str
    tools_used: list[str]
    messages: list[dict[str, Any]]


class Nanobot:
    """Programmatic facade for running the nanobot agent.

    Usage::

        bot = Nanobot.from_config()
        result = await bot.run("Summarize this repo", hooks=[MyHook()])
        print(result.content)
    """

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | None = None,
        *,
        workspace: str | Path | None = None,
    ) -> Nanobot:
        """Create a Nanobot instance from a config file.

        Args:
            config_path: Path to ``config.json``.  Defaults to
                ``~/.nanobot/config.json``.
            workspace: Override the workspace directory from config.
        """
        from nanobot.config.loader import load_config, resolve_config_env_vars
        from nanobot.config.schema import Config

        resolved: Path | None = None
        if config_path is not None:
            resolved = Path(config_path).expanduser().resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"Config not found: {resolved}")

        config: Config = resolve_config_env_vars(load_config(resolved))
        if workspace is not None:
            config.agents.defaults.workspace = str(
                Path(workspace).expanduser().resolve()
            )

        provider = _make_provider(config)
        bus = MessageBus()
        defaults = config.agents.defaults
        runtime_config_path = getattr(config, "_config_path", None)

        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            config_path=runtime_config_path,
            model=defaults.model,
            max_iterations=defaults.max_tool_iterations,
            context_window_tokens=defaults.context_window_tokens,
            brave_api_key=config.tools.web.search.api_key or None,
            web_proxy=config.tools.web.proxy or None,
            web_search_provider=config.tools.web.search.provider,
            web_search_base_url=config.tools.web.search.base_url or None,
            web_search_max_results=config.tools.web.search.max_results,
            exec_config=config.tools.exec,
            image_gen_config=config.tools.image_gen,
            memory_config=config.memory,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            mcp_servers=config.tools.mcp_servers,
            channels_config=config.channels,
            timezone=defaults.timezone,
        )
        return cls(loop)

    async def run(
        self,
        message: str,
        *,
        session_key: str = "sdk:default",
        hooks: list[AgentHook] | None = None,
    ) -> RunResult:
        """Run the agent once and return the result.

        Args:
            message: The user message to process.
            session_key: Session identifier for conversation isolation.
                Different keys get independent history.
            hooks: Optional lifecycle hooks for this run.
        """
        prev = self._loop._extra_hooks
        if hooks is not None:
            self._loop._extra_hooks = list(hooks)
        try:
            response = await self._loop.process_direct(
                message, session_key=session_key,
            )
        finally:
            self._loop._extra_hooks = prev

        content = (response.content if response else None) or ""
        return RunResult(content=content, tools_used=[], messages=[])


def _make_provider(config: Any) -> Any:
    """Create the configured LLM provider or provider pool from config."""
    defaults = config.agents.defaults
    provider_pool = defaults.provider_pool
    if provider_pool and provider_pool.targets:
        from nanobot.providers.pool_provider import ProviderPoolEntry, ProviderPoolProvider

        entries = [
            ProviderPoolEntry(
                name=target.provider,
                model=target.model,
                provider=_make_single_provider(
                    config,
                    model=target.model or defaults.model,
                    provider_name=target.provider,
                ),
            )
            for target in provider_pool.targets
        ]
        pooled = ProviderPoolProvider(
            entries,
            strategy=provider_pool.strategy,
            default_model=defaults.model,
        )
        pooled.generation = entries[0].provider.generation
        return pooled

    return _make_single_provider(config, model=defaults.model)


def _make_single_provider(
    config: Any,
    *,
    model: str,
    provider_name: str | None = None,
) -> Any:
    """Create one provider instance from config."""
    from nanobot.providers.base import GenerationSettings
    from nanobot.providers.registry import find_by_name

    resolved_provider_name = provider_name or config.get_provider_name(model)
    spec = find_by_name(resolved_provider_name) if resolved_provider_name else None
    if provider_name and spec is None:
        raise ValueError(f"Unknown provider: {provider_name}")

    if spec is not None:
        p = getattr(config.providers, spec.name, None)
    else:
        p = config.get_provider(model)
    backend = spec.backend if spec else "openai_compat"
    api_base = None
    if p and p.api_base:
        api_base = p.api_base
    elif spec and (spec.is_gateway or spec.is_local) and spec.default_api_base:
        api_base = spec.default_api_base

    if backend == "azure_openai":
        if not p or not p.api_key or not p.api_base:
            raise ValueError("Azure OpenAI requires api_key and api_base in config.")
    elif backend == "openai_compat" and not model.startswith("bedrock/"):
        needs_key = not (p and p.api_key)
        exempt = spec and (spec.is_oauth or spec.is_local or spec.is_direct)
        if needs_key and not exempt:
            raise ValueError(f"No API key configured for provider '{provider_name}'.")

    if backend == "openai_codex":
        from nanobot.providers.openai_codex_provider import OpenAICodexProvider

        provider = OpenAICodexProvider(default_model=model)
    elif backend == "github_copilot":
        from nanobot.providers.github_copilot_provider import GitHubCopilotProvider

        provider = GitHubCopilotProvider(default_model=model)
    elif backend == "azure_openai":
        from nanobot.providers.azure_openai_provider import AzureOpenAIProvider

        provider = AzureOpenAIProvider(
            api_key=p.api_key, api_base=p.api_base, default_model=model
        )
    elif backend == "anthropic":
        from nanobot.providers.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(
            api_key=p.api_key if p else None,
            api_base=api_base,
            default_model=model,
            extra_headers=p.extra_headers if p else None,
        )
    else:
        from nanobot.providers.openai_compat_provider import OpenAICompatProvider

        provider = OpenAICompatProvider(
            api_key=p.api_key if p else None,
            api_base=api_base,
            default_model=model,
            extra_headers=p.extra_headers if p else None,
            spec=spec,
        )

    defaults = config.agents.defaults
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=defaults.reasoning_effort,
    )
    return provider
