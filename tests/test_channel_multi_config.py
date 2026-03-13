import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import (
    Config,
    DingTalkConfig,
    DingTalkMultiConfig,
    DiscordConfig,
    DiscordMultiConfig,
    EmailConfig,
    EmailMultiConfig,
    FeishuConfig,
    FeishuMultiConfig,
    MatrixConfig,
    MatrixMultiConfig,
    MochatConfig,
    MochatMultiConfig,
    QQConfig,
    QQMultiConfig,
    SlackConfig,
    SlackMultiConfig,
    TelegramConfig,
    TelegramMultiConfig,
    WhatsAppConfig,
    WhatsAppMultiConfig,
    WecomConfig,
    WecomMultiConfig,
)


class _DummyChannel(BaseChannel):
    name = "dummy"
    display_name = "Dummy"

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        return None


def _patch_registry(monkeypatch: pytest.MonkeyPatch, channel_names: list[str]) -> None:
    monkeypatch.setattr("nanobot.channels.registry.discover_channel_names", lambda: channel_names)
    monkeypatch.setattr("nanobot.channels.registry.load_channel_class", lambda _: _DummyChannel)


@pytest.mark.parametrize(
    ("field_name", "payload", "expected_cls", "attr_name", "attr_value"),
    [
        (
            "whatsapp",
            {"enabled": True, "bridgeUrl": "ws://127.0.0.1:3001", "allowFrom": ["123"]},
            WhatsAppConfig,
            "bridge_url",
            "ws://127.0.0.1:3001",
        ),
        (
            "telegram",
            {"enabled": True, "token": "tg-1", "allowFrom": ["alice"]},
            TelegramConfig,
            "token",
            "tg-1",
        ),
        (
            "discord",
            {"enabled": True, "token": "dc-1", "allowFrom": ["42"]},
            DiscordConfig,
            "token",
            "dc-1",
        ),
        (
            "feishu",
            {"enabled": True, "appId": "fs-1", "appSecret": "secret-1", "allowFrom": ["ou_1"]},
            FeishuConfig,
            "app_id",
            "fs-1",
        ),
        (
            "dingtalk",
            {
                "enabled": True,
                "clientId": "dt-1",
                "clientSecret": "secret-1",
                "allowFrom": ["staff-1"],
            },
            DingTalkConfig,
            "client_id",
            "dt-1",
        ),
        (
            "matrix",
            {
                "enabled": True,
                "homeserver": "https://matrix.example.com",
                "accessToken": "mx-token",
                "userId": "@bot:example.com",
                "allowFrom": ["@alice:example.com"],
            },
            MatrixConfig,
            "homeserver",
            "https://matrix.example.com",
        ),
        (
            "email",
            {
                "enabled": True,
                "consentGranted": True,
                "imapHost": "imap.example.com",
                "allowFrom": ["a@example.com"],
            },
            EmailConfig,
            "imap_host",
            "imap.example.com",
        ),
        (
            "mochat",
            {
                "enabled": True,
                "clawToken": "claw-token",
                "agentUserId": "agent-1",
                "allowFrom": ["user-1"],
            },
            MochatConfig,
            "claw_token",
            "claw-token",
        ),
        (
            "slack",
            {"enabled": True, "botToken": "xoxb-1", "appToken": "xapp-1", "allowFrom": ["U1"]},
            SlackConfig,
            "bot_token",
            "xoxb-1",
        ),
        (
            "qq",
            {
                "enabled": True,
                "appId": "qq-1",
                "secret": "secret-1",
                "allowFrom": ["openid-1"],
            },
            QQConfig,
            "app_id",
            "qq-1",
        ),
        (
            "wecom",
            {
                "enabled": True,
                "botId": "wc-1",
                "secret": "secret-1",
                "allowFrom": ["user-1"],
            },
            WecomConfig,
            "bot_id",
            "wc-1",
        ),
    ],
)
def test_config_parses_supported_single_instance_channels(
    field_name: str,
    payload: dict,
    expected_cls: type,
    attr_name: str,
    attr_value: str,
) -> None:
    config = Config.model_validate({"channels": {field_name: payload}})

    section = getattr(config.channels, field_name)
    assert isinstance(section, expected_cls)
    assert getattr(section, attr_name) == attr_value


@pytest.mark.parametrize(
    ("field_name", "payload", "expected_cls", "attr_name", "attr_value"),
    [
        (
            "whatsapp",
            {
                "enabled": True,
                "instances": [
                    {"name": "main", "bridgeUrl": "ws://127.0.0.1:3001", "allowFrom": ["123"]},
                    {"name": "backup", "bridgeUrl": "ws://127.0.0.1:3002", "allowFrom": ["456"]},
                ],
            },
            WhatsAppMultiConfig,
            "bridge_url",
            "ws://127.0.0.1:3002",
        ),
        (
            "telegram",
            {
                "enabled": True,
                "instances": [
                    {"name": "main", "token": "tg-main", "allowFrom": ["alice"]},
                    {"name": "backup", "token": "tg-backup", "allowFrom": ["bob"]},
                ],
            },
            TelegramMultiConfig,
            "token",
            "tg-backup",
        ),
        (
            "discord",
            {
                "enabled": True,
                "instances": [
                    {"name": "main", "token": "dc-main", "allowFrom": ["42"]},
                    {"name": "backup", "token": "dc-backup", "allowFrom": ["43"]},
                ],
            },
            DiscordMultiConfig,
            "token",
            "dc-backup",
        ),
        (
            "feishu",
            {
                "enabled": True,
                "instances": [
                    {"name": "main", "appId": "fs-main", "appSecret": "s1", "allowFrom": ["ou_1"]},
                    {
                        "name": "backup",
                        "appId": "fs-backup",
                        "appSecret": "s2",
                        "allowFrom": ["ou_2"],
                    },
                ],
            },
            FeishuMultiConfig,
            "app_id",
            "fs-backup",
        ),
        (
            "dingtalk",
            {
                "enabled": True,
                "instances": [
                    {
                        "name": "main",
                        "clientId": "dt-main",
                        "clientSecret": "s1",
                        "allowFrom": ["staff-1"],
                    },
                    {
                        "name": "backup",
                        "clientId": "dt-backup",
                        "clientSecret": "s2",
                        "allowFrom": ["staff-2"],
                    },
                ],
            },
            DingTalkMultiConfig,
            "client_id",
            "dt-backup",
        ),
        (
            "matrix",
            {
                "enabled": True,
                "instances": [
                    {
                        "name": "main",
                        "homeserver": "https://matrix-1.example.com",
                        "accessToken": "mx-token-1",
                        "userId": "@bot1:example.com",
                        "allowFrom": ["@alice:example.com"],
                    },
                    {
                        "name": "backup",
                        "homeserver": "https://matrix-2.example.com",
                        "accessToken": "mx-token-2",
                        "userId": "@bot2:example.com",
                        "allowFrom": ["@bob:example.com"],
                    },
                ],
            },
            MatrixMultiConfig,
            "homeserver",
            "https://matrix-2.example.com",
        ),
        (
            "email",
            {
                "enabled": True,
                "instances": [
                    {
                        "name": "work",
                        "consentGranted": True,
                        "imapHost": "imap.work",
                        "allowFrom": ["a@work"],
                    },
                    {
                        "name": "home",
                        "consentGranted": True,
                        "imapHost": "imap.home",
                        "allowFrom": ["a@home"],
                    },
                ],
            },
            EmailMultiConfig,
            "imap_host",
            "imap.home",
        ),
        (
            "mochat",
            {
                "enabled": True,
                "instances": [
                    {
                        "name": "main",
                        "clawToken": "claw-main",
                        "agentUserId": "agent-1",
                        "allowFrom": ["user-1"],
                    },
                    {
                        "name": "backup",
                        "clawToken": "claw-backup",
                        "agentUserId": "agent-2",
                        "allowFrom": ["user-2"],
                    },
                ],
            },
            MochatMultiConfig,
            "claw_token",
            "claw-backup",
        ),
        (
            "slack",
            {
                "enabled": True,
                "instances": [
                    {
                        "name": "main",
                        "botToken": "xoxb-main",
                        "appToken": "xapp-main",
                        "allowFrom": ["U1"],
                    },
                    {
                        "name": "backup",
                        "botToken": "xoxb-backup",
                        "appToken": "xapp-backup",
                        "allowFrom": ["U2"],
                    },
                ],
            },
            SlackMultiConfig,
            "bot_token",
            "xoxb-backup",
        ),
        (
            "qq",
            {
                "enabled": True,
                "instances": [
                    {"name": "main", "appId": "qq-main", "secret": "s1", "allowFrom": ["openid-1"]},
                    {
                        "name": "backup",
                        "appId": "qq-backup",
                        "secret": "s2",
                        "allowFrom": ["openid-2"],
                    },
                ],
            },
            QQMultiConfig,
            "app_id",
            "qq-backup",
        ),
        (
            "wecom",
            {
                "enabled": True,
                "instances": [
                    {"name": "main", "botId": "wc-main", "secret": "s1", "allowFrom": ["user-1"]},
                    {
                        "name": "backup",
                        "botId": "wc-backup",
                        "secret": "s2",
                        "allowFrom": ["user-2"],
                    },
                ],
            },
            WecomMultiConfig,
            "bot_id",
            "wc-backup",
        ),
    ],
)
def test_config_parses_supported_multi_instance_channels(
    field_name: str,
    payload: dict,
    expected_cls: type,
    attr_name: str,
    attr_value: str,
) -> None:
    config = Config.model_validate({"channels": {field_name: payload}})

    section = getattr(config.channels, field_name)
    assert isinstance(section, expected_cls)
    assert [inst.name for inst in section.instances] == ["main", "backup"]
    assert getattr(section.instances[1], attr_name) == attr_value


def test_channel_manager_registers_mixed_single_and_multi_instance_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_registry(
        monkeypatch,
        ["whatsapp", "telegram", "discord", "qq", "email", "matrix", "mochat"],
    )
    config = Config.model_validate(
        {
            "channels": {
                "whatsapp": {
                    "enabled": True,
                    "instances": [
                        {
                            "name": "phone-a",
                            "bridgeUrl": "ws://127.0.0.1:3001",
                            "allowFrom": ["123"],
                        },
                    ],
                },
                "telegram": {
                    "enabled": True,
                    "instances": [
                        {"name": "main", "token": "tg-main", "allowFrom": ["alice"]},
                        {"name": "backup", "token": "tg-backup", "allowFrom": ["bob"]},
                    ],
                },
                "discord": {
                    "enabled": True,
                    "token": "dc-main",
                    "allowFrom": ["42"],
                },
                "qq": {
                    "enabled": True,
                    "instances": [
                        {
                            "name": "alpha",
                            "appId": "qq-alpha",
                            "secret": "s1",
                            "allowFrom": ["openid-1"],
                        },
                    ],
                },
                "email": {
                    "enabled": True,
                    "instances": [
                        {
                            "name": "work",
                            "consentGranted": True,
                            "imapHost": "imap.work",
                            "allowFrom": ["a@work"],
                        },
                    ],
                },
                "matrix": {
                    "enabled": True,
                    "instances": [
                        {
                            "name": "ops",
                            "homeserver": "https://matrix.example.com",
                            "accessToken": "mx-token",
                            "userId": "@bot:example.com",
                            "allowFrom": ["@alice:example.com"],
                        },
                    ],
                },
                "mochat": {
                    "enabled": True,
                    "instances": [
                        {
                            "name": "sales",
                            "clawToken": "claw-token",
                            "agentUserId": "agent-1",
                            "allowFrom": ["user-1"],
                        },
                    ],
                },
            }
        }
    )

    manager = ChannelManager(config, MessageBus())

    assert manager.enabled_channels == [
        "whatsapp/phone-a",
        "telegram/main",
        "telegram/backup",
        "discord",
        "qq/alpha",
        "email/work",
        "matrix/ops",
        "mochat/sales",
    ]
    assert manager.get_channel("whatsapp/phone-a").config.bridge_url == "ws://127.0.0.1:3001"
    assert manager.get_channel("telegram/backup") is not None
    assert manager.get_channel("telegram/backup").config.token == "tg-backup"
    assert manager.get_channel("discord") is not None
    assert manager.get_channel("qq/alpha").config.app_id == "qq-alpha"
    assert manager.get_channel("email/work").config.imap_host == "imap.work"
    assert manager.get_channel("matrix/ops").config.user_id == "@bot:example.com"
    assert manager.get_channel("mochat/sales").config.claw_token == "claw-token"


def test_channel_manager_skips_empty_multi_instance_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_registry(monkeypatch, ["telegram"])
    config = Config.model_validate(
        {"channels": {"telegram": {"enabled": True, "instances": []}}}
    )

    manager = ChannelManager(config, MessageBus())

    assert isinstance(config.channels.telegram, TelegramMultiConfig)
    assert manager.enabled_channels == []
