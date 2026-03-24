from base64 import b64encode
from types import SimpleNamespace

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.qq import QQChannel, _make_bot_class
from nanobot.config.schema import QQConfig


class _FakeApi:
    def __init__(self) -> None:
        self.c2c_calls: list[dict] = []
        self.group_calls: list[dict] = []
        self.c2c_file_calls: list[dict] = []
        self.group_file_calls: list[dict] = []
        self.raw_file_upload_calls: list[dict] = []
        self.raise_on_raw_file_upload = False
        self._http = SimpleNamespace(request=self._request)

    async def _request(self, route, json=None, **kwargs) -> dict:
        if self.raise_on_raw_file_upload:
            raise RuntimeError("raw upload failed")
        self.raw_file_upload_calls.append(
            {
                "method": route.method,
                "path": route.path,
                "params": route.parameters,
                "json": json,
            }
        )
        if "/groups/" in route.path:
            return {"file_info": "group-file-info", "file_uuid": "group-file", "ttl": 60}
        return {"file_info": "c2c-file-info", "file_uuid": "c2c-file", "ttl": 60}

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


def test_make_bot_class_uses_longer_http_timeout(monkeypatch) -> None:
    if not hasattr(__import__("nanobot.channels.qq", fromlist=["botpy"]).botpy, "Client"):
        pytest.skip("botpy not installed")

    captured: dict[str, object] = {}

    def fake_init(self, *args, **kwargs) -> None:  # noqa: ARG001
        captured["kwargs"] = kwargs

    monkeypatch.setattr("nanobot.channels.qq.botpy.Client.__init__", fake_init)
    bot_cls = _make_bot_class(SimpleNamespace(_on_message=None))
    bot_cls()

    assert captured["kwargs"]["timeout"] == 20
    assert captured["kwargs"]["ext_handlers"] is False


@pytest.mark.asyncio
async def test_on_group_message_routes_to_group_chat_id() -> None:
    channel = QQChannel(QQConfig(app_id="app", secret="secret", allow_from=["user1"]), MessageBus())

    data = SimpleNamespace(
        id="msg1",
        content="hello",
        group_openid="group123",
        author=SimpleNamespace(member_openid="user1"),
        attachments=[],
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
async def test_send_group_message_uses_markdown_when_configured() -> None:
    channel = QQChannel(
        QQConfig(app_id="app", secret="secret", allow_from=["*"], msg_format="markdown"),
        MessageBus(),
    )
    channel._client = _FakeClient()
    channel._chat_type_cache["group123"] = "group"

    await channel.send(
        OutboundMessage(
            channel="qq",
            chat_id="group123",
            content="**hello**",
            metadata={"message_id": "msg1"},
        )
    )

    assert len(channel._client.api.group_calls) == 1
    call = channel._client.api.group_calls[0]
    assert call == {
        "group_openid": "group123",
        "msg_type": 2,
        "markdown": {"content": "**hello**"},
        "msg_id": "msg1",
        "msg_seq": 2,
    }


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
async def test_send_local_media_without_media_base_url_uses_file_data_only(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    out_dir = workspace / "out"
    out_dir.mkdir()
    source = out_dir / "demo.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nfake-png")

    channel = QQChannel(
        QQConfig(app_id="app", secret="secret", allow_from=["*"]),
        MessageBus(),
        workspace=workspace,
    )
    channel._client = _FakeClient()

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
    assert channel._client.api.group_file_calls == []
    assert channel._client.api.raw_file_upload_calls == [
        {
            "method": "POST",
            "path": "/v2/users/{openid}/files",
            "params": {"openid": "user123"},
            "json": {
                "file_type": 1,
                "file_data": b64encode(b"\x89PNG\r\n\x1a\nfake-png").decode("ascii"),
                "srv_send_msg": False,
            },
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
async def test_send_local_media_under_out_dir_uses_c2c_file_api(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    out_dir = workspace / "out"
    out_dir.mkdir()
    source = out_dir / "demo.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nfake-png")

    channel = QQChannel(
        QQConfig(
            app_id="app",
            secret="secret",
            allow_from=["*"],
            media_base_url="https://files.example.com/out",
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

    assert channel._client.api.raw_file_upload_calls == [
        {
            "method": "POST",
            "path": "/v2/users/{openid}/files",
            "params": {"openid": "user123"},
            "json": {
                "file_type": 1,
                "file_data": b64encode(b"\x89PNG\r\n\x1a\nfake-png").decode("ascii"),
                "srv_send_msg": False,
            },
        }
    ]
    assert channel._client.api.c2c_file_calls == []
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
async def test_send_local_media_in_nested_out_path_uses_relative_url(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    out_dir = workspace / "out"
    source_dir = out_dir / "shots"
    source_dir.mkdir(parents=True)
    source = source_dir / "github.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nfake-png")

    channel = QQChannel(
        QQConfig(
            app_id="app",
            secret="secret",
            allow_from=["*"],
            media_base_url="https://files.example.com/qq-media",
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

    assert channel._client.api.raw_file_upload_calls == [
        {
            "method": "POST",
            "path": "/v2/users/{openid}/files",
            "params": {"openid": "user123"},
            "json": {
                "file_type": 1,
                "file_data": b64encode(b"\x89PNG\r\n\x1a\nfake-png").decode("ascii"),
                "srv_send_msg": False,
            },
        }
    ]
    assert channel._client.api.c2c_file_calls == []
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
async def test_send_local_media_outside_out_falls_back_to_text_notice(
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
            media_base_url="https://files.example.com/out",
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
            "content": (
                "hello\n[Failed to send: outside.png - local delivery media must stay under "
                f"{workspace / 'out'}]"
            ),
            "msg_id": "msg1",
            "msg_seq": 2,
        }
    ]


@pytest.mark.asyncio
async def test_send_local_media_with_media_base_url_still_falls_back_to_text_notice_when_file_data_upload_fails(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    out_dir = workspace / "out"
    out_dir.mkdir()
    source = out_dir / "demo.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nfake-png")

    channel = QQChannel(
        QQConfig(
            app_id="app",
            secret="secret",
            allow_from=["*"],
            media_base_url="https://files.example.com/out",
        ),
        MessageBus(),
        workspace=workspace,
    )
    channel._client = _FakeClient()
    channel._client.api.raise_on_raw_file_upload = True

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
            "content": "hello\n[Failed to send: demo.png - QQ local file_data upload failed]",
            "msg_id": "msg1",
            "msg_seq": 2,
        }
    ]


@pytest.mark.asyncio
async def test_send_local_media_without_media_base_url_falls_back_to_text_notice_when_file_data_upload_fails(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    out_dir = workspace / "out"
    out_dir.mkdir()
    source = out_dir / "demo.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nfake-png")

    channel = QQChannel(
        QQConfig(app_id="app", secret="secret", allow_from=["*"]),
        MessageBus(),
        workspace=workspace,
    )
    channel._client = _FakeClient()
    channel._client.api.raise_on_raw_file_upload = True

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
            "content": "hello\n[Failed to send: demo.png - QQ local file_data upload failed]",
            "msg_id": "msg1",
            "msg_seq": 2,
        }
    ]


@pytest.mark.asyncio
async def test_send_local_media_symlink_to_outside_out_dir_is_rejected(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    out_dir = workspace / "out"
    out_dir.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"secret")
    source = out_dir / "linked.png"
    source.symlink_to(outside)

    channel = QQChannel(
        QQConfig(
            app_id="app",
            secret="secret",
            allow_from=["*"],
            media_base_url="https://files.example.com/out",
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
            "content": (
                "hello\n[Failed to send: linked.png - local delivery media must stay under "
                f"{workspace / 'out'}]"
            ),
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
            media_base_url="https://files.example.com/out",
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
            "content": (
                "hello\n[Failed to send: note.txt - local delivery media must be an image, .mp4 video, "
                "or .silk voice]"
            ),
            "msg_id": "msg1",
            "msg_seq": 2,
        }
    ]


@pytest.mark.asyncio
async def test_send_local_silk_voice_uses_file_type_three_direct_upload(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    out_dir = workspace / "out"
    out_dir.mkdir()
    source = out_dir / "reply.silk"
    source.write_bytes(b"fake-silk")

    channel = QQChannel(
        QQConfig(app_id="app", secret="secret", allow_from=["*"]),
        MessageBus(),
        workspace=workspace,
    )
    channel._client = _FakeClient()

    await channel.send(
        OutboundMessage(
            channel="qq",
            chat_id="user123",
            content="hello",
            media=[str(source)],
            metadata={"message_id": "msg1"},
        )
    )

    assert channel._client.api.raw_file_upload_calls == [
        {
            "method": "POST",
            "path": "/v2/users/{openid}/files",
            "params": {"openid": "user123"},
            "json": {
                "file_type": 3,
                "file_data": b64encode(b"fake-silk").decode("ascii"),
                "srv_send_msg": False,
            },
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
