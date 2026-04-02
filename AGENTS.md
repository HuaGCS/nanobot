# Repository Guidelines

## Project Structure & Module Organization
`nanobot/` is the main Python package. Core agent logic lives in `nanobot/agent/`, channel integrations in `nanobot/channels/`, providers in `nanobot/providers/`, and CLI/config code in `nanobot/cli/` and `nanobot/config/`. Localized command/help text lives in `nanobot/locales/`. Bundled prompts and built-in skills live in `nanobot/templates/` and `nanobot/skills/`, while workspace-installed skills are loaded from `<workspace>/skills/`. Tests go in `tests/` with `test_<feature>.py` names. The WhatsApp bridge is a separate TypeScript project in `bridge/`.

## Build, Test, and Development Commands
- `uv sync --extra dev`: install Python runtime and developer dependencies from `pyproject.toml` and `uv.lock`.
- `uv run pytest`: run the full Python test suite.
- `uv run pytest tests/test_web_tools.py -q`: run one focused test file during iteration.
- `uv run pytest tests/test_skill_commands.py -q`: run the ClawHub slash-command regression tests.
- `uv run ruff check .`: lint Python code and normalize import ordering.
- `uv run nanobot agent`: start the local CLI agent.
- `cd bridge && npm install && npm run build`: install and compile the WhatsApp bridge.
- `bash tests/test_docker.sh`: smoke-test the Docker image and onboarding flow.

## Coding Style & Naming Conventions
Target Python 3.11+ and keep Python code consistent with Ruff: 4-space indentation, `snake_case` for functions/modules, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants. Ruff uses a 100-character target; stay near it even though long-line errors are ignored. Prefer explicit type hints and small functions. In `bridge/src/`, keep the current ESM TypeScript style and avoid reformatting unrelated lines.

## Testing Guidelines
Write pytest tests using `tests/test_<feature>.py` naming. Add a regression test for every bug fix and cover async flows, channel adapters, and tool behavior when touched. If you change slash commands or command help, update the related loop/localization tests and, when relevant, Telegram command-menu coverage. `pytest-asyncio` is already enabled with automatic asyncio handling. There is no published coverage gate, so prefer targeted assertions over smoke-only tests.

## Commit & Pull Request Guidelines
Recent history favors short Conventional Commit subjects such as `fix(memory): ...`, `feat(web): ...`, and `docs: ...`. Use imperative mood, add a scope when it helps, and keep unrelated changes out of the same commit. PRs should summarize the behavior change, note config or channel impact, list the tests you ran, and link the relevant issue or PR discussion. Include screenshots only when CLI output or user-visible behavior changed.

## Security & Configuration Tips
Do not commit real API keys, tokens, chat logs, or workspace data. Keep local secrets in `~/.nanobot/config.json` and use sanitized examples in docs and tests. If you change authentication, network access, or other safety-sensitive behavior, update `README.md` or `SECURITY.md` in the same PR.
- If a change affects user-visible behavior, commands, workflows, or contributor conventions, update both `README.md`, `README_ZH.md` and `AGENTS.md` in the same patch so runtime docs and repo rules stay aligned.
- `gateway.admin` controls the built-in per-instance admin page. Keep it disabled by default unless explicitly configured, require a non-empty `authKey` when enabled, and treat `/admin` as unreachable when the switch is off.
- `gateway.status` controls the optional HTTP status endpoint for Star-Office-UI-style dashboards. Keep it disabled by default, serve it from the same gateway process at `/status`, return `404` when off, and require `Authorization: Bearer <authKey>` when `authKey` is configured.
- `gateway.status.push` may optionally push status directly to Star-Office-UI over HTTP. Keep `mode=guest` as the default using `join-agent` / `agent-push` with a required `joinKey`, and support `mode=main` for driving the built-in main `/set_state` contract without a `joinKey`. Treat push settings as hot-reloadable runtime config.
- The built-in admin page edits the active instance config plus the started process's runtime workspace/persona files. Do not silently retarget it to some other workspace outside the current instance context.
- The built-in admin page should also expose a slash-command reference page for the currently supported chat commands, including aliases and concrete usage examples.
- The admin slash-command reference should use a split layout: command list on the left, selected-command details on the right, and stack vertically on narrow screens.
- The persona detail editor should include locale-backed inline explanations for each editable file block, not just the raw filename.
- Admin UI strings must come from `nanobot/locales/en.json` and `nanobot/locales/zh.json`, not hardcoded dictionaries in `nanobot/gateway/admin.py`. Keep Chinese as the default admin locale, allow English switching, and preserve the visual config editor plus raw JSON fallback.
- Visual admin config fields should include locale-backed hover descriptions for every exposed option, so operators can inspect field semantics without leaving the page.
- Admin config saves should force-reload hot-reloadable runtime settings for the current instance where supported, and every visual field should be marked directly in the UI as either hot-reloadable or restart-required.
- The admin visual config should expose `gateway.status.enabled` / `gateway.status.authKey` plus `gateway.status.push.*` for Star-Office-UI integration, `tools.exec.*` for shell-tool runtime control, and common single-instance channel credential cards for `whatsapp`, `telegram`, `discord`, `feishu`, `dingtalk`, `slack`, `qq`, `matrix`, `weixin`, and `wecom`. If a channel is already using `instances`, keep it read-only in the visual editor and preserve raw JSON as the edit path.
- The built-in admin page may also expose a dedicated Weixin QR-login helper for the current instance. Keep it scoped to the active `channels.weixin` config/state file, and make the UI clear that a non-empty `channels.weixin.token` in `config.json` still takes precedence over saved QR-login state at runtime.
- The admin visual config should expose a dedicated Memorix MCP section for `tools.mcpServers.memorix` with locale-backed labels/tooltips and hot-reload badges; leave generic multi-server MCP editing in the raw JSON fallback.

## Chat Commands & Skills
- Slash commands are handled in `nanobot/agent/loop.py`; keep parsing logic there instead of scattering command behavior across channels.
- When a slash command changes user-visible wording, update both `nanobot/locales/en.json` and `nanobot/locales/zh.json`.
- If a slash command should appear in Telegram's native command menu, also update `nanobot/channels/telegram.py`.
- `/skill` currently supports `search`, `install`, `uninstall`, `list`, and `update`. Keep subcommand dispatch in `nanobot/agent/loop.py`.
- `/mcp` supports the default `list` behavior (and explicit `/mcp list`) to show configured MCP servers and registered MCP tools.
- `/status` should return plain-text runtime info for the active session and stay wired into `/help` plus Telegram's command menu/localization coverage.
- `nanobot serve` starts the built-in OpenAI-compatible API. Keep it local-bind by default via `api.host` / `api.port`, reuse the fixed session `api:default`, require exactly one `user` message per request, and keep `stream=true` unsupported unless the API contract is deliberately expanded.
- Agent runtime config should be hot-reloaded from the active `config.json` for safe in-process fields such as `tools.mcpServers`, `tools.web.*`, `tools.exec.*`, `tools.imageGen.*`, `tools.restrictToWorkspace`, `agents.defaults.workspace`, `agents.defaults.model`, `agents.defaults.maxToolIterations`, `agents.defaults.contextWindowTokens`, `agents.defaults.maxTokens`, `agents.defaults.temperature`, `agents.defaults.reasoningEffort`, `agents.defaults.timezone`, `channels.sendProgress`, `channels.sendToolHints`, `channels.sendMaxRetries`, and `channels.voiceReply.*`. Channel connection settings and provider credentials still require a restart.
- `agents.defaults.providerPool` may define an ordered list of provider targets for `failover` or `round_robin` routing. Keep credentials under `providers.*`, let `providerPool` take precedence over `agents.defaults.provider` when non-empty, and treat pool changes as restart-required routing changes.
- The admin visual config should expose `agents.defaults.providerPool.strategy` and `agents.defaults.providerPool.targets` with locale-backed labels/tooltips, a row-based target editor with add/remove/reorder controls, and restart-required badges; leaving all target rows blank should remove the pool from saved config.
- The admin visual config may also expose common `providers.*` credential blocks such as `openrouter`, `openai`, `anthropic`, `deepseek`, `custom`, `ollama`, and `vllm`; group them into locale-backed collapsible provider cards with safe summary chips, mark them clearly as restart-required, and leave less common providers in raw JSON fallback.
- Long tool-heavy turns should compact older tool outputs on demand before the next model call so system prompt memory and recent working context are less likely to fall out of the active context window.
- Reserved `memory.user.mem0` config is schema-only for now and must not enable a runtime backend by itself. Prefer explicit `provider`, `apiKey`, `url`, `model`, and `headers` fields in `llm`, `embedder`, and `vectorStore`, and use `config` only for provider-specific extras.
- The admin visual config may expose reserved Mem0 fields for `memory.user.mem0`, including common `provider`/`apiKey`/`url`/`model` inputs and JSON textareas for `headers`/`config`/`metadata`, but it must keep the same contract: configuration-only, no implicit runtime enablement.
- nanobot does not expose local files over HTTP. If a feature needs a public URL for local files, provide your own static file server and point config such as `mediaBaseUrl` at it.
- Generated screenshots, downloads, and other temporary user-delivery artifacts should be written under `workspace/out`, not the workspace root. Treat that as the generic delivery-artifact root for tools, MCP servers, and skills.
- QQ outbound media can send remote rich-media URLs directly. For local QQ media under `workspace/out`, use direct `file_data` upload only; do not rely on URL fallback for local files. Supported local QQ rich media are images, `.mp4` video, and `.silk` voice.
- QQ outbound send paths should raise on final delivery failure so `ChannelManager` can apply `channels.sendMaxRetries` consistently.
- `channels.voiceReply` currently adds TTS attachments on supported outbound channels such as Telegram, and QQ when the configured TTS endpoint returns `silk`. Supported providers are `openai`, `edge`, and `sovits`. Preserve plain-text fallback when QQ voice requirements are not met.
- Voice replies should follow the active session persona. Build TTS style instructions from the resolved persona's prompt files, and allow optional persona-local overrides from `VOICE.json` under the persona workspace (`<workspace>/VOICE.json` for default, `<workspace>/personas/<name>/VOICE.json` for custom personas).
- `channels.voiceReply.url` may override the TTS endpoint independently of the chat model provider. When omitted, fall back to the active conversation provider URL. Keep `apiBase` accepted as a compatibility alias. Persona `VOICE.json` may also override provider-specific fields such as Edge voice/rate/volume or GPT-SoVITS reference-audio settings.
- `/skill search` queries `https://lightmake.site/api/skills` directly with SkillHub-compatible query params (`page`, `pageSize`, `sortBy`, `order`, `keyword`) and does not require Node.js.
- `/skill` shells out to `npx clawhub@latest` for `install`, `list`, and `update`; those subcommands still require Node.js/`npx` at runtime.
- Keep ClawHub global options first when shelling out: `--workdir <workspace> --no-input ...`.
- `/skill uninstall` is local workspace cleanup, not a ClawHub subprocess call. Remove `<workspace>/skills/<slug>` and best-effort prune `<workspace>/.clawhub/lock.json`.
- Treat empty `/skill search` output as a user-visible "no results" case rather than a silent success. Surface npm/registry failures directly to the user.
- Never hardcode `~/.nanobot/workspace` for skill installation or lookup. Use the active runtime workspace from config or `--workspace`.
- The implicit default workspace is `<config-dir>/workspace`. If `agents.defaults.workspace` is empty, keep workspace resolution tied to the active config path instead of a separate global default.
- Workspace skills in `<workspace>/skills/` take precedence over built-in skills with the same directory name.
- Built-in skills now include `translate`, `living-together`, `emotional-companion`, and `memorix`. Keep them aligned with the existing skill loader and current built-in tool surface instead of importing NanoMate-only runtime assumptions.
- File memory now also writes structured archive sidecars under `<persona-workspace>/memory/archive/`. Prefer `history_search` / `history_expand` for archived conversation recall, and keep `HISTORY.md` grep as a fallback path.
- When Memorix MCP tools are connected, auto-load the built-in `memorix` skill and call `memorix_session_start` with the active workspace as `projectRoot` once per runtime MCP connection and chat session. Keep Memorix scoped to workspace/code memory instead of user-profile memory.
- `nanobot persona import-st-card <file>` imports a SillyTavern character card into `<workspace>/personas/<name>/` by generating `SOUL.md`, `USER.md`, `memory/`, and persona-local metadata under `.nanobot/`.
- `nanobot persona import-st-preset <file> --persona <name>` imports a SillyTavern preset into an existing persona by generating `STYLE.md` plus `.nanobot/st_preset.json`.
- `nanobot persona import-st-worldinfo <file> --persona <name>` imports SillyTavern world info into an existing persona by generating `LORE.md` plus `.nanobot/st_world_info.json`.
- Persona workspaces may include optional `STYLE.md` and `LORE.md`; when present, runtime context loading should include them after `SOUL.md` and `USER.md`.
- Persona-local `.nanobot/st_manifest.json` may define `response_filter_tags`. Apply those tags only to final user-visible output; preserve the unfiltered assistant content in saved session history.
- Persona-local `.nanobot/st_manifest.json` may also define `reference_image` and `reference_images`. When `tools.imageGen.enabled` is true, the `image_gen` tool should resolve `__default__` and `__default__:scene` against that manifest and save generated output under `workspace/out/image_gen`.
- The local WhatsApp bridge should honor standard proxy environment variables (`https_proxy`, `http_proxy`, `all_proxy`, including SOCKS5 URLs) because bridge deployments may run behind proxies.

## Multi-Instance Channel Notes
The repository supports multi-instance channel configs through `channels.<name>.instances`. Each
instance must define a unique `name`, and runtime routing uses `channel/name` rather than
`channel:name`.

- Supported multi-instance channels currently include `whatsapp`, `telegram`, `discord`,
  `feishu`, `mochat`, `dingtalk`, `slack`, `email`, `qq`, `matrix`, and `wecom`.
- Keep backward compatibility with single-instance configs when touching channel schema or docs.
- If a channel persists local runtime state, isolate it per instance instead of sharing one global
  directory.
- `matrix` instances should keep separate sync/encryption stores.
- `mochat` instances should keep separate cursor/runtime state.
- `whatsapp` multi-instance means multiple bridge processes, usually with different `bridgeUrl`,
  `BRIDGE_PORT`, and `AUTH_DIR` values.
