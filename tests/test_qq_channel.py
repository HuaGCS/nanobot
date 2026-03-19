import os
from types import SimpleNamespace

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.qq import QQChannel
from nanobot.config.schema import QQConfig


class _FakeApi:
    def __init__(self) -> None:
        self.c2c_calls: list[dict] = []
        self.group_calls: list[dict] = []
        self.c2c_file_calls: list[dict] = []
        self.group_file_calls: list[dict] = []

    async def post_c2c_message(self, **kwargs) -> None:
        self.c2c_calls.append(kwargs)

    async def post_group_message(self, **kwargs) -> None:
        self.group_calls.append(kwargs)

    async def post_c2c_file(self, **kwargs) -> dict:
        self.c2c_file_calls.append(kwargs)
        return {"file_info": "c2c-file-info", "file_uuid": "c2c-file", "ttl": 60}

    async def post_group_file(self, **kwargs) -> dict:
        self.group_file_calls.append(kwargs)
        return {"file_info": "group-file-info", "file_uuid": "group-file", "ttl": 60}


class _FakeClient:
    def __init__(self) -> None:
        self.api = _FakeApi()


@pytest.mark.asyncio
async def test_on_group_message_routes_to_group_chat_id() -> None:
    channel = QQChannel(QQConfig(app_id="app", secret="secret", allow_from=["user1"]), MessageBus())

    data = SimpleNamespace(
        id="msg1",
        content="hello",
        group_openid="group123",
        author=SimpleNamespace(member_openid="user1"),
    )

    await channel._on_message(data, is_group=True)

    msg = await channel.bus.consume_inbound()
    assert msg.sender_id == "user1"
    assert msg.chat_id == "group123"


@pytest.mark.asyncio
async def test_send_group_message_uses_plain_text_group_api_with_msg_seq() -> None:
    channel = QQChannel(QQConfig(app_id="app", secret="secret", allow_from=["*"]), MessageBus())
    channel._client = _FakeClient()
    channel._chat_type_cache["group123"] = "group"

    await channel.send(
        OutboundMessage(
            channel="qq",
            chat_id="group123",
            content="hello",
            metadata={"message_id": "msg1"},
        )
    )

    assert len(channel._client.api.group_calls) == 1
    call = channel._client.api.group_calls[0]
    assert call == {
        "group_openid": "group123",
        "msg_type": 0,
        "content": "hello",
        "msg_id": "msg1",
        "msg_seq": 2,
    }
    assert not channel._client.api.c2c_calls


@pytest.mark.asyncio
async def test_send_c2c_message_uses_plain_text_c2c_api_with_msg_seq() -> None:
    channel = QQChannel(QQConfig(app_id="app", secret="secret", allow_from=["*"]), MessageBus())
    channel._client = _FakeClient()

    await channel.send(
        OutboundMessage(
            channel="qq",
            chat_id="user123",
            content="hello",
            metadata={"message_id": "msg1"},
        )
    )

    assert len(channel._client.api.c2c_calls) == 1
    call = channel._client.api.c2c_calls[0]
    assert call == {
        "openid": "user123",
        "msg_type": 0,
        "content": "hello",
        "msg_id": "msg1",
        "msg_seq": 2,
    }
    assert not channel._client.api.group_calls


@pytest.mark.asyncio
async def test_send_group_remote_media_url_uses_file_api_then_media_message(monkeypatch) -> None:
    channel = QQChannel(QQConfig(app_id="app", secret="secret", allow_from=["*"]), MessageBus())
    channel._client = _FakeClient()
    channel._chat_type_cache["group123"] = "group"
    monkeypatch.setattr("nanobot.channels.qq.validate_url_target", lambda url: (True, ""))

    await channel.send(
        OutboundMessage(
            channel="qq",
            chat_id="group123",
            content="look",
            media=["https://example.com/cat.jpg"],
            metadata={"message_id": "msg1"},
        )
    )

    assert channel._client.api.group_file_calls == [
        {
            "group_openid": "group123",
            "file_type": 1,
            "url": "https://example.com/cat.jpg",
            "srv_send_msg": False,
        }
    ]
    assert channel._client.api.group_calls == [
        {
            "group_openid": "group123",
            "msg_type": 7,
            "content": "look",
            "media": {"file_info": "group-file-info", "file_uuid": "group-file", "ttl": 60},
            "msg_id": "msg1",
            "msg_seq": 2,
        }
    ]
    assert channel._client.api.c2c_calls == []


@pytest.mark.asyncio
async def test_send_local_media_falls_back_to_text_notice_when_publishing_not_configured() -> None:
    channel = QQChannel(QQConfig(app_id="app", secret="secret", allow_from=["*"]), MessageBus())
    channel._client = _FakeClient()

    await channel.send(
        OutboundMessage(
            channel="qq",
            chat_id="user123",
            content="hello",
            media=["/tmp/demo.png"],
            metadata={"message_id": "msg1"},
        )
    )

    assert channel._client.api.c2c_file_calls == []
    assert channel._client.api.group_file_calls == []
    assert channel._client.api.c2c_calls == [
        {
            "openid": "user123",
            "msg_type": 0,
            "content": "hello\n[Failed to send: demo.png - QQ local media publishing is not configured]",
            "msg_id": "msg1",
            "msg_seq": 2,
        }
    ]


@pytest.mark.asyncio
async def test_send_local_media_under_public_dir_uses_c2c_file_api(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    public_dir = workspace / "public" / "qq"
    public_dir.mkdir(parents=True)
    source = public_dir / "demo.png"
    source.write_bytes(b"fake-png")

    channel = QQChannel(
        QQConfig(
            app_id="app",
            secret="secret",
            allow_from=["*"],
            media_base_url="https://files.example.com/public/qq",
            media_public_dir="public/qq",
            media_ttl_seconds=0,
        ),
        MessageBus(),
        workspace=workspace,
    )
    channel._client = _FakeClient()
    monkeypatch.setattr("nanobot.channels.qq.validate_url_target", lambda url: (True, ""))

    await channel.send(
        OutboundMessage(
            channel="qq",
            chat_id="user123",
            content="hello",
            media=[str(source)],
            metadata={"message_id": "msg1"},
        )
    )

    assert channel._client.api.c2c_file_calls == [
        {
            "openid": "user123",
            "file_type": 1,
            "url": "https://files.example.com/public/qq/demo.png",
            "srv_send_msg": False,
        }
    ]
    assert channel._client.api.c2c_calls == [
        {
            "openid": "user123",
            "msg_type": 7,
            "content": "hello",
            "media": {"file_info": "c2c-file-info", "file_uuid": "c2c-file", "ttl": 60},
            "msg_id": "msg1",
            "msg_seq": 2,
        }
    ]


@pytest.mark.asyncio
async def test_send_local_media_from_out_auto_links_into_public_then_uses_c2c_file_api(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    out_dir = workspace / "out"
    out_dir.mkdir()
    source = out_dir / "outside.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nfake-png")

    channel = QQChannel(
        QQConfig(
            app_id="app",
            secret="secret",
            allow_from=["*"],
            media_base_url="https://files.example.com/public/qq",
            media_public_dir="public/qq",
            media_ttl_seconds=0,
        ),
        MessageBus(),
        workspace=workspace,
    )
    channel._client = _FakeClient()
    monkeypatch.setattr("nanobot.channels.qq.validate_url_target", lambda url: (True, ""))

    await channel.send(
        OutboundMessage(
            channel="qq",
            chat_id="user123",
            content="hello",
            media=[str(source)],
            metadata={"message_id": "msg1"},
        )
    )

    published = list((workspace / "public" / "qq").iterdir())
    assert len(published) == 1
    assert published[0].name.startswith("outside-")
    assert published[0].suffix == ".png"
    assert os.stat(source).st_ino == os.stat(published[0]).st_ino
    assert channel._client.api.c2c_file_calls == [
        {
            "openid": "user123",
            "file_type": 1,
            "url": f"https://files.example.com/public/qq/{published[0].name}",
            "srv_send_msg": False,
        }
    ]
    assert channel._client.api.c2c_calls == [
        {
            "openid": "user123",
            "msg_type": 7,
            "content": "hello",
            "media": {"file_info": "c2c-file-info", "file_uuid": "c2c-file", "ttl": 60},
            "msg_id": "msg1",
            "msg_seq": 2,
        }
    ]


@pytest.mark.asyncio
async def test_send_local_media_outside_public_and_out_falls_back_to_text_notice(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    docs_dir = workspace / "docs"
    docs_dir.mkdir()
    source = docs_dir / "outside.png"
    source.write_bytes(b"fake-png")

    channel = QQChannel(
        QQConfig(
            app_id="app",
            secret="secret",
            allow_from=["*"],
            media_base_url="https://files.example.com/public/qq",
            media_public_dir="public/qq",
            media_ttl_seconds=0,
        ),
        MessageBus(),
        workspace=workspace,
    )
    channel._client = _FakeClient()
    monkeypatch.setattr("nanobot.channels.qq.validate_url_target", lambda url: (True, ""))

    await channel.send(
        OutboundMessage(
            channel="qq",
            chat_id="user123",
            content="hello",
            media=[str(source)],
            metadata={"message_id": "msg1"},
        )
    )

    assert channel._client.api.c2c_file_calls == []
    assert channel._client.api.c2c_calls == [
        {
            "openid": "user123",
            "msg_type": 0,
            "content": "hello\n[Failed to send: outside.png - QQ local media must stay under "
            f"{workspace / 'public' / 'qq'} or {workspace / 'out'}]",
            "msg_id": "msg1",
            "msg_seq": 2,
        }
    ]


@pytest.mark.asyncio
async def test_send_local_media_symlink_to_outside_public_dir_is_rejected(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    public_dir = workspace / "public" / "qq"
    public_dir.mkdir(parents=True)
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"secret")
    source = public_dir / "linked.png"
    source.symlink_to(outside)

    channel = QQChannel(
        QQConfig(
            app_id="app",
            secret="secret",
            allow_from=["*"],
            media_base_url="https://files.example.com/public/qq",
            media_public_dir="public/qq",
            media_ttl_seconds=0,
        ),
        MessageBus(),
        workspace=workspace,
    )
    channel._client = _FakeClient()
    monkeypatch.setattr("nanobot.channels.qq.validate_url_target", lambda url: (True, ""))

    await channel.send(
        OutboundMessage(
            channel="qq",
            chat_id="user123",
            content="hello",
            media=[str(source)],
            metadata={"message_id": "msg1"},
        )
    )

    assert channel._client.api.c2c_file_calls == []
    assert channel._client.api.c2c_calls == [
        {
            "openid": "user123",
            "msg_type": 0,
            "content": "hello\n[Failed to send: linked.png - QQ local media must stay under "
            f"{workspace / 'public' / 'qq'} or {workspace / 'out'}]",
            "msg_id": "msg1",
            "msg_seq": 2,
        }
    ]


@pytest.mark.asyncio
async def test_send_non_image_media_from_out_falls_back_to_text_notice(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    out_dir = workspace / "out"
    out_dir.mkdir()
    source = out_dir / "note.txt"
    source.write_text("not an image", encoding="utf-8")

    channel = QQChannel(
        QQConfig(
            app_id="app",
            secret="secret",
            allow_from=["*"],
            media_base_url="https://files.example.com/public/qq",
            media_public_dir="public/qq",
            media_ttl_seconds=0,
        ),
        MessageBus(),
        workspace=workspace,
    )
    channel._client = _FakeClient()
    monkeypatch.setattr("nanobot.channels.qq.validate_url_target", lambda url: (True, ""))

    await channel.send(
        OutboundMessage(
            channel="qq",
            chat_id="user123",
            content="hello",
            media=[str(source)],
            metadata={"message_id": "msg1"},
        )
    )

    assert channel._client.api.c2c_file_calls == []
    assert channel._client.api.c2c_calls == [
        {
            "openid": "user123",
            "msg_type": 0,
            "content": "hello\n[Failed to send: note.txt - QQ local media must be an image]",
            "msg_id": "msg1",
            "msg_seq": 2,
        }
    ]
