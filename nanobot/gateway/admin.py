"""Built-in admin UI for per-instance config and persona editing."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from aiohttp import web

from nanobot.agent.i18n import language_label, normalize_language_code
from nanobot.agent.i18n import text as i18n_text
from nanobot.agent.personas import (
    DEFAULT_PERSONA,
    PERSONA_METADATA_DIRNAME,
    PERSONA_ST_MANIFEST_FILENAME,
    PERSONA_VOICE_FILENAME,
    list_personas,
    normalize_persona_name,
    persona_workspace,
    personas_root,
    resolve_persona_name,
)
from nanobot.config.loader import _migrate_config, load_config
from nanobot.config.schema import Config
from nanobot.utils.helpers import ensure_dir

_ADMIN_COOKIE = "nanobot_admin_session"
_ADMIN_LANG_COOKIE = "nanobot_admin_lang"
_ADMIN_COOKIE_TTL_S = 12 * 60 * 60
_ADMIN_LANG_COOKIE_TTL_S = 365 * 24 * 60 * 60
_DEFAULT_ADMIN_LANG = "zh"
_ADMIN_CONFIG_PATH_KEY = web.AppKey("admin_config_path", Path)
_ADMIN_WORKSPACE_KEY = web.AppKey("admin_workspace_path", Path)
_ADMIN_RELOAD_RUNTIME_KEY = web.AppKey("admin_reload_runtime", object)
_MEMORIX_MCP_SERVER_NAME = "memorix"
_MEMORIX_MCP_DEFAULT_COMMAND = "memorix"
_MEMORIX_MCP_DEFAULT_ARGS = ("serve",)
_MEMORIX_MCP_DEFAULT_TIMEOUT = 60


@dataclass(frozen=True)
class ConfigFieldSpec:
    """Renderable visual-config field."""

    name: str
    path: tuple[str, ...]
    kind: str
    label_key: str
    hint_key: str | None = None
    options: tuple[str, ...] = ()
    rows: int = 4
    placeholder: str = ""
    restart_required: bool = False


@dataclass(frozen=True)
class CommandDocSpec:
    """Renderable slash-command documentation entry."""

    command: str
    description_keys: tuple[str, ...]
    usage_lines: tuple[str, ...] = ()
    usage_text_key: str | None = None
    aliases: tuple[str, ...] = ()
    note_key: str | None = None


_CONFIG_FIELDS = (
    ConfigFieldSpec(
        "agents_defaults_workspace",
        ("agents", "defaults", "workspace"),
        "text",
        "admin_config_agents_workspace_label",
        "admin_config_agents_workspace_hint",
        placeholder="",
    ),
    ConfigFieldSpec(
        "agents_defaults_model",
        ("agents", "defaults", "model"),
        "text",
        "admin_config_agents_model_label",
        placeholder="openrouter/openai/gpt-4o-mini",
    ),
    ConfigFieldSpec(
        "agents_defaults_provider",
        ("agents", "defaults", "provider"),
        "text",
        "admin_config_agents_provider_label",
        "admin_config_agents_provider_hint",
        placeholder="auto",
        restart_required=True,
    ),
    ConfigFieldSpec(
        "agents_defaults_max_tokens",
        ("agents", "defaults", "maxTokens"),
        "int",
        "admin_config_agents_max_tokens_label",
    ),
    ConfigFieldSpec(
        "agents_defaults_context_window_tokens",
        ("agents", "defaults", "contextWindowTokens"),
        "int",
        "admin_config_agents_context_window_label",
    ),
    ConfigFieldSpec(
        "agents_defaults_temperature",
        ("agents", "defaults", "temperature"),
        "float",
        "admin_config_agents_temperature_label",
    ),
    ConfigFieldSpec(
        "agents_defaults_max_tool_iterations",
        ("agents", "defaults", "maxToolIterations"),
        "int",
        "admin_config_agents_max_tool_iterations_label",
    ),
    ConfigFieldSpec(
        "agents_defaults_reasoning_effort",
        ("agents", "defaults", "reasoningEffort"),
        "select",
        "admin_config_agents_reasoning_effort_label",
        options=("", "low", "medium", "high"),
    ),
    ConfigFieldSpec(
        "agents_defaults_timezone",
        ("agents", "defaults", "timezone"),
        "text",
        "admin_config_agents_timezone_label",
        placeholder="Asia/Shanghai",
    ),
    ConfigFieldSpec(
        "gateway_host",
        ("gateway", "host"),
        "text",
        "admin_config_gateway_host_label",
        placeholder="0.0.0.0",
        restart_required=True,
    ),
    ConfigFieldSpec(
        "gateway_port",
        ("gateway", "port"),
        "int",
        "admin_config_gateway_port_label",
        restart_required=True,
    ),
    ConfigFieldSpec(
        "gateway_heartbeat_enabled",
        ("gateway", "heartbeat", "enabled"),
        "bool",
        "admin_config_gateway_heartbeat_enabled_label",
    ),
    ConfigFieldSpec(
        "gateway_heartbeat_interval_s",
        ("gateway", "heartbeat", "intervalS"),
        "int",
        "admin_config_gateway_heartbeat_interval_label",
    ),
    ConfigFieldSpec(
        "gateway_heartbeat_keep_recent_messages",
        ("gateway", "heartbeat", "keepRecentMessages"),
        "int",
        "admin_config_gateway_heartbeat_keep_recent_label",
        restart_required=True,
    ),
    ConfigFieldSpec(
        "gateway_admin_enabled",
        ("gateway", "admin", "enabled"),
        "bool",
        "admin_config_gateway_admin_enabled_label",
    ),
    ConfigFieldSpec(
        "gateway_admin_auth_key",
        ("gateway", "admin", "authKey"),
        "text",
        "admin_config_gateway_admin_auth_key_label",
    ),
    ConfigFieldSpec(
        "tools_restrict_to_workspace",
        ("tools", "restrictToWorkspace"),
        "bool",
        "admin_config_tools_restrict_to_workspace_label",
    ),
    ConfigFieldSpec(
        "tools_web_proxy",
        ("tools", "web", "proxy"),
        "text",
        "admin_config_web_proxy_label",
        placeholder="socks5://127.0.0.1:1080",
    ),
    ConfigFieldSpec(
        "tools_web_search_provider",
        ("tools", "web", "search", "provider"),
        "select",
        "admin_config_web_search_provider_label",
        options=("brave", "searxng"),
    ),
    ConfigFieldSpec(
        "tools_web_search_api_key",
        ("tools", "web", "search", "apiKey"),
        "text",
        "admin_config_web_search_api_key_label",
    ),
    ConfigFieldSpec(
        "tools_web_search_base_url",
        ("tools", "web", "search", "baseUrl"),
        "text",
        "admin_config_web_search_base_url_label",
        "admin_config_web_search_base_url_hint",
        placeholder="http://localhost:8080",
    ),
    ConfigFieldSpec(
        "tools_web_search_max_results",
        ("tools", "web", "search", "maxResults"),
        "int",
        "admin_config_web_search_max_results_label",
    ),
    ConfigFieldSpec(
        "tools_image_gen_enabled",
        ("tools", "imageGen", "enabled"),
        "bool",
        "admin_config_image_enabled_label",
    ),
    ConfigFieldSpec(
        "tools_image_gen_api_key",
        ("tools", "imageGen", "apiKey"),
        "text",
        "admin_config_image_api_key_label",
    ),
    ConfigFieldSpec(
        "tools_image_gen_base_url",
        ("tools", "imageGen", "baseUrl"),
        "text",
        "admin_config_image_base_url_label",
        placeholder="https://api.openai.com/v1",
    ),
    ConfigFieldSpec(
        "tools_image_gen_model",
        ("tools", "imageGen", "model"),
        "text",
        "admin_config_image_model_label",
        placeholder="gpt-image-1",
    ),
    ConfigFieldSpec(
        "tools_image_gen_proxy",
        ("tools", "imageGen", "proxy"),
        "text",
        "admin_config_image_proxy_label",
        placeholder="http://127.0.0.1:7890",
    ),
    ConfigFieldSpec(
        "tools_image_gen_timeout",
        ("tools", "imageGen", "timeout"),
        "int",
        "admin_config_image_timeout_label",
    ),
    ConfigFieldSpec(
        "tools_image_gen_reference_image",
        ("tools", "imageGen", "referenceImage"),
        "text",
        "admin_config_image_reference_image_label",
        placeholder="__default__",
    ),
    ConfigFieldSpec(
        "memory_user_shadow_write_mem0",
        ("memory", "user", "shadowWriteMem0"),
        "bool",
        "admin_config_mem0_shadow_write_label",
    ),
    ConfigFieldSpec(
        "memory_user_mem0_mode",
        ("memory", "user", "mem0", "mode"),
        "select",
        "admin_config_mem0_mode_label",
        options=("embedded",),
    ),
    ConfigFieldSpec(
        "memory_user_mem0_llm_provider",
        ("memory", "user", "mem0", "llm", "provider"),
        "text",
        "admin_config_mem0_llm_provider_label",
        placeholder="openai",
    ),
    ConfigFieldSpec(
        "memory_user_mem0_llm_api_key",
        ("memory", "user", "mem0", "llm", "apiKey"),
        "text",
        "admin_config_mem0_llm_api_key_label",
    ),
    ConfigFieldSpec(
        "memory_user_mem0_llm_url",
        ("memory", "user", "mem0", "llm", "url"),
        "text",
        "admin_config_mem0_llm_url_label",
        placeholder="https://api.mem0.ai/v1",
    ),
    ConfigFieldSpec(
        "memory_user_mem0_llm_model",
        ("memory", "user", "mem0", "llm", "model"),
        "text",
        "admin_config_mem0_llm_model_label",
        placeholder="gpt-4.1-mini",
    ),
    ConfigFieldSpec(
        "memory_user_mem0_llm_headers",
        ("memory", "user", "mem0", "llm", "headers"),
        "json",
        "admin_config_mem0_llm_headers_label",
        rows=5,
    ),
    ConfigFieldSpec(
        "memory_user_mem0_llm_config",
        ("memory", "user", "mem0", "llm", "config"),
        "json",
        "admin_config_mem0_llm_config_label",
        rows=6,
    ),
    ConfigFieldSpec(
        "memory_user_mem0_embedder_provider",
        ("memory", "user", "mem0", "embedder", "provider"),
        "text",
        "admin_config_mem0_embedder_provider_label",
        placeholder="openai",
    ),
    ConfigFieldSpec(
        "memory_user_mem0_embedder_api_key",
        ("memory", "user", "mem0", "embedder", "apiKey"),
        "text",
        "admin_config_mem0_embedder_api_key_label",
    ),
    ConfigFieldSpec(
        "memory_user_mem0_embedder_url",
        ("memory", "user", "mem0", "embedder", "url"),
        "text",
        "admin_config_mem0_embedder_url_label",
        placeholder="https://api.mem0.ai/v1",
    ),
    ConfigFieldSpec(
        "memory_user_mem0_embedder_model",
        ("memory", "user", "mem0", "embedder", "model"),
        "text",
        "admin_config_mem0_embedder_model_label",
        placeholder="text-embedding-3-small",
    ),
    ConfigFieldSpec(
        "memory_user_mem0_embedder_headers",
        ("memory", "user", "mem0", "embedder", "headers"),
        "json",
        "admin_config_mem0_embedder_headers_label",
        rows=5,
    ),
    ConfigFieldSpec(
        "memory_user_mem0_embedder_config",
        ("memory", "user", "mem0", "embedder", "config"),
        "json",
        "admin_config_mem0_embedder_config_label",
        rows=6,
    ),
    ConfigFieldSpec(
        "memory_user_mem0_vector_store_provider",
        ("memory", "user", "mem0", "vectorStore", "provider"),
        "text",
        "admin_config_mem0_vector_store_provider_label",
        placeholder="qdrant",
    ),
    ConfigFieldSpec(
        "memory_user_mem0_vector_store_api_key",
        ("memory", "user", "mem0", "vectorStore", "apiKey"),
        "text",
        "admin_config_mem0_vector_store_api_key_label",
    ),
    ConfigFieldSpec(
        "memory_user_mem0_vector_store_url",
        ("memory", "user", "mem0", "vectorStore", "url"),
        "text",
        "admin_config_mem0_vector_store_url_label",
        placeholder="https://qdrant.example.com",
    ),
    ConfigFieldSpec(
        "memory_user_mem0_vector_store_model",
        ("memory", "user", "mem0", "vectorStore", "model"),
        "text",
        "admin_config_mem0_vector_store_model_label",
        placeholder="",
    ),
    ConfigFieldSpec(
        "memory_user_mem0_vector_store_headers",
        ("memory", "user", "mem0", "vectorStore", "headers"),
        "json",
        "admin_config_mem0_vector_store_headers_label",
        rows=5,
    ),
    ConfigFieldSpec(
        "memory_user_mem0_vector_store_config",
        ("memory", "user", "mem0", "vectorStore", "config"),
        "json",
        "admin_config_mem0_vector_store_config_label",
        rows=6,
    ),
    ConfigFieldSpec(
        "memory_user_mem0_metadata",
        ("memory", "user", "mem0", "metadata"),
        "json",
        "admin_config_mem0_metadata_label",
        rows=6,
    ),
    ConfigFieldSpec(
        "tools_mcp_memorix_enabled",
        (),
        "bool",
        "admin_config_memorix_enabled_label",
    ),
    ConfigFieldSpec(
        "tools_mcp_memorix_type",
        ("tools", "mcpServers", "memorix", "type"),
        "select",
        "admin_config_memorix_type_label",
        options=("", "stdio", "streamableHttp", "sse"),
    ),
    ConfigFieldSpec(
        "tools_mcp_memorix_command",
        ("tools", "mcpServers", "memorix", "command"),
        "text",
        "admin_config_memorix_command_label",
        "admin_config_memorix_command_hint",
        placeholder="memorix",
    ),
    ConfigFieldSpec(
        "tools_mcp_memorix_args",
        ("tools", "mcpServers", "memorix", "args"),
        "csv",
        "admin_config_memorix_args_label",
        "admin_config_memorix_args_hint",
        placeholder="serve",
    ),
    ConfigFieldSpec(
        "tools_mcp_memorix_url",
        ("tools", "mcpServers", "memorix", "url"),
        "text",
        "admin_config_memorix_url_label",
        "admin_config_memorix_url_hint",
        placeholder="http://127.0.0.1:3211/mcp",
    ),
    ConfigFieldSpec(
        "tools_mcp_memorix_tool_timeout",
        ("tools", "mcpServers", "memorix", "toolTimeout"),
        "int",
        "admin_config_memorix_tool_timeout_label",
    ),
    ConfigFieldSpec(
        "channels_send_progress",
        ("channels", "sendProgress"),
        "bool",
        "admin_config_channel_send_progress_label",
    ),
    ConfigFieldSpec(
        "channels_send_tool_hints",
        ("channels", "sendToolHints"),
        "bool",
        "admin_config_channel_send_tool_hints_label",
    ),
    ConfigFieldSpec(
        "channels_send_max_retries",
        ("channels", "sendMaxRetries"),
        "int",
        "admin_config_channel_send_max_retries_label",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_enabled",
        ("channels", "voiceReply", "enabled"),
        "bool",
        "admin_config_voice_enabled_label",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_channels",
        ("channels", "voiceReply", "channels"),
        "csv",
        "admin_config_voice_channels_label",
        "admin_config_voice_channels_hint",
        placeholder="telegram, qq",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_provider",
        ("channels", "voiceReply", "provider"),
        "select",
        "admin_config_voice_provider_label",
        options=("openai", "edge", "sovits"),
    ),
    ConfigFieldSpec(
        "channels_voice_reply_model",
        ("channels", "voiceReply", "model"),
        "text",
        "admin_config_voice_model_label",
        placeholder="gpt-4o-mini-tts",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_voice",
        ("channels", "voiceReply", "voice"),
        "text",
        "admin_config_voice_voice_label",
        placeholder="alloy",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_instructions",
        ("channels", "voiceReply", "instructions"),
        "textarea",
        "admin_config_voice_instructions_label",
        rows=5,
    ),
    ConfigFieldSpec(
        "channels_voice_reply_speed",
        ("channels", "voiceReply", "speed"),
        "float",
        "admin_config_voice_speed_label",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_response_format",
        ("channels", "voiceReply", "responseFormat"),
        "select",
        "admin_config_voice_response_format_label",
        options=("mp3", "opus", "aac", "flac", "wav", "pcm", "silk"),
    ),
    ConfigFieldSpec(
        "channels_voice_reply_api_key",
        ("channels", "voiceReply", "apiKey"),
        "text",
        "admin_config_voice_api_key_label",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_api_base",
        ("channels", "voiceReply", "apiBase"),
        "text",
        "admin_config_voice_api_base_label",
        "admin_config_voice_api_base_hint",
        placeholder="https://api.openai.com/v1",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_edge_voice",
        ("channels", "voiceReply", "edgeVoice"),
        "text",
        "admin_config_edge_voice_label",
        placeholder="zh-CN-XiaoxiaoNeural",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_edge_rate",
        ("channels", "voiceReply", "edgeRate"),
        "text",
        "admin_config_edge_rate_label",
        placeholder="+0%",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_edge_volume",
        ("channels", "voiceReply", "edgeVolume"),
        "text",
        "admin_config_edge_volume_label",
        placeholder="+0%",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_sovits_api_url",
        ("channels", "voiceReply", "sovitsApiUrl"),
        "text",
        "admin_config_sovits_api_url_label",
        placeholder="http://127.0.0.1:9880",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_sovits_refer_wav_path",
        ("channels", "voiceReply", "sovitsReferWavPath"),
        "text",
        "admin_config_sovits_refer_wav_path_label",
        placeholder="workspace/personas/Aria/reference.wav",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_sovits_prompt_text",
        ("channels", "voiceReply", "sovitsPromptText"),
        "textarea",
        "admin_config_sovits_prompt_text_label",
        rows=4,
    ),
    ConfigFieldSpec(
        "channels_voice_reply_sovits_prompt_language",
        ("channels", "voiceReply", "sovitsPromptLanguage"),
        "text",
        "admin_config_sovits_prompt_language_label",
        placeholder="zh",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_sovits_text_language",
        ("channels", "voiceReply", "sovitsTextLanguage"),
        "text",
        "admin_config_sovits_text_language_label",
        placeholder="zh",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_sovits_cut_punc",
        ("channels", "voiceReply", "sovitsCutPunc"),
        "text",
        "admin_config_sovits_cut_punc_label",
        placeholder="，。",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_sovits_top_k",
        ("channels", "voiceReply", "sovitsTopK"),
        "int",
        "admin_config_sovits_top_k_label",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_sovits_top_p",
        ("channels", "voiceReply", "sovitsTopP"),
        "float",
        "admin_config_sovits_top_p_label",
    ),
    ConfigFieldSpec(
        "channels_voice_reply_sovits_temperature",
        ("channels", "voiceReply", "sovitsTemperature"),
        "float",
        "admin_config_sovits_temperature_label",
    ),
)
_CONFIG_FIELD_MAP = {field.name: field for field in _CONFIG_FIELDS}
_COMMAND_DOCS = (
    CommandDocSpec(
        command="/help",
        description_keys=("cmd_help",),
        usage_lines=("/help",),
    ),
    CommandDocSpec(
        command="/status",
        description_keys=("cmd_status",),
        usage_lines=("/status",),
    ),
    CommandDocSpec(
        command="/new",
        description_keys=("cmd_new",),
        usage_lines=("/new",),
    ),
    CommandDocSpec(
        command="/lang",
        description_keys=("cmd_lang_current", "cmd_lang_list", "cmd_lang_set"),
        usage_lines=(
            "/lang current",
            "/lang list",
            "/lang set <en|zh>",
        ),
        aliases=("/language",),
        note_key="admin_commands_note_lang",
    ),
    CommandDocSpec(
        command="/persona",
        description_keys=("cmd_persona_current", "cmd_persona_list", "cmd_persona_set"),
        usage_lines=(
            "/persona current",
            "/persona list",
            "/persona set <name>",
        ),
        note_key="admin_commands_note_persona",
    ),
    CommandDocSpec(
        command="/skill",
        description_keys=("cmd_skill",),
        usage_text_key="skill_usage",
        note_key="admin_commands_note_skill",
    ),
    CommandDocSpec(
        command="/mcp",
        description_keys=("cmd_mcp",),
        usage_text_key="mcp_usage",
        note_key="admin_commands_note_mcp",
    ),
    CommandDocSpec(
        command="/stop",
        description_keys=("cmd_stop",),
        usage_lines=("/stop",),
        note_key="admin_commands_note_stop",
    ),
    CommandDocSpec(
        command="/restart",
        description_keys=("cmd_restart",),
        usage_lines=("/restart",),
        note_key="admin_commands_note_restart",
    ),
)
_BLANK_AS_NONE_FIELDS = {
    "tools_web_proxy",
    "tools_image_gen_proxy",
    "tools_mcp_memorix_type",
    "channels_voice_reply_speed",
}
_MEMORIX_CONFIG_FIELD_NAMES = {
    "tools_mcp_memorix_enabled",
    "tools_mcp_memorix_type",
    "tools_mcp_memorix_command",
    "tools_mcp_memorix_args",
    "tools_mcp_memorix_url",
    "tools_mcp_memorix_tool_timeout",
}
_CONFIG_SECTIONS = (
    (
        "admin_config_section_agents_title",
        "admin_config_section_agents_desc",
        (
            "agents_defaults_workspace",
            "agents_defaults_model",
            "agents_defaults_provider",
            "agents_defaults_max_tokens",
            "agents_defaults_context_window_tokens",
            "agents_defaults_temperature",
            "agents_defaults_max_tool_iterations",
            "agents_defaults_reasoning_effort",
            "agents_defaults_timezone",
        ),
    ),
    (
        "admin_config_section_gateway_title",
        "admin_config_section_gateway_desc",
        (
            "gateway_host",
            "gateway_port",
            "gateway_heartbeat_enabled",
            "gateway_heartbeat_interval_s",
            "gateway_heartbeat_keep_recent_messages",
            "gateway_admin_enabled",
            "gateway_admin_auth_key",
        ),
    ),
    (
        "admin_config_section_web_title",
        "admin_config_section_web_desc",
        (
            "tools_restrict_to_workspace",
            "tools_web_proxy",
            "tools_web_search_provider",
            "tools_web_search_api_key",
            "tools_web_search_base_url",
            "tools_web_search_max_results",
        ),
    ),
    (
        "admin_config_section_image_title",
        "admin_config_section_image_desc",
        (
            "tools_image_gen_enabled",
            "tools_image_gen_api_key",
            "tools_image_gen_base_url",
            "tools_image_gen_model",
            "tools_image_gen_proxy",
            "tools_image_gen_timeout",
            "tools_image_gen_reference_image",
        ),
    ),
    (
        "admin_config_section_mem0_title",
        "admin_config_section_mem0_desc",
        (
            "memory_user_shadow_write_mem0",
            "memory_user_mem0_mode",
            "memory_user_mem0_llm_provider",
            "memory_user_mem0_llm_api_key",
            "memory_user_mem0_llm_url",
            "memory_user_mem0_llm_model",
            "memory_user_mem0_llm_headers",
            "memory_user_mem0_llm_config",
            "memory_user_mem0_embedder_provider",
            "memory_user_mem0_embedder_api_key",
            "memory_user_mem0_embedder_url",
            "memory_user_mem0_embedder_model",
            "memory_user_mem0_embedder_headers",
            "memory_user_mem0_embedder_config",
            "memory_user_mem0_vector_store_provider",
            "memory_user_mem0_vector_store_api_key",
            "memory_user_mem0_vector_store_url",
            "memory_user_mem0_vector_store_model",
            "memory_user_mem0_vector_store_headers",
            "memory_user_mem0_vector_store_config",
            "memory_user_mem0_metadata",
        ),
    ),
    (
        "admin_config_section_memorix_title",
        "admin_config_section_memorix_desc",
        (
            "tools_mcp_memorix_enabled",
            "tools_mcp_memorix_type",
            "tools_mcp_memorix_command",
            "tools_mcp_memorix_args",
            "tools_mcp_memorix_url",
            "tools_mcp_memorix_tool_timeout",
        ),
    ),
    (
        "admin_config_section_channel_runtime_title",
        "admin_config_section_channel_runtime_desc",
        (
            "channels_send_progress",
            "channels_send_tool_hints",
            "channels_send_max_retries",
        ),
    ),
    (
        "admin_config_section_voice_title",
        "admin_config_section_voice_desc",
        (
            "channels_voice_reply_enabled",
            "channels_voice_reply_channels",
            "channels_voice_reply_provider",
            "channels_voice_reply_model",
            "channels_voice_reply_voice",
            "channels_voice_reply_instructions",
            "channels_voice_reply_speed",
            "channels_voice_reply_response_format",
            "channels_voice_reply_api_key",
            "channels_voice_reply_api_base",
        ),
    ),
    (
        "admin_config_section_edge_title",
        "admin_config_section_edge_desc",
        (
            "channels_voice_reply_edge_voice",
            "channels_voice_reply_edge_rate",
            "channels_voice_reply_edge_volume",
        ),
    ),
    (
        "admin_config_section_sovits_title",
        "admin_config_section_sovits_desc",
        (
            "channels_voice_reply_sovits_api_url",
            "channels_voice_reply_sovits_refer_wav_path",
            "channels_voice_reply_sovits_prompt_text",
            "channels_voice_reply_sovits_prompt_language",
            "channels_voice_reply_sovits_text_language",
            "channels_voice_reply_sovits_cut_punc",
            "channels_voice_reply_sovits_top_k",
            "channels_voice_reply_sovits_top_p",
            "channels_voice_reply_sovits_temperature",
        ),
    ),
)


def register_admin_routes(
    app: web.Application,
    *,
    config_path: Path,
    workspace: Path,
    reload_runtime: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Register built-in admin routes for the current gateway instance."""
    app[_ADMIN_CONFIG_PATH_KEY] = config_path
    app[_ADMIN_WORKSPACE_KEY] = workspace
    if reload_runtime is not None:
        app[_ADMIN_RELOAD_RUNTIME_KEY] = reload_runtime
    app.router.add_get("/admin", _admin_index)
    app.router.add_get("/admin/login", _admin_login_page)
    app.router.add_post("/admin/login", _admin_login_submit)
    app.router.add_post("/admin/logout", _admin_logout)
    app.router.add_get("/admin/config", _admin_config_page)
    app.router.add_post("/admin/config", _admin_config_submit)
    app.router.add_get("/admin/commands", _admin_commands_page)
    app.router.add_get("/admin/personas", _admin_personas_page)
    app.router.add_post("/admin/personas/new", _admin_persona_create)
    app.router.add_get("/admin/personas/{persona:[A-Za-z0-9_-]+}", _admin_persona_page)
    app.router.add_post("/admin/personas/{persona:[A-Za-z0-9_-]+}", _admin_persona_submit)


def update_admin_runtime_workspace(app: web.Application, workspace: Path) -> None:
    """Update the runtime-workspace pointer used by the admin UI."""
    app[_ADMIN_WORKSPACE_KEY] = workspace


def _current_config_path(request: web.Request) -> Path:
    return Path(request.app[_ADMIN_CONFIG_PATH_KEY])


def _runtime_workspace(request: web.Request) -> Path:
    return Path(request.app[_ADMIN_WORKSPACE_KEY])


def _load_current_config(request: web.Request) -> Config:
    return load_config(_current_config_path(request))


def _load_raw_config_data(request: web.Request) -> dict[str, Any]:
    path = _current_config_path(request)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config.json must contain a JSON object.")
    return _migrate_config(data)


def _save_raw_config_data(request: web.Request, data: dict[str, Any]) -> None:
    path = _current_config_path(request)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_pretty_json(data) + "\n", encoding="utf-8")


def _admin_enabled(request: web.Request) -> bool:
    try:
        return bool(_load_current_config(request).gateway.admin.enabled)
    except Exception:
        return False


def _admin_auth_key(request: web.Request) -> str:
    try:
        return (_load_current_config(request).gateway.admin.auth_key or "").strip()
    except Exception:
        return ""


def _require_admin_enabled(request: web.Request) -> None:
    if not _admin_enabled(request):
        raise web.HTTPNotFound()


def _session_signature(auth_key: str, expires_at: int, nonce: str) -> str:
    payload = f"{expires_at}:{nonce}".encode("utf-8")
    return hmac.new(auth_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _build_session_cookie(auth_key: str) -> str:
    expires_at = int(time.time()) + _ADMIN_COOKIE_TTL_S
    nonce = secrets.token_hex(12)
    signature = _session_signature(auth_key, expires_at, nonce)
    return f"{expires_at}:{nonce}:{signature}"


def _is_authenticated(request: web.Request) -> bool:
    auth_key = _admin_auth_key(request)
    if not auth_key:
        return False

    raw = request.cookies.get(_ADMIN_COOKIE, "")
    parts = raw.split(":", 2)
    if len(parts) != 3:
        return False

    expires_at_raw, nonce, signature = parts
    try:
        expires_at = int(expires_at_raw)
    except ValueError:
        return False
    if expires_at < int(time.time()):
        return False

    expected = _session_signature(auth_key, expires_at, nonce)
    return hmac.compare_digest(signature, expected)


def _normalize_next_path(value: str | None) -> str:
    if not isinstance(value, str):
        return "/admin"
    value = value.strip()
    if not value.startswith("/admin"):
        return "/admin"
    return value


def _admin_language(request: web.Request) -> str:
    query_lang = normalize_language_code(request.query.get("lang"))
    if query_lang:
        return query_lang
    cookie_lang = normalize_language_code(request.cookies.get(_ADMIN_LANG_COOKIE))
    if cookie_lang:
        return cookie_lang
    return _DEFAULT_ADMIN_LANG


def _t(request: web.Request, key: str, **kwargs: Any) -> str:
    return i18n_text(_admin_language(request), key, **kwargs)


def _th(request: web.Request, key: str, **kwargs: Any) -> str:
    safe_kwargs = {name: escape(str(value)) for name, value in kwargs.items()}
    return _t(request, key, **safe_kwargs)


def _language_switch_label(code: str, ui_language: str) -> str:
    label = language_label(code, ui_language)
    if "(" in label and label.endswith(")"):
        return label.split("(", 1)[1][:-1]
    return label


def _set_lang_cookie(response: web.StreamResponse, request: web.Request) -> web.StreamResponse:
    response.set_cookie(
        _ADMIN_LANG_COOKIE,
        _admin_language(request),
        max_age=_ADMIN_LANG_COOKIE_TTL_S,
        samesite="Lax",
    )
    return response


def _redirect(request: web.Request, location: str) -> web.HTTPFound:
    response = web.HTTPFound(location)
    _set_lang_cookie(response, request)
    return response


def _require_admin_auth(request: web.Request) -> None:
    _require_admin_enabled(request)
    if _is_authenticated(request):
        return
    destination = quote(str(request.rel_url), safe="/?=&")
    raise _redirect(request, f"/admin/login?next={destination}")


def _query_url(request: web.Request, **updates: str | None) -> str:
    params = dict(request.query)
    for key, value in updates.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    query = urlencode(params)
    return f"{request.path}?{query}" if query else request.path


def _language_switch(request: web.Request) -> str:
    active = _admin_language(request)
    links: list[str] = []
    for code in ("zh", "en"):
        href = escape(_query_url(request, lang=code))
        label = escape(_language_switch_label(code, active))
        css_class = "lang-link active" if code == active else "lang-link"
        links.append(f'<a class="{css_class}" href="{href}">{label}</a>')
    return (
        f'<div class="lang-switch"><span class="muted">{escape(_t(request, "admin_meta_language"))}</span>'
        f'{"".join(links)}</div>'
    )


def _nav_link(request: web.Request, href: str, label_key: str) -> str:
    path = request.path
    if href == "/admin":
        active = path == href
    else:
        active = path == href or path.startswith(f"{href}/")
    css_class = "nav-link active" if active else "nav-link"
    return f'<a class="{css_class}" href="{href}">{escape(_t(request, label_key))}</a>'


def _page(
    *,
    title: str,
    body: str,
    request: web.Request,
    heading: str | None = None,
    flash: str | None = None,
    error: str | None = None,
) -> web.Response:
    heading_text = heading or title
    config_path = escape(str(_current_config_path(request)))
    workspace = escape(str(_runtime_workspace(request)))
    lang = _admin_language(request)
    nav = ""
    if _is_authenticated(request):
        nav = (
            '<nav class="nav">'
            f'{_nav_link(request, "/admin", "admin_nav_overview")}'
            f'{_nav_link(request, "/admin/config", "admin_nav_config")}'
            f'{_nav_link(request, "/admin/commands", "admin_nav_commands")}'
            f'{_nav_link(request, "/admin/personas", "admin_nav_personas")}'
            '<form method="post" action="/admin/logout" class="inline-form">'
            f'<button type="submit" class="ghost nav-link nav-link-button">{escape(_t(request, "admin_nav_logout"))}</button>'
            "</form>"
            "</nav>"
        )

    notices: list[str] = []
    if flash:
        notices.append(f'<div class="notice success">{escape(flash)}</div>')
    if error:
        notices.append(f'<div class="notice error">{escape(error)}</div>')

    html = f"""<!doctype html>
<html lang="{escape(lang)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} · {escape(_t(request, "admin_brand"))}</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #efe9dc;
      --bg-accent: rgba(190, 116, 55, 0.12);
      --panel: rgba(255, 251, 245, 0.92);
      --panel-strong: #fffdf8;
      --panel-soft: rgba(255, 255, 255, 0.56);
      --line: rgba(88, 68, 40, 0.18);
      --line-strong: rgba(88, 68, 40, 0.28);
      --ink: #1d1a15;
      --muted: #6e6354;
      --accent: #0c7a6c;
      --accent-strong: #0a5b51;
      --success: #166534;
      --error: #b42318;
      --shadow: 0 24px 70px rgba(29, 26, 21, 0.12);
      --code-bg: rgba(17, 24, 39, 0.06);
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #171410;
        --bg-accent: rgba(90, 190, 171, 0.12);
        --panel: rgba(26, 22, 17, 0.94);
        --panel-strong: #211c16;
        --panel-soft: rgba(255, 255, 255, 0.04);
        --line: rgba(235, 225, 205, 0.10);
        --line-strong: rgba(235, 225, 205, 0.18);
        --ink: #f6efe4;
        --muted: #b4a995;
        --accent: #7be2d2;
        --accent-strong: #a5fff1;
        --success: #6ee7a7;
        --error: #ff8f82;
        --shadow: 0 28px 80px rgba(0, 0, 0, 0.36);
        --code-bg: rgba(255, 255, 255, 0.06);
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "IBM Plex Sans", "Noto Sans SC", sans-serif;
      line-height: 1.5;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, var(--bg-accent), transparent 30%),
        radial-gradient(circle at right 15%, rgba(12, 122, 108, 0.08), transparent 32%),
        linear-gradient(180deg, rgba(255, 255, 255, 0.08), transparent 40%),
        var(--bg);
    }}
    main {{
      max-width: 1380px;
      margin: 0 auto;
      padding: 28px 18px 56px;
    }}
    .shell {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 28px;
      overflow: hidden;
      box-shadow: var(--shadow);
      backdrop-filter: blur(14px);
    }}
    header {{
      padding: 28px;
      border-bottom: 1px solid var(--line);
      background:
        radial-gradient(circle at top right, rgba(12, 122, 108, 0.16), transparent 36%),
        linear-gradient(135deg, rgba(12, 122, 108, 0.14), transparent 55%),
        linear-gradient(180deg, rgba(255, 255, 255, 0.08), transparent 70%);
    }}
    .header-top {{
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: flex-start;
      flex-wrap: wrap;
    }}
    .header-copy {{
      max-width: 860px;
      display: grid;
      gap: 12px;
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      width: fit-content;
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid rgba(12, 122, 108, 0.20);
      background: rgba(12, 122, 108, 0.08);
      color: var(--accent-strong);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.16em;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 0;
      font-size: 36px;
      line-height: 1;
      letter-spacing: -0.02em;
    }}
    p, li, label, input, textarea, button, select, summary {{
      font-size: 14px;
      line-height: 1.5;
    }}
    a {{
      color: var(--accent-strong);
      text-decoration: none;
    }}
    a:hover {{
      text-decoration: underline;
    }}
    .muted {{ color: var(--muted); }}
    .muted,
    .notice,
    .section-head,
    .section-topline,
    .field,
    .jump-link,
    .jump-link-meta,
    .stat-card,
    .list a,
    .detail-list li,
    .panel-title,
    .nav-link,
    .lang-link,
    strong,
    h1,
    h2 {{
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .page-flow {{
      display: grid;
      gap: 20px;
    }}
    .meta {{
      display: grid;
      gap: 8px;
      min-width: 0;
    }}
    .nav {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      margin-top: 22px;
      padding-top: 18px;
      border-top: 1px solid var(--line);
    }}
    .nav-link {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 40px;
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.08);
      color: var(--ink);
      font-weight: 600;
      text-decoration: none;
      text-align: center;
      white-space: normal;
    }}
    .nav-link:hover {{
      text-decoration: none;
      border-color: rgba(12, 122, 108, 0.35);
      transform: translateY(-1px);
    }}
    .nav-link.active {{
      border-color: rgba(12, 122, 108, 0.35);
      background: rgba(12, 122, 108, 0.14);
      color: var(--accent-strong);
      box-shadow: inset 0 0 0 1px rgba(12, 122, 108, 0.08);
    }}
    .nav-link-button {{
      font: inherit;
    }}
    .lang-switch {{
      display: inline-flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .lang-link {{
      display: inline-flex;
      align-items: center;
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.08);
      color: var(--ink);
      text-decoration: none;
    }}
    .lang-link.active {{
      border-color: rgba(12, 122, 108, 0.35);
      background: rgba(12, 122, 108, 0.14);
      color: var(--accent-strong);
    }}
    .content {{
      padding: 24px;
      display: grid;
      gap: 20px;
    }}
    .card {{
      background: var(--panel-strong);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 20px;
    }}
    .spotlight {{
      background:
        linear-gradient(140deg, rgba(12, 122, 108, 0.10), transparent 46%),
        linear-gradient(180deg, rgba(255, 255, 255, 0.06), transparent 100%),
        var(--panel-strong);
      border-color: rgba(12, 122, 108, 0.16);
    }}
    .hero-grid {{
      display: grid;
      gap: 18px;
      grid-template-columns: minmax(0, 1.35fr) minmax(280px, 0.85fr);
      align-items: start;
    }}
    .hero-grid > *,
    .section-layout > *,
    .grid > *,
    .field-grid > *,
    .editor-grid > * {{
      min-width: 0;
    }}
    .panel-title {{
      margin: 0;
      font-size: 24px;
      line-height: 1.1;
      letter-spacing: -0.02em;
    }}
    .stat-grid {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    }}
    .stat-card {{
      display: grid;
      gap: 6px;
      padding: 14px 16px;
      border-radius: 16px;
      border: 1px solid var(--line);
      background: var(--panel-soft);
    }}
    .stat-card span {{
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .stat-card strong {{
      font-size: 16px;
      line-height: 1.35;
      word-break: break-word;
    }}
    .grid {{
      display: grid;
      gap: 16px;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    }}
    .feature-card {{
      height: 100%;
    }}
    .field-grid {{
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    }}
    .section-layout {{
      display: grid;
      gap: 18px;
      grid-template-columns: minmax(0, 300px) minmax(0, 1fr);
      align-items: start;
    }}
    .sticky-stack {{
      position: sticky;
      top: 18px;
      display: grid;
      gap: 16px;
    }}
    .jump-list {{
      display: grid;
      gap: 8px;
    }}
    .jump-link {{
      display: grid;
      gap: 4px;
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--panel-soft);
      color: var(--ink);
      text-decoration: none;
    }}
    .jump-link:hover {{
      text-decoration: none;
      border-color: rgba(12, 122, 108, 0.35);
      transform: translateY(-1px);
    }}
    .jump-link-top {{
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
      flex-wrap: wrap;
    }}
    .jump-link-index,
    .section-index {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 42px;
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid rgba(12, 122, 108, 0.22);
      background: rgba(12, 122, 108, 0.08);
      color: var(--accent-strong);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.18em;
      text-transform: uppercase;
    }}
    .jump-link-meta {{
      font-size: 12px;
      color: var(--muted);
    }}
    .stack {{
      display: grid;
      gap: 12px;
    }}
    .field {{
      display: grid;
      gap: 8px;
      align-content: start;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--panel-soft);
    }}
    .field.full {{
      grid-column: 1 / -1;
    }}
    .field .label {{
      font-weight: 600;
    }}
    .label-row {{
      display: flex;
      align-items: flex-start;
      gap: 8px;
      width: 100%;
      max-width: 100%;
      flex-wrap: wrap;
    }}
    .tooltip-anchor {{
      position: relative;
      cursor: help;
      outline: none;
      width: 100%;
    }}
    .tooltip-trigger {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 18px;
      height: 18px;
      border-radius: 999px;
      border: 1px solid var(--line-strong);
      color: var(--accent-strong);
      font-size: 11px;
      font-weight: 800;
      background: rgba(12, 122, 108, 0.08);
      flex: 0 0 auto;
    }}
    .tooltip-card {{
      position: absolute;
      left: 0;
      top: calc(100% + 8px);
      min-width: 240px;
      max-width: min(420px, 80vw);
      padding: 12px 14px;
      border-radius: 12px;
      border: 1px solid var(--line-strong);
      background: var(--panel-strong);
      color: var(--ink);
      box-shadow: 0 18px 44px rgba(0, 0, 0, 0.18);
      opacity: 0;
      pointer-events: none;
      transform: translateY(-4px);
      transition: opacity 120ms ease, transform 120ms ease;
      z-index: 20;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .tooltip-anchor:hover .tooltip-card,
    .tooltip-anchor:focus .tooltip-card,
    .tooltip-anchor:focus-within .tooltip-card {{
      opacity: 1;
      pointer-events: auto;
      transform: translateY(0);
    }}
    .hint {{
      color: var(--muted);
      font-size: 13px;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      border: 1px solid var(--line-strong);
      color: var(--muted);
      background: rgba(255, 255, 255, 0.04);
      flex: 0 0 auto;
    }}
    .pill.restart {{
      color: var(--error);
      border-color: rgba(180, 35, 24, 0.28);
      background: rgba(180, 35, 24, 0.08);
    }}
    .pill.hot {{
      color: var(--accent-strong);
      border-color: rgba(12, 122, 108, 0.26);
      background: rgba(12, 122, 108, 0.10);
    }}
    .badge-row {{
      display: inline-flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }}
    .toggle {{
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: 10px;
      align-items: flex-start;
      font-weight: 600;
      width: 100%;
    }}
    .toggle input[type="checkbox"] {{
      margin-top: 2px;
    }}
    input[type="text"],
    input[type="password"],
    input[type="number"],
    textarea,
    select {{
      width: 100%;
      border: 1px solid var(--line-strong);
      border-radius: 12px;
      padding: 10px 12px;
      background: rgba(255, 255, 255, 0.68);
      color: var(--ink);
      font: inherit;
    }}
    @media (prefers-color-scheme: dark) {{
      input[type="text"],
      input[type="password"],
      input[type="number"],
      textarea,
      select {{
        background: rgba(255, 255, 255, 0.04);
      }}
    }}
    textarea {{
      min-height: 160px;
      resize: vertical;
      font-family: "IBM Plex Mono", "Noto Sans Mono", monospace;
    }}
    .json-editor {{
      min-height: 300px;
    }}
    button {{
      appearance: none;
      border: none;
      border-radius: 12px;
      padding: 10px 16px;
      background: var(--accent);
      color: #fff;
      cursor: pointer;
      font-weight: 700;
    }}
    button.ghost {{
      background: transparent;
      color: var(--accent-strong);
      border: 1px solid var(--line-strong);
    }}
    .inline-form {{ display: inline; }}
    .notice {{
      border-radius: 12px;
      padding: 12px 14px;
      border: 1px solid var(--line);
    }}
    .notice.success {{
      color: var(--success);
      border-color: rgba(22, 101, 52, 0.28);
      background: rgba(22, 101, 52, 0.08);
    }}
    .notice.error {{
      color: var(--error);
      border-color: rgba(180, 35, 24, 0.28);
      background: rgba(180, 35, 24, 0.08);
    }}
    .list {{
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 10px;
    }}
    .list a {{
      display: block;
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 16px;
      color: var(--ink);
      background: var(--panel-soft);
      text-decoration: none;
    }}
    .list a:hover {{
      text-decoration: none;
      border-color: rgba(12, 122, 108, 0.35);
    }}
    .list a strong,
    .list a span {{
      display: block;
      min-width: 0;
    }}
    .persona-list {{
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    }}
    .detail-list {{
      margin: 0;
      padding-left: 18px;
      display: grid;
      gap: 8px;
    }}
    code {{
      font-family: "IBM Plex Mono", "Noto Sans Mono", monospace;
      font-size: 13px;
      background: var(--code-bg);
      padding: 2px 6px;
      border-radius: 8px;
      white-space: break-spaces;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    pre {{
      margin: 0;
    }}
    .code-block {{
      padding: 14px 16px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: var(--code-bg);
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: "IBM Plex Mono", "Noto Sans Mono", monospace;
      font-size: 13px;
      line-height: 1.6;
    }}
    .code-block code {{
      padding: 0;
      background: transparent;
      border-radius: 0;
    }}
    summary {{
      cursor: pointer;
      font-weight: 700;
    }}
    .section-head {{
      display: grid;
      gap: 4px;
      margin-bottom: 14px;
    }}
    .section-head h2 {{
      margin: 0;
      font-size: 18px;
    }}
    .section-topline {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
    }}
    .section-card {{
      scroll-margin-top: 24px;
    }}
    .command-browser {{
      display: grid;
      gap: 18px;
      grid-template-columns: minmax(250px, 320px) minmax(0, 1fr);
      align-items: start;
    }}
    .command-sidebar {{
      position: sticky;
      top: 18px;
    }}
    .command-nav {{
      display: grid;
      gap: 10px;
    }}
    .command-nav-item {{
      display: grid;
      gap: 6px;
      padding: 14px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: var(--panel-soft);
      color: var(--ink);
      text-decoration: none;
      transition: border-color 120ms ease, transform 120ms ease, background 120ms ease;
    }}
    .command-nav-item:hover {{
      text-decoration: none;
      border-color: rgba(12, 122, 108, 0.35);
      transform: translateY(-1px);
    }}
    .command-nav-item.active {{
      border-color: rgba(12, 122, 108, 0.35);
      background: rgba(12, 122, 108, 0.12);
      box-shadow: inset 0 0 0 1px rgba(12, 122, 108, 0.08);
    }}
    .command-nav-item code {{
      width: fit-content;
      max-width: 100%;
    }}
    .command-nav-preview {{
      font-size: 13px;
      color: var(--muted);
    }}
    .command-detail-stack {{
      display: grid;
      gap: 16px;
    }}
    .command-panel {{
      align-self: start;
    }}
    .command-panel[hidden] {{
      display: none;
    }}
    .editor-grid {{
      display: grid;
      gap: 16px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .editor-card textarea {{
      min-height: 240px;
    }}
    .actions {{
      display: flex;
      justify-content: flex-end;
      gap: 12px;
      flex-wrap: wrap;
    }}
    @media (max-width: 1080px) {{
      .hero-grid,
      .section-layout,
      .editor-grid,
      .command-browser {{
        grid-template-columns: 1fr;
      }}
      .sticky-stack,
      .command-sidebar {{
        position: static;
      }}
    }}
    @media (max-width: 720px) {{
      main {{
        padding: 16px 12px 32px;
      }}
      header,
      .content,
      .card {{
        padding: 16px;
      }}
      h1 {{
        font-size: 28px;
      }}
      .field {{
        padding: 12px;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <div class="shell">
      <header>
        <div class="header-top">
          <div class="header-copy">
            <span class="eyebrow">{escape(_t(request, "admin_brand"))}</span>
            <h1>{escape(heading_text)}</h1>
            <div class="meta muted">
              <div>{escape(_t(request, "admin_meta_config"))}: <code>{config_path}</code></div>
              <div>{escape(_t(request, "admin_meta_workspace"))}: <code>{workspace}</code></div>
            </div>
          </div>
          {_language_switch(request)}
        </div>
        {nav}
      </header>
      <section class="content">
        {''.join(notices)}
        <div class="page-flow">
          {body}
        </div>
      </section>
    </div>
  </main>
</body>
</html>"""
    response = web.Response(text=html, content_type="text/html")
    _set_lang_cookie(response, request)
    return response


def _pretty_json(data: object) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _read_json_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return _pretty_json(json.loads(path.read_text(encoding="utf-8")))
    except ValueError:
        return path.read_text(encoding="utf-8")


def _write_text_file(path: Path, content: str, *, optional: bool = False) -> None:
    if optional and not content.strip():
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n" if content else "", encoding="utf-8")


def _write_json_file(
    path: Path,
    raw: str,
    *,
    object_required_message: str,
    optional: bool = True,
) -> None:
    if optional and not raw.strip():
        if path.exists():
            path.unlink()
        return
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(object_required_message)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_pretty_json(data) + "\n", encoding="utf-8")


def _persona_file_map(workspace: Path, persona: str) -> dict[str, Path]:
    root = persona_workspace(workspace, persona)
    return {
        "SOUL.md": root / "SOUL.md",
        "USER.md": root / "USER.md",
        "STYLE.md": root / "STYLE.md",
        "LORE.md": root / "LORE.md",
        "VOICE.json": root / PERSONA_VOICE_FILENAME,
        "st_manifest.json": root / PERSONA_METADATA_DIRNAME / PERSONA_ST_MANIFEST_FILENAME,
    }


def _ensure_persona_scaffold(workspace: Path, persona: str) -> Path:
    root = workspace if persona == DEFAULT_PERSONA else personas_root(workspace) / persona
    ensure_dir(root)
    ensure_dir(root / "memory")
    ensure_dir(root / PERSONA_METADATA_DIRNAME)
    for filename in ("SOUL.md", "USER.md"):
        target = root / filename
        if not target.exists():
            target.write_text("", encoding="utf-8")
    for filename in ("MEMORY.md", "HISTORY.md"):
        target = root / "memory" / filename
        if not target.exists():
            target.write_text("", encoding="utf-8")
    return root


def _snake_case_key(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _resolve_nested_key(data: dict[str, Any], segment: str) -> str:
    if segment in data:
        return segment
    snake = _snake_case_key(segment)
    if snake in data:
        return snake
    return segment


def _set_nested_value(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = data
    for segment in path[:-1]:
        key = _resolve_nested_key(node, segment)
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[_resolve_nested_key(node, path[-1])] = value


def _config_form_values(config: Config) -> dict[str, Any]:
    voice = config.channels.voice_reply
    memorix = config.tools.mcp_servers.get(_MEMORIX_MCP_SERVER_NAME)
    memorix_args = (
        list(memorix.args)
        if memorix and memorix.args
        else list(_MEMORIX_MCP_DEFAULT_ARGS)
    )
    return {
        "agents_defaults_workspace": config.agents.defaults.workspace,
        "agents_defaults_model": config.agents.defaults.model,
        "agents_defaults_provider": config.agents.defaults.provider,
        "agents_defaults_max_tokens": str(config.agents.defaults.max_tokens),
        "agents_defaults_context_window_tokens": str(config.agents.defaults.context_window_tokens),
        "agents_defaults_temperature": str(config.agents.defaults.temperature),
        "agents_defaults_max_tool_iterations": str(config.agents.defaults.max_tool_iterations),
        "agents_defaults_reasoning_effort": config.agents.defaults.reasoning_effort or "",
        "agents_defaults_timezone": config.agents.defaults.timezone,
        "gateway_host": config.gateway.host,
        "gateway_port": str(config.gateway.port),
        "gateway_heartbeat_enabled": config.gateway.heartbeat.enabled,
        "gateway_heartbeat_interval_s": str(config.gateway.heartbeat.interval_s),
        "gateway_heartbeat_keep_recent_messages": str(config.gateway.heartbeat.keep_recent_messages),
        "gateway_admin_enabled": config.gateway.admin.enabled,
        "gateway_admin_auth_key": config.gateway.admin.auth_key,
        "tools_restrict_to_workspace": config.tools.restrict_to_workspace,
        "tools_web_proxy": config.tools.web.proxy or "",
        "tools_web_search_provider": config.tools.web.search.provider,
        "tools_web_search_api_key": config.tools.web.search.api_key,
        "tools_web_search_base_url": config.tools.web.search.base_url,
        "tools_web_search_max_results": str(config.tools.web.search.max_results),
        "tools_image_gen_enabled": config.tools.image_gen.enabled,
        "tools_image_gen_api_key": config.tools.image_gen.api_key,
        "tools_image_gen_base_url": config.tools.image_gen.base_url,
        "tools_image_gen_model": config.tools.image_gen.model,
        "tools_image_gen_proxy": config.tools.image_gen.proxy or "",
        "tools_image_gen_timeout": str(config.tools.image_gen.timeout),
        "tools_image_gen_reference_image": config.tools.image_gen.reference_image,
        "memory_user_shadow_write_mem0": config.memory.user.shadow_write_mem0,
        "memory_user_mem0_mode": config.memory.user.mem0.mode,
        "memory_user_mem0_llm_provider": config.memory.user.mem0.llm.provider,
        "memory_user_mem0_llm_api_key": config.memory.user.mem0.llm.api_key,
        "memory_user_mem0_llm_url": config.memory.user.mem0.llm.url,
        "memory_user_mem0_llm_model": config.memory.user.mem0.llm.model,
        "memory_user_mem0_llm_headers": _pretty_json(config.memory.user.mem0.llm.headers),
        "memory_user_mem0_llm_config": _pretty_json(config.memory.user.mem0.llm.config),
        "memory_user_mem0_embedder_provider": config.memory.user.mem0.embedder.provider,
        "memory_user_mem0_embedder_api_key": config.memory.user.mem0.embedder.api_key,
        "memory_user_mem0_embedder_url": config.memory.user.mem0.embedder.url,
        "memory_user_mem0_embedder_model": config.memory.user.mem0.embedder.model,
        "memory_user_mem0_embedder_headers": _pretty_json(config.memory.user.mem0.embedder.headers),
        "memory_user_mem0_embedder_config": _pretty_json(config.memory.user.mem0.embedder.config),
        "memory_user_mem0_vector_store_provider": config.memory.user.mem0.vector_store.provider,
        "memory_user_mem0_vector_store_api_key": config.memory.user.mem0.vector_store.api_key,
        "memory_user_mem0_vector_store_url": config.memory.user.mem0.vector_store.url,
        "memory_user_mem0_vector_store_model": config.memory.user.mem0.vector_store.model,
        "memory_user_mem0_vector_store_headers": _pretty_json(
            config.memory.user.mem0.vector_store.headers
        ),
        "memory_user_mem0_vector_store_config": _pretty_json(
            config.memory.user.mem0.vector_store.config
        ),
        "memory_user_mem0_metadata": _pretty_json(config.memory.user.mem0.metadata),
        "tools_mcp_memorix_enabled": memorix is not None,
        "tools_mcp_memorix_type": memorix.type if memorix and memorix.type else "",
        "tools_mcp_memorix_command": (
            memorix.command if memorix and memorix.command else _MEMORIX_MCP_DEFAULT_COMMAND
        ),
        "tools_mcp_memorix_args": ", ".join(memorix_args),
        "tools_mcp_memorix_url": memorix.url if memorix else "",
        "tools_mcp_memorix_tool_timeout": str(
            memorix.tool_timeout if memorix else _MEMORIX_MCP_DEFAULT_TIMEOUT
        ),
        "channels_send_progress": config.channels.send_progress,
        "channels_send_tool_hints": config.channels.send_tool_hints,
        "channels_send_max_retries": str(config.channels.send_max_retries),
        "channels_voice_reply_enabled": voice.enabled,
        "channels_voice_reply_channels": ", ".join(voice.channels),
        "channels_voice_reply_provider": voice.provider,
        "channels_voice_reply_model": voice.model,
        "channels_voice_reply_voice": voice.voice,
        "channels_voice_reply_instructions": voice.instructions,
        "channels_voice_reply_speed": "" if voice.speed is None else str(voice.speed),
        "channels_voice_reply_response_format": voice.response_format,
        "channels_voice_reply_api_key": voice.api_key,
        "channels_voice_reply_api_base": voice.api_base,
        "channels_voice_reply_edge_voice": voice.edge_voice,
        "channels_voice_reply_edge_rate": voice.edge_rate,
        "channels_voice_reply_edge_volume": voice.edge_volume,
        "channels_voice_reply_sovits_api_url": voice.sovits_api_url,
        "channels_voice_reply_sovits_refer_wav_path": voice.sovits_refer_wav_path,
        "channels_voice_reply_sovits_prompt_text": voice.sovits_prompt_text,
        "channels_voice_reply_sovits_prompt_language": voice.sovits_prompt_language,
        "channels_voice_reply_sovits_text_language": voice.sovits_text_language,
        "channels_voice_reply_sovits_cut_punc": voice.sovits_cut_punc,
        "channels_voice_reply_sovits_top_k": str(voice.sovits_top_k),
        "channels_voice_reply_sovits_top_p": str(voice.sovits_top_p),
        "channels_voice_reply_sovits_temperature": str(voice.sovits_temperature),
    }


def _extract_visual_values(
    form: Any,
    *,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    values = dict(baseline)
    bool_fields = {str(value) for value in form.getall("__bool_fields", [])}
    for field in _CONFIG_FIELDS:
        if field.kind == "bool":
            if field.name in bool_fields:
                values[field.name] = str(form.get(field.name, "")).lower() in {"1", "true", "on", "yes"}
            continue
        if field.name in form:
            values[field.name] = str(form.get(field.name, ""))
    return values


def _parse_visual_value(request: web.Request, field: ConfigFieldSpec, raw_value: Any) -> Any:
    if field.kind == "bool":
        return bool(raw_value)

    if field.kind == "csv":
        return [part.strip() for part in re.split(r"[\n,]", str(raw_value)) if part.strip()]

    text_value = str(raw_value)
    stripped = text_value.strip()

    if field.kind == "json":
        if not stripped:
            return {}
        try:
            data = json.loads(text_value)
        except ValueError as exc:
            raise ValueError(_t(request, "admin_error_invalid_json", error=exc)) from exc
        if not isinstance(data, dict):
            raise ValueError(_t(request, "admin_json_object_required"))
        return data

    if field.kind == "int":
        if not stripped:
            raise ValueError(
                _t(request, "admin_error_invalid_integer", field=_t(request, field.label_key))
            )
        try:
            return int(stripped)
        except ValueError as exc:
            raise ValueError(
                _t(request, "admin_error_invalid_integer", field=_t(request, field.label_key))
            ) from exc

    if field.kind == "float":
        if not stripped and field.name in _BLANK_AS_NONE_FIELDS:
            return None
        if not stripped:
            raise ValueError(
                _t(request, "admin_error_invalid_number", field=_t(request, field.label_key))
            )
        try:
            return float(stripped)
        except ValueError as exc:
            raise ValueError(
                _t(request, "admin_error_invalid_number", field=_t(request, field.label_key))
            ) from exc

    if field.name in _BLANK_AS_NONE_FIELDS and not stripped:
        return None

    if field.kind == "textarea":
        return text_value.replace("\r\n", "\n")
    return stripped


def _apply_visual_config_values(
    request: web.Request,
    *,
    raw_data: dict[str, Any],
    visual_values: dict[str, Any],
) -> dict[str, Any]:
    updated = json.loads(json.dumps(raw_data))
    memorix_enabled = bool(visual_values["tools_mcp_memorix_enabled"])
    tools_node = updated.setdefault("tools", {})
    if not isinstance(tools_node, dict):
        tools_node = {}
        updated["tools"] = tools_node
    servers_node = tools_node.get("mcpServers")
    if not isinstance(servers_node, dict):
        servers_node = {}
        tools_node["mcpServers"] = servers_node

    if memorix_enabled:
        memorix_values = {
            "type": _parse_visual_value(
                request,
                _CONFIG_FIELD_MAP["tools_mcp_memorix_type"],
                visual_values["tools_mcp_memorix_type"],
            ),
            "command": _parse_visual_value(
                request,
                _CONFIG_FIELD_MAP["tools_mcp_memorix_command"],
                visual_values["tools_mcp_memorix_command"],
            ),
            "args": _parse_visual_value(
                request,
                _CONFIG_FIELD_MAP["tools_mcp_memorix_args"],
                visual_values["tools_mcp_memorix_args"],
            ),
            "url": _parse_visual_value(
                request,
                _CONFIG_FIELD_MAP["tools_mcp_memorix_url"],
                visual_values["tools_mcp_memorix_url"],
            ),
            "toolTimeout": _parse_visual_value(
                request,
                _CONFIG_FIELD_MAP["tools_mcp_memorix_tool_timeout"],
                visual_values["tools_mcp_memorix_tool_timeout"],
            ),
        }
        servers_node[_MEMORIX_MCP_SERVER_NAME] = memorix_values
    else:
        servers_node.pop(_MEMORIX_MCP_SERVER_NAME, None)
        if not servers_node:
            tools_node.pop("mcpServers", None)

    for field in _CONFIG_FIELDS:
        if field.name in _MEMORIX_CONFIG_FIELD_NAMES:
            continue
        value = _parse_visual_value(request, field, visual_values[field.name])
        _set_nested_value(updated, field.path, value)
    return updated


def _validate_config_data(request: web.Request, data: dict[str, Any]) -> Config:
    try:
        config = Config.model_validate(data).bind_config_path(_current_config_path(request))
    except Exception as exc:
        raise ValueError(_t(request, "admin_error_config_validation", error=exc)) from exc
    if config.gateway.admin.enabled and not config.gateway.admin.auth_key.strip():
        raise ValueError(_t(request, "admin_error_admin_auth_required"))
    return config


def _reload_runtime_callback(request: web.Request) -> Callable[[], Awaitable[None]] | None:
    try:
        callback = request.app[_ADMIN_RELOAD_RUNTIME_KEY]
    except KeyError:
        return None
    return callback if callable(callback) else None


def _config_section_id(title_key: str) -> str:
    slug = title_key.removeprefix("admin_config_section_").removesuffix("_title")
    return f"section-{slug}"


def _render_config_field(request: web.Request, field: ConfigFieldSpec, value: Any) -> str:
    label = escape(_t(request, field.label_key))
    tooltip_key = field.label_key.removesuffix("_label") + "_tooltip"
    badge_class = "restart" if field.restart_required else "hot"
    badge_key = "admin_badge_restart_required" if field.restart_required else "admin_badge_hot_reload"
    runtime_badge = f'<span class="pill {badge_class}">{escape(_t(request, badge_key))}</span>'
    label_row = (
        '<span class="label-row tooltip-anchor" tabindex="0">'
        f'<span class="label">{label}</span>'
        f"{runtime_badge}"
        '<span class="tooltip-trigger" aria-hidden="true">?</span>'
        f'<span class="tooltip-card">{_th(request, tooltip_key)}</span>'
        "</span>"
    )
    hint = ""
    if field.hint_key:
        hint = f'<div class="hint">{_th(request, field.hint_key)}</div>'

    if field.kind == "bool":
        checked = " checked" if bool(value) else ""
        return (
            '<div class="field">'
            f'<input type="hidden" name="__bool_fields" value="{escape(field.name)}">'
            f'<label class="toggle"><input type="checkbox" name="{escape(field.name)}" value="1"{checked}>'
            f"{label_row}</label>{hint}</div>"
        )

    if field.kind in {"textarea", "json"}:
        rows = max(field.rows, 3)
        css_class = "field full"
        return (
            f'<label class="{css_class}">{label_row}'
            f'<textarea name="{escape(field.name)}" rows="{rows}" spellcheck="false">'
            f"{escape(str(value))}</textarea>{hint}</label>"
        )

    if field.kind == "select":
        options = []
        for option in field.options:
            selected = " selected" if str(value) == option else ""
            text = _t(request, "admin_option_default") if option == "" else option
            options.append(
                f'<option value="{escape(option)}"{selected}>{escape(text)}</option>'
            )
        control = f'<select name="{escape(field.name)}">{"".join(options)}</select>'
    else:
        input_type = "number" if field.kind in {"int", "float"} else "text"
        step = ' step="any"' if field.kind == "float" else ""
        placeholder = f' placeholder="{escape(field.placeholder)}"' if field.placeholder else ""
        control = (
            f'<input type="{input_type}" name="{escape(field.name)}" value="{escape(str(value))}"'
            f"{step}{placeholder}>"
        )

    return f'<label class="field">{label_row}{control}{hint}</label>'


def _render_config_section(
    request: web.Request,
    *,
    index: int,
    title_key: str,
    desc_key: str,
    field_names: tuple[str, ...],
    values: dict[str, Any],
) -> str:
    section_id = _config_section_id(title_key)
    fields = "".join(
        _render_config_field(request, _CONFIG_FIELD_MAP[field_name], values[field_name])
        for field_name in field_names
    )
    return (
        f'<section id="{section_id}" class="card stack section-card">'
        '<div class="section-topline">'
        '<div class="section-head">'
        f'<h2>{escape(_t(request, title_key))}</h2>'
        f'<div class="muted">{_th(request, desc_key)}</div>'
        "</div>"
        f'<span class="section-index">{index:02d}</span>'
        "</div>"
        f'<div class="field-grid">{fields}</div>'
        "</section>"
    )


async def _admin_login_page(request: web.Request) -> web.Response:
    _require_admin_enabled(request)
    if _is_authenticated(request):
        raise _redirect(request, _normalize_next_path(request.query.get("next")))

    auth_key = _admin_auth_key(request)
    if not auth_key:
        return _page(
            title=_t(request, "admin_login_title"),
            heading=_t(request, "admin_login_heading"),
            body=f'<div class="card"><p class="muted">{_th(request, "admin_login_missing_key_body")}</p></div>',
            request=request,
            error=_t(request, "admin_login_missing_key_error"),
        )

    next_path = _normalize_next_path(request.query.get("next"))
    body = f"""
      <div class="hero-grid">
        <section class="card stack spotlight">
          <span class="eyebrow">{escape(_t(request, "admin_brand"))}</span>
          <h2 class="panel-title">{escape(_t(request, "admin_login_heading"))}</h2>
          <ul class="detail-list">
            <li>{_th(request, "admin_card_config_desc")}</li>
            <li>{_th(request, "admin_card_commands_desc")}</li>
            <li>{_th(request, "admin_card_personas_desc")}</li>
          </ul>
        </section>
        <form method="post" action="/admin/login" class="card stack">
          <input type="hidden" name="next" value="{escape(next_path)}">
          <label class="field">
            <span class="label">{escape(_t(request, "admin_login_key_label"))}</span>
            <input type="password" name="auth_key" autocomplete="current-password" required>
          </label>
          <div class="actions">
            <button type="submit">{escape(_t(request, "admin_login_submit"))}</button>
          </div>
        </form>
      </div>
    """
    return _page(
        title=_t(request, "admin_login_title"),
        heading=_t(request, "admin_login_heading"),
        body=body,
        request=request,
    )


async def _admin_login_submit(request: web.Request) -> web.Response:
    _require_admin_enabled(request)
    form = await request.post()
    auth_key = _admin_auth_key(request)
    next_path = _normalize_next_path(form.get("next"))

    if not auth_key:
        return _page(
            title=_t(request, "admin_login_title"),
            heading=_t(request, "admin_login_heading"),
            body=f'<div class="card"><p class="muted">{_th(request, "admin_login_configure_key")}</p></div>',
            request=request,
            error=_t(request, "admin_login_missing_key_error"),
        )

    submitted = str(form.get("auth_key", ""))
    if not hmac.compare_digest(submitted, auth_key):
        return _page(
            title=_t(request, "admin_login_title"),
            heading=_t(request, "admin_login_heading"),
            body=(
                f'<form method="post" action="/admin/login" class="card stack">'
                f'<input type="hidden" name="next" value="{escape(next_path)}">'
                f'<label class="field"><span class="label">{escape(_t(request, "admin_login_key_label"))}</span>'
                '<input type="password" name="auth_key" autocomplete="current-password" required>'
                f"</label><div class=\"actions\"><button type=\"submit\">{escape(_t(request, 'admin_login_submit'))}</button></div>"
                "</form>"
            ),
            request=request,
            error=_t(request, "admin_login_invalid_error"),
        )

    response = _redirect(request, next_path)
    response.set_cookie(
        _ADMIN_COOKIE,
        _build_session_cookie(auth_key),
        max_age=_ADMIN_COOKIE_TTL_S,
        httponly=True,
        samesite="Strict",
    )
    raise response


async def _admin_logout(request: web.Request) -> web.Response:
    _require_admin_enabled(request)
    response = _redirect(request, "/admin/login")
    response.del_cookie(_ADMIN_COOKIE)
    raise response


async def _admin_index(request: web.Request) -> web.Response:
    _require_admin_auth(request)
    config = _load_current_config(request)
    runtime_workspace = _runtime_workspace(request)
    config_workspace = config.workspace_path
    mismatch = ""
    if config_workspace.resolve(strict=False) != runtime_workspace.resolve(strict=False):
        mismatch = f'<div class="notice error">{_th(request, "admin_overview_workspace_mismatch")}</div>'

    body = f"""
      {mismatch}
      <section class="hero-grid">
        <div class="card stack spotlight">
          <span class="eyebrow">{escape(_t(request, "admin_brand"))}</span>
          <h2 class="panel-title">{escape(_t(request, "admin_overview_heading"))}</h2>
          <div class="stat-grid">
            <div class="stat-card">
              <span>{escape(_t(request, "admin_label_model"))}</span>
              <strong><code>{escape(config.agents.defaults.model)}</code></strong>
            </div>
            <div class="stat-card">
              <span>{escape(_t(request, "admin_label_provider"))}</span>
              <strong><code>{escape(config.agents.defaults.provider)}</code></strong>
            </div>
            <div class="stat-card">
              <span>{escape(_t(request, "admin_label_config_workspace"))}</span>
              <strong><code>{escape(str(config_workspace))}</code></strong>
            </div>
          </div>
        </div>
        <div class="card stack">
          <strong>{escape(_t(request, "admin_card_admin"))}</strong>
          <div class="muted">{escape(_t(request, "admin_label_enabled"))}: <code>{escape(_t(request, "admin_boolean_true" if config.gateway.admin.enabled else "admin_boolean_false"))}</code></div>
          <div class="muted">{escape(_t(request, "admin_label_auth_configured"))}: <code>{escape(_t(request, "admin_boolean_true" if bool(config.gateway.admin.auth_key.strip()) else "admin_boolean_false"))}</code></div>
          <div class="muted">{escape(_t(request, "admin_label_scope"))}: {escape(_t(request, "admin_scope_text"))}</div>
          <div class="muted">{escape(_t(request, "admin_meta_workspace"))}: <code>{escape(str(runtime_workspace))}</code></div>
        </div>
      </section>
      <div class="grid">
        <div class="card stack feature-card">
          <strong>{escape(_t(request, "admin_card_config"))}</strong>
          <p class="muted">{_th(request, "admin_card_config_desc")}</p>
          <a class="nav-link active" href="/admin/config">{escape(_t(request, "admin_card_config_open"))}</a>
        </div>
        <div class="card stack feature-card">
          <strong>{escape(_t(request, "admin_card_personas"))}</strong>
          <p class="muted">{_th(request, "admin_card_personas_desc")}</p>
          <a class="nav-link active" href="/admin/personas">{escape(_t(request, "admin_card_personas_open"))}</a>
        </div>
        <div class="card stack feature-card">
          <strong>{escape(_t(request, "admin_card_commands"))}</strong>
          <p class="muted">{_th(request, "admin_card_commands_desc")}</p>
          <a class="nav-link active" href="/admin/commands">{escape(_t(request, "admin_card_commands_open"))}</a>
        </div>
      </div>
    """
    return _page(
        title=_t(request, "admin_overview_title"),
        heading=_t(request, "admin_overview_heading"),
        body=body,
        request=request,
    )


def _render_config_page(
    request: web.Request,
    *,
    visual_values: dict[str, Any],
    raw_text: str,
    flash: str | None = None,
    error: str | None = None,
    active_mode: str = "visual",
) -> web.Response:
    sections_parts: list[str] = []
    jump_links: list[str] = []
    for index, (title_key, desc_key, field_names) in enumerate(_CONFIG_SECTIONS, start=1):
        section_id = _config_section_id(title_key)
        sections_parts.append(
            _render_config_section(
                request,
                index=index,
                title_key=title_key,
                desc_key=desc_key,
                field_names=field_names,
                values=visual_values,
            )
        )
        jump_links.append(
            f'<a class="jump-link" href="#{section_id}">'
            '<div class="jump-link-top">'
            f'<span class="jump-link-index">{index:02d}</span>'
            f'<strong>{escape(_t(request, title_key))}</strong>'
            "</div>"
            f'<div class="jump-link-meta">{len(field_names)} {escape(_t(request, "admin_label_fields"))}</div>'
            "</a>"
        )
    sections = "".join(sections_parts)
    raw_open = " open" if active_mode == "raw" else ""
    body = f"""
      <div class="section-layout">
        <aside class="sticky-stack">
          <div class="card stack spotlight">
            <span class="eyebrow">{escape(_t(request, "admin_nav_config"))}</span>
            <p class="muted">{_th(request, "admin_config_intro", config_path=_current_config_path(request))}</p>
            <div class="muted">{_th(request, "admin_config_reload_notice")}</div>
            <div class="badge-row">
              <span class="pill hot">{escape(_t(request, "admin_badge_hot_reload"))}</span>
              <span class="pill restart">{escape(_t(request, "admin_badge_restart_required"))}</span>
            </div>
            <div class="stat-grid">
              <div class="stat-card">
                <span>{escape(_t(request, "admin_label_sections"))}</span>
                <strong>{len(_CONFIG_SECTIONS)}</strong>
              </div>
              <div class="stat-card">
                <span>{escape(_t(request, "admin_label_fields"))}</span>
                <strong>{len(_CONFIG_FIELDS)}</strong>
              </div>
            </div>
          </div>
          <nav class="card stack">
            <div class="section-head">
              <h2>{escape(_t(request, "admin_config_jump_title"))}</h2>
              <div class="muted">{escape(_t(request, "admin_config_jump_desc"))}</div>
            </div>
            <div class="jump-list">
              {''.join(jump_links)}
            </div>
          </nav>
        </aside>
        <div class="stack">
          <form method="post" action="/admin/config" class="stack">
            <input type="hidden" name="mode" value="visual">
            {sections}
            <div class="card actions">
              <button type="submit">{escape(_t(request, "admin_config_save_visual"))}</button>
            </div>
          </form>
          <details class="card stack"{raw_open}>
            <summary>{escape(_t(request, "admin_config_advanced_title"))}</summary>
            <p class="muted">{_th(request, "admin_config_advanced_desc")}</p>
            <form method="post" action="/admin/config" class="stack">
              <input type="hidden" name="mode" value="raw">
              <label class="field full">
                <span class="label">{escape(_t(request, "admin_config_raw_label"))}</span>
                <textarea class="json-editor" name="config_json" spellcheck="false">{escape(raw_text)}</textarea>
              </label>
              <div class="actions">
                <button type="submit" class="ghost">{escape(_t(request, "admin_config_save_raw"))}</button>
              </div>
            </form>
          </details>
        </div>
      </div>
    """
    return _page(
        title=_t(request, "admin_config_title"),
        heading=_t(request, "admin_config_heading"),
        body=body,
        request=request,
        flash=flash,
        error=error,
    )


async def _admin_config_page(request: web.Request) -> web.Response:
    _require_admin_auth(request)
    config = _load_current_config(request)
    try:
        raw_data = _load_raw_config_data(request)
    except Exception:
        raw_data = config.model_dump(mode="json", by_alias=True)
    flash = None
    if request.query.get("saved") == "1":
        flash = (
            _t(request, "admin_config_saved_reloaded")
            if request.query.get("reloaded") == "1"
            else _t(request, "admin_config_saved")
        )
    return _render_config_page(
        request,
        visual_values=_config_form_values(config),
        raw_text=_pretty_json(raw_data),
        flash=flash,
    )


async def _admin_config_submit(request: web.Request) -> web.Response:
    _require_admin_auth(request)
    form = await request.post()
    mode = str(form.get("mode", "visual"))
    current_config = _load_current_config(request)
    baseline_values = _config_form_values(current_config)
    try:
        current_raw = _load_raw_config_data(request)
    except Exception:
        current_raw = current_config.model_dump(mode="json", by_alias=True)

    if mode == "raw":
        raw_text = str(form.get("config_json", ""))
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            return _render_config_page(
                request,
                visual_values=baseline_values,
                raw_text=raw_text,
                error=_t(request, "admin_error_invalid_json", error=exc),
                active_mode="raw",
            )
        if not isinstance(data, dict):
            return _render_config_page(
                request,
                visual_values=baseline_values,
                raw_text=raw_text,
                error=_t(
                    request,
                    "admin_error_config_validation",
                    error=_t(request, "admin_json_object_required"),
                ),
                active_mode="raw",
            )
        data = _migrate_config(data)
        try:
            _validate_config_data(request, data)
        except ValueError as exc:
            return _render_config_page(
                request,
                visual_values=_config_form_values(current_config),
                raw_text=raw_text,
                error=str(exc),
                active_mode="raw",
            )
        _save_raw_config_data(request, data)
        reload_runtime = _reload_runtime_callback(request)
        if reload_runtime is not None:
            try:
                await reload_runtime()
            except Exception as exc:
                return _render_config_page(
                    request,
                    visual_values=_config_form_values(load_config(_current_config_path(request))),
                    raw_text=_pretty_json(_load_raw_config_data(request)),
                    error=_t(request, "admin_error_runtime_reload_failed", error=exc),
                    active_mode="raw",
                )
            raise _redirect(request, "/admin/config?saved=1&reloaded=1")
        raise _redirect(request, "/admin/config?saved=1")

    visual_values = _extract_visual_values(form, baseline=baseline_values)
    try:
        updated = _apply_visual_config_values(
            request,
            raw_data=current_raw,
            visual_values=visual_values,
        )
        _validate_config_data(request, updated)
    except ValueError as exc:
        return _render_config_page(
            request,
            visual_values=visual_values,
            raw_text=_pretty_json(current_raw),
            error=str(exc),
        )

    _save_raw_config_data(request, updated)
    reload_runtime = _reload_runtime_callback(request)
    if reload_runtime is not None:
        try:
            await reload_runtime()
        except Exception as exc:
            return _render_config_page(
                request,
                visual_values=_config_form_values(load_config(_current_config_path(request))),
                raw_text=_pretty_json(_load_raw_config_data(request)),
                error=_t(request, "admin_error_runtime_reload_failed", error=exc),
            )
        raise _redirect(request, "/admin/config?saved=1&reloaded=1")
    raise _redirect(request, "/admin/config?saved=1")


def _command_usage_lines(request: web.Request, spec: CommandDocSpec) -> list[str]:
    if spec.usage_text_key:
        return [
            line.strip()
            for line in _t(request, spec.usage_text_key).splitlines()
            if line.strip().startswith("/")
        ]
    return list(spec.usage_lines)


def _command_panel_id(spec: CommandDocSpec) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", spec.command.lower()).strip("-")
    return f"command-{slug or 'item'}"


def _render_command_nav_item(request: web.Request, spec: CommandDocSpec, *, active: bool) -> str:
    panel_id = _command_panel_id(spec)
    preview = _t(request, spec.description_keys[0]) if spec.description_keys else spec.command
    css_class = "command-nav-item active" if active else "command-nav-item"
    selected = "true" if active else "false"
    return (
        f'<a class="{css_class}" href="#{panel_id}" data-command-target="{panel_id}" '
        f'role="tab" aria-selected="{selected}" aria-controls="{panel_id}">'
        f"<code>{escape(spec.command)}</code>"
        f'<span class="command-nav-preview">{escape(preview)}</span>'
        "</a>"
    )


def _render_command_panel(request: web.Request, spec: CommandDocSpec, *, active: bool) -> str:
    description_items = "".join(
        f"<li>{escape(_t(request, key))}</li>"
        for key in spec.description_keys
    )
    usage_lines = "\n".join(_command_usage_lines(request, spec))
    panel_id = _command_panel_id(spec)
    aliases = ""
    if spec.aliases:
        aliases_html = " ".join(f"<code>{escape(alias)}</code>" for alias in spec.aliases)
        aliases = (
            f'<div><strong>{escape(_t(request, "admin_commands_aliases_label"))}:</strong> '
            f"{aliases_html}</div>"
        )
    notes = ""
    if spec.note_key:
        notes = (
            f'<div><strong>{escape(_t(request, "admin_commands_notes_label"))}:</strong> '
            f'{_th(request, spec.note_key)}</div>'
        )
    active_class = " active" if active else ""
    hidden = "" if active else " hidden"
    return f"""
      <section id="{panel_id}" class="card stack command-panel{active_class}" data-command-panel="{panel_id}" role="tabpanel"{hidden}>
        <div class="section-head">
          <h2><code>{escape(spec.command)}</code></h2>
        </div>
        <div class="stack">
          <div><strong>{escape(_t(request, "admin_commands_forms_label"))}:</strong></div>
          <ul class="detail-list">{description_items}</ul>
          <div><strong>{escape(_t(request, "admin_commands_usage_label"))}:</strong></div>
          <pre class="code-block"><code>{escape(usage_lines)}</code></pre>
          {aliases}
          {notes}
        </div>
      </section>
    """


async def _admin_commands_page(request: web.Request) -> web.Response:
    _require_admin_auth(request)
    nav_items = "".join(
        _render_command_nav_item(request, spec, active=index == 0)
        for index, spec in enumerate(_COMMAND_DOCS)
    )
    panels = "".join(
        _render_command_panel(request, spec, active=index == 0)
        for index, spec in enumerate(_COMMAND_DOCS)
    )
    body = f"""
      <div class="hero-grid">
        <div class="card stack spotlight">
          <span class="eyebrow">{escape(_t(request, "admin_nav_commands"))}</span>
          <h2 class="panel-title">{escape(_t(request, "admin_commands_heading"))}</h2>
          <p class="muted">{_th(request, "admin_commands_intro")}</p>
        </div>
        <div class="card stack">
          <div class="stat-grid">
            <div class="stat-card">
              <span>{escape(_t(request, "admin_commands_title"))}</span>
              <strong>{len(_COMMAND_DOCS)}</strong>
            </div>
            <div class="stat-card">
              <span>{escape(_t(request, "admin_commands_aliases_label"))}</span>
              <strong>{sum(len(spec.aliases) for spec in _COMMAND_DOCS)}</strong>
            </div>
          </div>
        </div>
      </div>
      <div class="command-browser" data-command-browser>
        <aside class="card stack command-sidebar">
          <div class="section-head">
            <h2>{escape(_t(request, "admin_commands_list_title"))}</h2>
            <div class="muted">{escape(_t(request, "admin_commands_list_desc"))}</div>
          </div>
          <nav class="command-nav" role="tablist">
            {nav_items}
          </nav>
        </aside>
        <div class="command-detail-stack">
          {panels}
        </div>
      </div>
      <script>
        (() => {{
          const root = document.querySelector("[data-command-browser]");
          if (!root) return;
          const items = Array.from(root.querySelectorAll("[data-command-target]"));
          const panels = new Map(
            Array.from(root.querySelectorAll("[data-command-panel]")).map((panel) => [
              panel.dataset.commandPanel,
              panel,
            ]),
          );

          const select = (id, updateHash = false) => {{
            items.forEach((item) => {{
              const active = item.dataset.commandTarget === id;
              item.classList.toggle("active", active);
              item.setAttribute("aria-selected", String(active));
            }});
            panels.forEach((panel, panelId) => {{
              const active = panelId === id;
              panel.classList.toggle("active", active);
              panel.hidden = !active;
              panel.setAttribute("aria-hidden", String(!active));
            }});
            if (updateHash && window.location.hash !== "#" + id) {{
              history.replaceState(null, "", "#" + id);
            }}
          }};

          const initialId = (() => {{
            const hash = window.location.hash.replace(/^#/, "");
            if (hash && panels.has(hash)) return hash;
            const first = items[0];
            return first ? first.dataset.commandTarget : null;
          }})();

          if (initialId) select(initialId);

          items.forEach((item) => {{
            item.addEventListener("click", (event) => {{
              event.preventDefault();
              const id = item.dataset.commandTarget;
              if (id) select(id, true);
            }});
          }});

          window.addEventListener("hashchange", () => {{
            const hash = window.location.hash.replace(/^#/, "");
            if (hash && panels.has(hash)) select(hash);
          }});
        }})();
      </script>
    """
    return _page(
        title=_t(request, "admin_commands_title"),
        heading=_t(request, "admin_commands_heading"),
        body=body,
        request=request,
    )


def _render_personas_page(
    request: web.Request,
    *,
    flash: str | None = None,
    error: str | None = None,
) -> web.Response:
    workspace = _runtime_workspace(request)
    items = []
    for persona in list_personas(workspace):
        label = _t(request, "admin_default_persona_label") if persona == DEFAULT_PERSONA else persona
        items.append(
            f'<li><a href="/admin/personas/{escape(persona)}"><strong>{escape(label)}</strong>'
            f'<span class="muted">{escape(str(persona_workspace(workspace, persona)))}</span></a></li>'
        )

    body = f"""
      <div class="hero-grid">
        <div class="card stack spotlight">
          <span class="eyebrow">{escape(_t(request, "admin_nav_personas"))}</span>
          <h2 class="panel-title">{escape(_t(request, "admin_personas_heading"))}</h2>
          <div class="muted">{_th(request, "admin_card_personas_desc")}</div>
          <div class="muted">{escape(_t(request, "admin_meta_workspace"))}: <code>{escape(str(workspace))}</code></div>
        </div>
        <div class="card stack">
          <strong>{escape(_t(request, "admin_card_create_persona"))}</strong>
          <p class="muted">{_th(request, "admin_card_create_persona_desc")}</p>
          <form method="post" action="/admin/personas/new" class="stack">
            <label class="field">
              <span class="label">{escape(_t(request, "admin_persona_name_label"))}</span>
              <input type="text" name="name" placeholder="Aria" required>
            </label>
            <div class="actions">
              <button type="submit">{escape(_t(request, "admin_button_create_persona"))}</button>
            </div>
          </form>
        </div>
      </div>
      <section class="card stack">
        <div class="section-head">
          <h2>{escape(_t(request, "admin_card_personas"))}</h2>
          <div class="muted">{escape(_t(request, "admin_meta_workspace"))}: <code>{escape(str(workspace))}</code></div>
        </div>
        <ul class="list grid persona-list">{''.join(items)}</ul>
      </section>
    """
    return _page(
        title=_t(request, "admin_personas_title"),
        heading=_t(request, "admin_personas_heading"),
        body=body,
        request=request,
        flash=flash,
        error=error,
    )


async def _admin_personas_page(request: web.Request) -> web.Response:
    _require_admin_auth(request)
    flash = request.query.get("saved")
    if flash == "created":
        flash = _t(request, "admin_persona_created")
    elif flash == "updated":
        flash = _t(request, "admin_persona_updated")
    else:
        flash = None
    return _render_personas_page(request, flash=flash)


async def _admin_persona_create(request: web.Request) -> web.Response:
    _require_admin_auth(request)
    form = await request.post()
    raw_name = str(form.get("name", "")).strip()
    normalized = normalize_persona_name(raw_name)
    if not normalized or normalized == DEFAULT_PERSONA:
        return _render_personas_page(
            request,
            error=_t(request, "admin_error_invalid_persona_name"),
        )

    root = personas_root(_runtime_workspace(request))
    persona_dir = root / normalized
    if persona_dir.exists():
        raise _redirect(request, f"/admin/personas/{quote(normalized, safe='')}")

    _ensure_persona_scaffold(_runtime_workspace(request), normalized)
    raise _redirect(request, f"/admin/personas/{quote(normalized, safe='')}?saved=created")


def _resolved_persona_or_404(request: web.Request) -> str:
    requested = request.match_info["persona"]
    workspace = _runtime_workspace(request)
    if requested == DEFAULT_PERSONA:
        return DEFAULT_PERSONA
    resolved = resolve_persona_name(workspace, requested)
    if resolved is None:
        raise web.HTTPNotFound()
    return resolved


def _render_persona_detail_page(
    request: web.Request,
    *,
    persona: str,
    values: dict[str, str],
    flash: str | None = None,
    error: str | None = None,
) -> web.Response:
    persona_root = persona_workspace(_runtime_workspace(request), persona)

    def _editor_card(title: str, desc_key: str, field_name: str, value: str) -> str:
        return (
            '<label class="card stack editor-card">'
            f"<strong>{escape(title)}</strong>"
            f'<div class="muted">{_th(request, desc_key)}</div>'
            f'<textarea name="{escape(field_name)}" spellcheck="false">{escape(value)}</textarea>'
            "</label>"
        )

    body = f"""
      <div class="hero-grid">
        <div class="card stack spotlight">
          <span class="eyebrow">{escape(_t(request, "admin_nav_personas"))}</span>
          <h2 class="panel-title"><code>{escape(persona)}</code></h2>
          <div class="muted">{escape(_t(request, "admin_persona_label"))}: <code>{escape(persona)}</code></div>
          <div class="muted">{escape(_t(request, "admin_persona_directory_label"))}: <code>{escape(str(persona_root))}</code></div>
        </div>
        <div class="card stack">
          <span class="eyebrow">{escape(_t(request, "admin_button_save_persona"))}</span>
          <div class="muted">{_th(request, "admin_persona_intro")}</div>
          <div class="muted">{_th(request, "admin_persona_optional_hint")}</div>
        </div>
      </div>
      <form method="post" action="/admin/personas/{escape(persona)}" class="stack" id="persona-form">
        <div class="editor-grid">
          {_editor_card("SOUL.md", "admin_persona_soul_desc", "soul_md", values["SOUL.md"])}
          {_editor_card("USER.md", "admin_persona_user_desc", "user_md", values["USER.md"])}
        </div>
        <div class="editor-grid">
          {_editor_card("STYLE.md", "admin_persona_style_desc", "style_md", values["STYLE.md"])}
          {_editor_card("LORE.md", "admin_persona_lore_desc", "lore_md", values["LORE.md"])}
        </div>
        <div class="editor-grid">
          {_editor_card("VOICE.json", "admin_persona_voice_desc", "voice_json", values["VOICE.json"])}
          {_editor_card("st_manifest.json", "admin_persona_manifest_desc", "manifest_json", values["st_manifest.json"])}
        </div>
        <div class="card stack">
          <div class="muted">{_th(request, "admin_persona_optional_hint")}</div>
          <div class="actions">
            <button type="submit">{escape(_t(request, "admin_button_save_persona"))}</button>
          </div>
        </div>
      </form>
    """
    return _page(
        title=_t(request, "admin_persona_title", persona=persona),
        heading=_t(request, "admin_persona_heading", persona=persona),
        body=body,
        request=request,
        flash=flash,
        error=error,
    )


async def _admin_persona_page(request: web.Request) -> web.Response:
    _require_admin_auth(request)
    persona = _resolved_persona_or_404(request)
    files = _persona_file_map(_runtime_workspace(request), persona)
    values = {
        "SOUL.md": _read_text(files["SOUL.md"]),
        "USER.md": _read_text(files["USER.md"]),
        "STYLE.md": _read_text(files["STYLE.md"]),
        "LORE.md": _read_text(files["LORE.md"]),
        "VOICE.json": _read_json_text(files["VOICE.json"]),
        "st_manifest.json": _read_json_text(files["st_manifest.json"]),
    }
    flash = None
    if request.query.get("saved") == "created":
        flash = _t(request, "admin_persona_created")
    elif request.query.get("saved") == "updated":
        flash = _t(request, "admin_persona_updated")
    return _render_persona_detail_page(request, persona=persona, values=values, flash=flash)


async def _admin_persona_submit(request: web.Request) -> web.Response:
    _require_admin_auth(request)
    persona = _resolved_persona_or_404(request)
    form = await request.post()
    files = _persona_file_map(_runtime_workspace(request), persona)
    values = {
        "SOUL.md": str(form.get("soul_md", "")),
        "USER.md": str(form.get("user_md", "")),
        "STYLE.md": str(form.get("style_md", "")),
        "LORE.md": str(form.get("lore_md", "")),
        "VOICE.json": str(form.get("voice_json", "")),
        "st_manifest.json": str(form.get("manifest_json", "")),
    }

    try:
        _ensure_persona_scaffold(_runtime_workspace(request), persona)
        _write_text_file(files["SOUL.md"], values["SOUL.md"], optional=False)
        _write_text_file(files["USER.md"], values["USER.md"], optional=False)
        _write_text_file(files["STYLE.md"], values["STYLE.md"], optional=True)
        _write_text_file(files["LORE.md"], values["LORE.md"], optional=True)
        _write_json_file(
            files["VOICE.json"],
            values["VOICE.json"],
            optional=True,
            object_required_message=_t(request, "admin_json_object_required"),
        )
        _write_json_file(
            files["st_manifest.json"],
            values["st_manifest.json"],
            optional=True,
            object_required_message=_t(request, "admin_json_object_required"),
        )
    except Exception as exc:
        return _render_persona_detail_page(request, persona=persona, values=values, error=str(exc))

    raise _redirect(request, f"/admin/personas/{quote(persona, safe='')}?saved=updated")
