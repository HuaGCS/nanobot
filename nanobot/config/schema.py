"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings

from nanobot.cron.types import CronSchedule


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class WhatsAppConfig(Base):
    """WhatsApp channel configuration."""

    enabled: bool = False
    bridge_url: str = "ws://localhost:3001"
    bridge_token: str = ""  # Shared token for bridge auth (optional, recommended)
    allow_from: list[str] = Field(default_factory=list)  # Allowed phone numbers
    group_policy: Literal["open", "mention"] = "open"


class WhatsAppInstanceConfig(WhatsAppConfig):
    """WhatsApp bridge instance config for multi-bot mode."""

    name: str = Field(min_length=1)


class WhatsAppMultiConfig(Base):
    """WhatsApp channel configuration supporting multiple bridge instances."""

    enabled: bool = False
    instances: list[WhatsAppInstanceConfig] = Field(default_factory=list)


class TelegramConfig(Base):
    """Telegram channel configuration."""

    enabled: bool = False
    token: str = ""  # Bot token from @BotFather
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs or usernames
    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    reply_to_message: bool = False  # If true, bot replies quote the original message
    react_emoji: str = "👀"
    group_policy: Literal["open", "mention"] = "mention"  # "mention" responds when @mentioned or replied to, "open" responds to all
    connection_pool_size: int = 32  # Outbound Telegram API HTTP pool size
    pool_timeout: float = 5.0  # Shared HTTP pool timeout for bot sends and getUpdates
    streaming: bool = True  # Progressive edit-based streaming for final text replies


class TelegramInstanceConfig(TelegramConfig):
    """Telegram bot instance config for multi-bot mode."""

    name: str = Field(min_length=1)


class TelegramMultiConfig(Base):
    """Telegram channel configuration supporting multiple bot instances."""

    enabled: bool = False
    instances: list[TelegramInstanceConfig] = Field(default_factory=list)


class FeishuConfig(Base):
    """Feishu/Lark channel configuration using WebSocket long connection."""

    enabled: bool = False
    app_id: str = ""  # App ID from Feishu Open Platform
    app_secret: str = ""  # App Secret from Feishu Open Platform
    encrypt_key: str = ""  # Encrypt Key for event subscription (optional)
    verification_token: str = ""  # Verification Token for event subscription (optional)
    allow_from: list[str] = Field(default_factory=list)  # Allowed user open_ids
    react_emoji: str = (
        "THUMBSUP"  # Emoji type for message reactions (e.g. THUMBSUP, OK, DONE, SMILE)
    )
    group_policy: Literal["open", "mention"] = "mention"  # "mention" responds when @mentioned, "open" responds to all
    reply_to_message: bool = False  # If true, replies quote the original Feishu message
    streaming: bool = True  # Progressive edit-based streaming for final text replies


class FeishuInstanceConfig(FeishuConfig):
    """Feishu bot instance config for multi-bot mode."""

    name: str = Field(min_length=1)


class FeishuMultiConfig(Base):
    """Feishu channel configuration supporting multiple bot instances."""

    enabled: bool = False
    instances: list[FeishuInstanceConfig] = Field(default_factory=list)


class DingTalkConfig(Base):
    """DingTalk channel configuration using Stream mode."""

    enabled: bool = False
    client_id: str = ""  # AppKey
    client_secret: str = ""  # AppSecret
    allow_from: list[str] = Field(default_factory=list)  # Allowed staff_ids


class DingTalkInstanceConfig(DingTalkConfig):
    """DingTalk bot instance config for multi-bot mode."""

    name: str = Field(min_length=1)


class DingTalkMultiConfig(Base):
    """DingTalk channel configuration supporting multiple bot instances."""

    enabled: bool = False
    instances: list[DingTalkInstanceConfig] = Field(default_factory=list)


class DiscordConfig(Base):
    """Discord channel configuration."""

    enabled: bool = False
    token: str = ""  # Bot token from Discord Developer Portal
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = 37377  # GUILDS + GUILD_MESSAGES + DIRECT_MESSAGES + MESSAGE_CONTENT
    group_policy: Literal["mention", "open"] = "mention"


class DiscordInstanceConfig(DiscordConfig):
    """Discord bot instance config for multi-bot mode."""

    name: str = Field(min_length=1)


class DiscordMultiConfig(Base):
    """Discord channel configuration supporting multiple bot instances."""

    enabled: bool = False
    instances: list[DiscordInstanceConfig] = Field(default_factory=list)


class MatrixConfig(Base):
    """Matrix (Element) channel configuration."""

    enabled: bool = False
    homeserver: str = "https://matrix.org"
    access_token: str = ""
    user_id: str = ""  # @bot:matrix.org
    device_id: str = ""
    e2ee_enabled: bool = True  # Enable Matrix E2EE support (encryption + encrypted room handling).
    sync_stop_grace_seconds: int = (
        2  # Max seconds to wait for sync_forever to stop gracefully before cancellation fallback.
    )
    max_media_bytes: int = (
        20 * 1024 * 1024
    )  # Max attachment size accepted for Matrix media handling (inbound + outbound).
    allow_from: list[str] = Field(default_factory=list)
    group_policy: Literal["open", "mention", "allowlist"] = "open"
    group_allow_from: list[str] = Field(default_factory=list)
    allow_room_mentions: bool = False


class MatrixInstanceConfig(MatrixConfig):
    """Matrix bot/account instance config for multi-account mode."""

    name: str = Field(min_length=1)


class MatrixMultiConfig(Base):
    """Matrix channel configuration supporting multiple accounts."""

    enabled: bool = False
    instances: list[MatrixInstanceConfig] = Field(default_factory=list)


class EmailConfig(Base):
    """Email channel configuration (IMAP inbound + SMTP outbound)."""

    enabled: bool = False
    consent_granted: bool = False  # Explicit owner permission to access mailbox data

    # IMAP (receive)
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"
    imap_use_ssl: bool = True

    # SMTP (send)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    from_address: str = ""

    # Behavior
    auto_reply_enabled: bool = (
        True  # If false, inbound email is read but no automatic reply is sent
    )
    poll_interval_seconds: int = 30
    mark_seen: bool = True
    max_body_chars: int = 12000
    subject_prefix: str = "Re: "
    allow_from: list[str] = Field(default_factory=list)  # Allowed sender email addresses
    verify_dkim: bool = True  # Require Authentication-Results with dkim=pass
    verify_spf: bool = True  # Require Authentication-Results with spf=pass
    allowed_attachment_types: list[str] = Field(default_factory=list)
    max_attachment_size: int = 2_000_000  # 2MB per attachment
    max_attachments_per_email: int = 5


class EmailInstanceConfig(EmailConfig):
    """Email account instance config for multi-account mode."""

    name: str = Field(min_length=1)


class EmailMultiConfig(Base):
    """Email channel configuration supporting multiple accounts."""

    enabled: bool = False
    instances: list[EmailInstanceConfig] = Field(default_factory=list)


class MochatMentionConfig(Base):
    """Mochat mention behavior configuration."""

    require_in_groups: bool = False


class MochatGroupRule(Base):
    """Mochat per-group mention requirement."""

    require_mention: bool = False


class MochatConfig(Base):
    """Mochat channel configuration."""

    enabled: bool = False
    base_url: str = "https://mochat.io"
    socket_url: str = ""
    socket_path: str = "/socket.io"
    socket_disable_msgpack: bool = False
    socket_reconnect_delay_ms: int = 1000
    socket_max_reconnect_delay_ms: int = 10000
    socket_connect_timeout_ms: int = 10000
    refresh_interval_ms: int = 30000
    watch_timeout_ms: int = 25000
    watch_limit: int = 100
    retry_delay_ms: int = 500
    max_retry_attempts: int = 0  # 0 means unlimited retries
    claw_token: str = ""
    agent_user_id: str = ""
    sessions: list[str] = Field(default_factory=list)
    panels: list[str] = Field(default_factory=list)
    allow_from: list[str] = Field(default_factory=list)
    mention: MochatMentionConfig = Field(default_factory=MochatMentionConfig)
    groups: dict[str, MochatGroupRule] = Field(default_factory=dict)
    reply_delay_mode: str = "non-mention"  # off | non-mention
    reply_delay_ms: int = 120000


class MochatInstanceConfig(MochatConfig):
    """Mochat account instance config for multi-account mode."""

    name: str = Field(min_length=1)


class MochatMultiConfig(Base):
    """Mochat channel configuration supporting multiple accounts."""

    enabled: bool = False
    instances: list[MochatInstanceConfig] = Field(default_factory=list)


class SlackDMConfig(Base):
    """Slack DM policy configuration."""

    enabled: bool = True
    policy: str = "open"  # "open" or "allowlist"
    allow_from: list[str] = Field(default_factory=list)  # Allowed Slack user IDs


class SlackConfig(Base):
    """Slack channel configuration."""

    enabled: bool = False
    mode: str = "socket"  # "socket" supported
    webhook_path: str = "/slack/events"
    bot_token: str = ""  # xoxb-...
    app_token: str = ""  # xapp-...
    user_token_read_only: bool = True
    reply_in_thread: bool = True
    react_emoji: str = "eyes"
    done_emoji: str = "white_check_mark"
    allow_from: list[str] = Field(default_factory=list)  # Allowed Slack user IDs (sender-level)
    group_policy: str = "mention"  # "mention", "open", "allowlist"
    group_allow_from: list[str] = Field(default_factory=list)  # Allowed channel IDs if allowlist
    dm: SlackDMConfig = Field(default_factory=SlackDMConfig)


class SlackInstanceConfig(SlackConfig):
    """Slack bot instance config for multi-bot mode."""

    name: str = Field(min_length=1)


class SlackMultiConfig(Base):
    """Slack channel configuration supporting multiple bot instances."""

    enabled: bool = False
    instances: list[SlackInstanceConfig] = Field(default_factory=list)


class QQConfig(Base):
    """QQ channel configuration using botpy SDK (single instance)."""

    enabled: bool = False
    app_id: str = ""  # 机器人 ID (AppID) from q.qq.com
    secret: str = ""  # 机器人密钥 (AppSecret) from q.qq.com
    allow_from: list[str] = Field(default_factory=list)  # Allowed user openids
    msg_format: Literal["plain", "markdown"] = "plain"
    media_dir: str = ""
    download_chunk_size: int = 1024 * 256
    download_max_bytes: int = 1024 * 1024 * 200
    media_base_url: str = ""  # Public base URL used to expose workspace/out QQ media files


class QQInstanceConfig(QQConfig):
    """QQ bot instance config for multi-bot mode."""

    name: str = Field(min_length=1)  # instance key, routed as channel name "qq/<name>"


class QQMultiConfig(Base):
    """QQ channel configuration supporting multiple bot instances."""

    enabled: bool = False
    instances: list[QQInstanceConfig] = Field(default_factory=list)


class WeixinConfig(Base):
    """Personal WeChat (Weixin) channel configuration."""

    enabled: bool = False
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs
    base_url: str = "https://ilinkai.weixin.qq.com"
    cdn_base_url: str = "https://novac2c.cdn.weixin.qq.com/c2c"
    route_tag: str | int | None = None
    token: str = ""  # Saved QR-login token or manually supplied token
    state_dir: str = ""  # Optional state directory for token/buffer persistence
    poll_timeout: int = 35


class WecomConfig(Base):
    """WeCom (Enterprise WeChat) AI Bot channel configuration."""

    enabled: bool = False
    bot_id: str = ""  # Bot ID from WeCom AI Bot platform
    secret: str = ""  # Bot Secret from WeCom AI Bot platform
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs
    welcome_message: str = ""  # Welcome message for enter_chat event


class WecomInstanceConfig(WecomConfig):
    """WeCom bot instance config for multi-bot mode."""

    name: str = Field(min_length=1)


class WecomMultiConfig(Base):
    """WeCom channel configuration supporting multiple bot instances."""

    enabled: bool = False
    instances: list[WecomInstanceConfig] = Field(default_factory=list)


class VoiceReplyConfig(Base):
    """Optional text-to-speech replies for supported outbound channels."""

    enabled: bool = False
    channels: list[str] = Field(default_factory=lambda: ["telegram"])
    provider: Literal["openai", "edge", "sovits"] = "openai"
    model: str = "gpt-4o-mini-tts"
    voice: str = "alloy"
    instructions: str = ""
    speed: float | None = None
    response_format: Literal["mp3", "opus", "aac", "flac", "wav", "pcm", "silk"] = "opus"
    api_key: str = ""
    api_base: str = Field(default="", validation_alias=AliasChoices("apiBase", "url"))
    edge_voice: str = "zh-CN-XiaoxiaoNeural"
    edge_rate: str = "+0%"
    edge_volume: str = "+0%"
    sovits_api_url: str = "http://127.0.0.1:9880"
    sovits_refer_wav_path: str = ""
    sovits_prompt_text: str = ""
    sovits_prompt_language: str = "zh"
    sovits_text_language: str = "zh"
    sovits_cut_punc: str = "，。"
    sovits_top_k: int = 5
    sovits_top_p: float = 1.0
    sovits_temperature: float = 1.0


def _coerce_multi_channel_config(
    value: Any,
    single_cls: type[BaseModel],
    multi_cls: type[BaseModel],
) -> BaseModel:
    """Parse a channel config into single- or multi-instance form."""
    if isinstance(value, (single_cls, multi_cls)):
        return value
    if value is None:
        return single_cls()
    if isinstance(value, dict) and "instances" in value:
        return multi_cls.model_validate(value)
    return single_cls.model_validate(value)


class ChannelsConfig(Base):
    """Configuration for chat channels.

    Built-in and plugin channel configs are stored as extra fields (dicts).
    Each channel parses its own config in __init__.
    Per-channel "streaming": true enables streaming output (requires send_delta impl).
    """

    model_config = ConfigDict(extra="allow")

    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file("…"))
    send_max_retries: int = Field(default=3, ge=0, le=10)  # Max delivery attempts (initial send included)
    transcription_provider: Literal["groq", "openai"] = "groq"
    voice_reply: VoiceReplyConfig = Field(default_factory=VoiceReplyConfig)
    whatsapp: WhatsAppConfig | WhatsAppMultiConfig = Field(default_factory=WhatsAppConfig)
    telegram: TelegramConfig | TelegramMultiConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig | DiscordMultiConfig = Field(default_factory=DiscordConfig)
    feishu: FeishuConfig | FeishuMultiConfig = Field(default_factory=FeishuConfig)
    mochat: MochatConfig | MochatMultiConfig = Field(default_factory=MochatConfig)
    dingtalk: DingTalkConfig | DingTalkMultiConfig = Field(default_factory=DingTalkConfig)
    email: EmailConfig | EmailMultiConfig = Field(default_factory=EmailConfig)
    slack: SlackConfig | SlackMultiConfig = Field(default_factory=SlackConfig)
    qq: QQConfig | QQMultiConfig = Field(default_factory=QQConfig)
    matrix: MatrixConfig | MatrixMultiConfig = Field(default_factory=MatrixConfig)
    weixin: WeixinConfig = Field(default_factory=WeixinConfig)
    wecom: WecomConfig | WecomMultiConfig = Field(default_factory=WecomConfig)

    @field_validator(
        "whatsapp",
        "telegram",
        "discord",
        "feishu",
        "mochat",
        "dingtalk",
        "email",
        "slack",
        "qq",
        "matrix",
        "wecom",
        mode="before",
    )
    @classmethod
    def _parse_multi_instance_channels(cls, value: Any, info: ValidationInfo) -> BaseModel:
        mapping: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
            "whatsapp": (WhatsAppConfig, WhatsAppMultiConfig),
            "telegram": (TelegramConfig, TelegramMultiConfig),
            "discord": (DiscordConfig, DiscordMultiConfig),
            "feishu": (FeishuConfig, FeishuMultiConfig),
            "mochat": (MochatConfig, MochatMultiConfig),
            "dingtalk": (DingTalkConfig, DingTalkMultiConfig),
            "email": (EmailConfig, EmailMultiConfig),
            "slack": (SlackConfig, SlackMultiConfig),
            "qq": (QQConfig, QQMultiConfig),
            "matrix": (MatrixConfig, MatrixMultiConfig),
            "wecom": (WecomConfig, WecomMultiConfig),
        }
        single_cls, multi_cls = mapping[info.field_name]
        return _coerce_multi_channel_config(value, single_cls, multi_cls)


class ProviderPoolTarget(Base):
    """One provider target inside a provider pool."""

    provider: str
    model: str | None = None

    @field_validator("provider")
    @classmethod
    def _normalize_provider_name(cls, value: str) -> str:
        from nanobot.providers.registry import find_by_name

        normalized = (value or "").strip()
        if not normalized:
            raise ValueError("provider pool target requires a provider name")
        spec = find_by_name(normalized)
        if spec is None:
            raise ValueError(f"unknown provider '{value}'")
        return spec.name

    @field_validator("model", mode="before")
    @classmethod
    def _empty_model_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class ProviderPoolConfig(Base):
    """Provider selection policy for multi-provider routing."""

    strategy: Literal["failover", "round_robin"] = "failover"
    targets: list[ProviderPoolTarget] = Field(default_factory=list)

    @field_validator("strategy", mode="before")
    @classmethod
    def _normalize_strategy(cls, value: Any) -> Any:
        if isinstance(value, str):
            normalized = value.strip().lower().replace("-", "_")
            if normalized == "roundrobin":
                return "round_robin"
            return normalized
        return value

    @field_validator("targets", mode="before")
    @classmethod
    def _coerce_targets(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            return value
        coerced: list[Any] = []
        for item in value:
            if isinstance(item, str):
                coerced.append({"provider": item})
            else:
                coerced.append(item)
        return coerced


class DreamConfig(Base):
    """Dream memory consolidation configuration."""

    _HOUR_MS = 3_600_000

    interval_h: int = Field(default=2, ge=1)  # Every 2 hours by default
    cron: str | None = Field(default=None, exclude=True)  # Legacy compatibility override
    model_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("modelOverride", "model", "model_override"),
    )  # Optional Dream-specific model override
    max_batch_size: int = Field(default=20, ge=1)  # Max history entries per run
    max_iterations: int = Field(default=10, ge=1)  # Max tool calls per Phase 2

    def build_schedule(self, timezone: str) -> CronSchedule:
        """Build the runtime schedule, preferring the legacy cron override if present."""
        if self.cron:
            return CronSchedule(kind="cron", expr=self.cron, tz=timezone)
        return CronSchedule(kind="every", every_ms=self.interval_h * self._HOUR_MS)

    def describe_schedule(self) -> str:
        """Return a human-readable summary for logs and startup output."""
        if self.cron:
            return f"cron {self.cron} (legacy)"
        return f"every {self.interval_h}h"


class AgentDefaults(Base):
    """Default agent configuration."""

    workspace: str = ""  # Default: <config-dir>/workspace
    model: str = "anthropic/claude-opus-4-5"
    provider: str = (
        "auto"  # Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection
    )
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    context_block_limit: int | None = None
    temperature: float = 0.1
    max_tool_iterations: int = 200
    max_tool_result_chars: int = 16_000
    provider_retry_mode: Literal["standard", "persistent"] = "standard"
    reasoning_effort: str | None = None  # low / medium / high / adaptive - enables LLM thinking mode
    timezone: str = "UTC"  # IANA timezone, e.g. "Asia/Shanghai", "America/New_York"
    dream: DreamConfig = Field(default_factory=DreamConfig)
    provider_pool: ProviderPoolConfig | None = Field(
        default=None,
        exclude_if=lambda value: value is None or not value.targets,
    )

    @field_validator("provider_pool", mode="before")
    @classmethod
    def _coerce_provider_pool(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        if isinstance(value, list):
            return {"targets": value}
        return value


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)


class ProvidersConfig(Base):
    """Configuration for LLM providers."""

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig)  # Azure OpenAI (model = deployment name)
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama local models
    ovms: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenVINO Model Server (OVMS)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    mistral: ProviderConfig = Field(default_factory=ProviderConfig)
    stepfun: ProviderConfig = Field(default_factory=ProviderConfig)  # Step Fun (阶跃星辰)
    xiaomi_mimo: ProviderConfig = Field(default_factory=ProviderConfig)  # Xiaomi MIMO (小米)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow (硅基流动)
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine (火山引擎)
    volcengine_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine Coding Plan
    byteplus: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus (VolcEngine international)
    byteplus_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus Coding Plan
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # OpenAI Codex (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # Github Copilot (OAuth)
    qianfan: ProviderConfig = Field(default_factory=ProviderConfig)  # Qianfan (百度千帆)


class HeartbeatConfig(Base):
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes
    keep_recent_messages: int = 8


class ApiConfig(Base):
    """OpenAI-compatible API server configuration."""

    host: str = "127.0.0.1"  # Safer default: local-only bind.
    port: int = 8900
    timeout: float = 120.0  # Per-request timeout in seconds.


class GatewayAdminConfig(Base):
    """Built-in admin page configuration."""

    enabled: bool = False
    auth_key: str = ""


class GatewayStatusPushConfig(Base):
    """Optional Star Office UI push configuration."""

    enabled: bool = False
    mode: Literal["guest", "main"] = "guest"
    office_url: str = ""
    join_key: str = ""
    agent_name: str = "nanobot"
    timeout: float = Field(default=10.0, gt=0.0)

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: Any) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
        return "guest"

    @field_validator("agent_name", mode="before")
    @classmethod
    def _default_agent_name(cls, value: Any) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return "nanobot"

    @model_validator(mode="after")
    def _validate_required_fields(self) -> "GatewayStatusPushConfig":
        if self.enabled and not self.office_url.strip():
            raise ValueError("gateway.status.push.officeUrl is required when push is enabled")
        if self.enabled and self.mode == "guest" and not self.join_key.strip():
            raise ValueError("gateway.status.push.joinKey is required when push is enabled")
        return self


class GatewayStatusConfig(Base):
    """Optional HTTP status endpoint for Star Office UI-style dashboards."""

    enabled: bool = False
    auth_key: str = ""
    push: GatewayStatusPushConfig = Field(default_factory=GatewayStatusPushConfig)


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    admin: GatewayAdminConfig = Field(default_factory=GatewayAdminConfig)
    status: GatewayStatusConfig = Field(default_factory=GatewayStatusConfig)


class WebSearchConfig(Base):
    """Web search tool configuration."""

    provider: Literal["brave", "searxng"] = "brave"
    api_key: str = ""  # Brave Search API key (ignored by SearXNG)
    base_url: str = ""  # Required for SearXNG, e.g. "http://localhost:8080"
    max_results: int = 5
    timeout: int = 30  # Wall-clock timeout (seconds) for search operations


class WebToolsConfig(Base):
    """Web tools configuration."""

    enable: bool = True
    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecToolConfig(Base):
    """Shell exec tool configuration."""

    enable: bool = True
    timeout: int = 60
    path_append: str = ""
    sandbox: str = ""  # sandbox backend: "" (none) or "bwrap"


class ImageGenConfig(Base):
    """Image generation tool configuration."""

    enabled: bool = False
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-image-1"
    proxy: str | None = None
    timeout: int = 180
    reference_image: str = ""


class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    tool_timeout: int = 30  # seconds before a tool call is cancelled


class ToolsConfig(Base):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    image_gen: ImageGenConfig = Field(default_factory=ImageGenConfig)
    restrict_to_workspace: bool = False  # If true, restrict all tool access to workspace directory
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    ssrf_whitelist: list[str] = Field(default_factory=list)  # CIDR ranges to exempt from SSRF blocking (e.g. ["100.64.0.0/10"] for Tailscale)


class Mem0ProviderConfig(Base):
    """Reserved provider section for future Mem0 component configuration."""

    provider: str = ""
    api_key: str = ""
    url: str = ""
    model: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class Mem0Config(Base):
    """Reserved Mem0 OSS configuration shape for future backend integration."""

    mode: Literal["embedded"] = "embedded"
    llm: Mem0ProviderConfig = Field(default_factory=Mem0ProviderConfig)
    embedder: Mem0ProviderConfig = Field(default_factory=Mem0ProviderConfig)
    vector_store: Mem0ProviderConfig = Field(default_factory=Mem0ProviderConfig)
    metadata: dict[str, str] = Field(default_factory=dict)


class UserMemoryConfig(Base):
    """User-scoped long-term memory settings."""

    shadow_write_mem0: bool = False
    mem0: Mem0Config = Field(default_factory=Mem0Config)


class MemoryConfig(Base):
    """Long-term memory configuration."""

    user: UserMemoryConfig = Field(default_factory=UserMemoryConfig)


class Config(BaseSettings):
    """Root configuration for nanobot."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    _config_path: Path | None = PrivateAttr(default=None)

    def bind_config_path(self, config_path: Path | None) -> "Config":
        """Attach the source config path used to load or save this config."""
        self._config_path = (
            Path(config_path).expanduser().resolve(strict=False) if config_path is not None else None
        )
        return self

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        from nanobot.config.loader import get_default_config_path
        from nanobot.config.paths import resolve_workspace_path

        config_path = self._config_path or get_default_config_path()
        return resolve_workspace_path(
            self.agents.defaults.workspace or None,
            config_path=config_path,
        )

    def _match_provider(
        self, model: str | None = None
    ) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name. Returns (config, spec_name)."""
        from nanobot.providers.registry import PROVIDERS, find_by_name

        forced = self.agents.defaults.provider
        if forced != "auto":
            spec = find_by_name(forced)
            if spec:
                p = getattr(self.providers, spec.name, None)
                return (p, spec.name) if p else (None, None)
            return None, None

        model_lower = (model or self.agents.defaults.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")

        def _kw_matches(kw: str) -> bool:
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        # Explicit provider prefix wins — prevents `github-copilot/...codex` matching openai_codex.
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Fallback: configured local providers can route models without
        # provider-specific keywords (for example plain "llama3.2" on Ollama).
        # Prefer providers whose detect_by_base_keyword matches the configured api_base
        # (e.g. Ollama's "11434" in "http://localhost:11434") over plain registry order.
        local_fallback: tuple[ProviderConfig, str] | None = None
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = getattr(self.providers, spec.name, None)
            if not (p and p.api_base):
                continue
            if spec.detect_by_base_keyword and spec.detect_by_base_keyword in p.api_base:
                return p, spec.name
            if local_fallback is None:
                local_fallback = (p, spec.name)
        if local_fallback:
            return local_fallback

        # Fallback: gateways first, then others (follows registry order)
        # OAuth providers are NOT valid fallbacks — they require explicit model selection
        for spec in PROVIDERS:
            if spec.is_oauth:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
                return p, spec.name
        return None, None

    def get_provider(self, model: str | None = None) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        p, _ = self._match_provider(model)
        return p

    def get_provider_name(self, model: str | None = None) -> str | None:
        """Get the registry name of the matched provider (e.g. "deepseek", "openrouter")."""
        _, name = self._match_provider(model)
        return name

    def get_api_key(self, model: str | None = None) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(model)
        return p.api_key if p else None

    def get_api_base(self, model: str | None = None) -> str | None:
        """Get API base URL for the given model. Applies default URLs for gateway/local providers."""
        from nanobot.providers.registry import find_by_name

        p, name = self._match_provider(model)
        if p and p.api_base:
            return p.api_base
        # Only gateways get a default api_base here. Standard providers
        # resolve their base URL from the registry in the provider constructor.
        if name:
            spec = find_by_name(name)
            if spec and (spec.is_gateway or spec.is_local) and spec.default_api_base:
                return spec.default_api_base
        return None

    model_config = ConfigDict(env_prefix="NANOBOT_", env_nested_delimiter="__")
