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
- If a change affects user-visible behavior, commands, workflows, or contributor conventions, update both `README.md` and `AGENTS.md` in the same patch so runtime docs and repo rules stay aligned.

## Chat Commands & Skills
- Slash commands are handled in `nanobot/agent/loop.py`; keep parsing logic there instead of scattering command behavior across channels.
- When a slash command changes user-visible wording, update both `nanobot/locales/en.json` and `nanobot/locales/zh.json`.
- If a slash command should appear in Telegram's native command menu, also update `nanobot/channels/telegram.py`.
- `/skill` currently supports `search`, `install`, `uninstall`, `list`, and `update`. Keep subcommand dispatch in `nanobot/agent/loop.py`.
- `/mcp` supports the default `list` behavior (and explicit `/mcp list`) to show configured MCP servers and registered MCP tools.
- `/status` should return plain-text runtime info for the active session and stay wired into `/help` plus Telegram's command menu/localization coverage.
- Agent runtime config should be hot-reloaded from the active `config.json` for safe in-process fields such as `tools.mcpServers`, `tools.web.*`, `tools.exec.*`, `tools.restrictToWorkspace`, `agents.defaults.model`, `agents.defaults.maxToolIterations`, `agents.defaults.contextWindowTokens`, `agents.defaults.maxTokens`, `agents.defaults.temperature`, `agents.defaults.reasoningEffort`, `channels.sendProgress`, `channels.sendToolHints`, and `channels.voiceReply.*`. Channel connection settings and provider credentials still require a restart.
- nanobot does not expose local files over HTTP. If a feature needs a public URL for local files, provide your own static file server and point config such as `mediaBaseUrl` at it.
- Generated screenshots, downloads, and other temporary user-delivery artifacts should be written under `workspace/out`, not the workspace root. Treat that as the generic delivery-artifact root for tools, MCP servers, and skills.
- QQ outbound media can send remote rich-media URLs directly. For local QQ media under `workspace/out`, use direct `file_data` upload only; do not rely on URL fallback for local files. Supported local QQ rich media are images, `.mp4` video, and `.silk` voice.
- `channels.voiceReply` currently adds TTS attachments on supported outbound channels such as Telegram, and QQ when the configured TTS endpoint returns `silk`. Preserve plain-text fallback when QQ voice requirements are not met.
- Voice replies should follow the active session persona. Build TTS style instructions from the resolved persona's prompt files, and allow optional persona-local overrides from `VOICE.json` under the persona workspace (`<workspace>/VOICE.json` for default, `<workspace>/personas/<name>/VOICE.json` for custom personas).
- `channels.voiceReply.url` may override the TTS endpoint independently of the chat model provider. When omitted, fall back to the active conversation provider URL. Keep `apiBase` accepted as a compatibility alias.
- `/skill search` queries `https://lightmake.site/api/skills` directly with SkillHub-compatible query params (`page`, `pageSize`, `sortBy`, `order`, `keyword`) and does not require Node.js.
- `/skill` shells out to `npx clawhub@latest` for `install`, `list`, and `update`; those subcommands still require Node.js/`npx` at runtime.
- Keep ClawHub global options first when shelling out: `--workdir <workspace> --no-input ...`.
- `/skill uninstall` is local workspace cleanup, not a ClawHub subprocess call. Remove `<workspace>/skills/<slug>` and best-effort prune `<workspace>/.clawhub/lock.json`.
- Treat empty `/skill search` output as a user-visible "no results" case rather than a silent success. Surface npm/registry failures directly to the user.
- Never hardcode `~/.nanobot/workspace` for skill installation or lookup. Use the active runtime workspace from config or `--workspace`.
- Workspace skills in `<workspace>/skills/` take precedence over built-in skills with the same directory name.

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
