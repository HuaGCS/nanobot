"""Focused tests for current QQ media helpers and file_data upload paths."""

from __future__ import annotations

from base64 import b64encode
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    from nanobot.channels import qq

    QQ_AVAILABLE = getattr(qq, "QQ_AVAILABLE", False)
except ImportError:
    QQ_AVAILABLE = False

if not QQ_AVAILABLE:
    pytest.skip("QQ dependencies not installed (qq-botpy)", allow_module_level=True)

from nanobot.bus.queue import MessageBus
from nanobot.channels.qq import QQChannel, QQConfig, _is_image_name, _sanitize_filename


class _FakeHttp:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def request(self, route, json=None, **kwargs) -> dict:
        self.calls.append(
            {
                "method": route.method,
                "path": route.path,
                "params": route.parameters,
                "json": json,
            }
        )
        return {"file_info": "uploaded"}


class _FakeApi:
    def __init__(self) -> None:
        self._http = _FakeHttp()
        self.group_calls: list[dict] = []
        self.c2c_calls: list[dict] = []
        self.group_file_calls: list[dict] = []
        self.c2c_file_calls: list[dict] = []

    async def post_group_message(self, **kwargs) -> None:
        self.group_calls.append(kwargs)

    async def post_c2c_message(self, **kwargs) -> None:
        self.c2c_calls.append(kwargs)

    async def post_group_file(self, **kwargs) -> dict:
        self.group_file_calls.append(kwargs)
        return {"file_info": "group-file"}

    async def post_c2c_file(self, **kwargs) -> dict:
        self.c2c_file_calls.append(kwargs)
        return {"file_info": "c2c-file"}


class _FakeClient:
    def __init__(self) -> None:
        self.api = _FakeApi()


def _make_channel(workspace: Path) -> QQChannel:
    channel = QQChannel(
        QQConfig(app_id="app", secret="secret", allow_from=["*"]),
        MessageBus(),
        workspace=workspace,
    )
    channel._client = _FakeClient()
    return channel


def test_sanitize_filename_blocks_traversal() -> None:
    assert _sanitize_filename("../../etc/passwd") == "passwd"


def test_is_image_name_knows_common_extensions() -> None:
    assert _is_image_name("demo.png") is True
    assert _is_image_name("demo.jpg") is True
    assert _is_image_name("demo.pdf") is False


def test_remote_media_file_type_detects_supported_kinds(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    assert channel._remote_media_file_type("https://x.test/demo.png") == 1
    assert channel._remote_media_file_type("https://x.test/demo.mp4") == 2
    assert channel._remote_media_file_type("https://x.test/demo.silk") == 3
    assert channel._remote_media_file_type("https://x.test/demo.pdf") is None


def test_resolve_local_media_accepts_workspace_out_image(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    target = tmp_path / "out" / "demo.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    resolved, file_type, error = channel._resolve_local_media(str(target))

    assert resolved == target.resolve()
    assert file_type == 1
    assert error is None


def test_resolve_local_media_rejects_non_delivery_path(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    target = tmp_path / "demo.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    resolved, file_type, error = channel._resolve_local_media(str(target))

    assert resolved is None
    assert file_type is None
    assert "local delivery media must stay under" in (error or "")


def test_resolve_local_media_rejects_unsupported_suffix(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    target = tmp_path / "out" / "demo.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("fake", encoding="utf-8")

    resolved, file_type, error = channel._resolve_local_media(str(target))

    assert resolved is None
    assert file_type is None
    assert ".mp4" in (error or "")


@pytest.mark.asyncio
async def test_post_local_media_message_uses_file_data_for_c2c(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    target = tmp_path / "out" / "demo.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = b"\x89PNG\r\n\x1a\nfake-png"
    target.write_bytes(raw)

    await channel._post_local_media_message(
        chat_id="user1",
        msg_type="c2c",
        file_type=1,
        local_path=target,
        content="hello",
        msg_id="m1",
    )

    assert channel._client.api._http.calls == [
        {
            "method": "POST",
            "path": "/v2/users/{openid}/files",
            "params": {"openid": "user1"},
            "json": {
                "file_type": 1,
                "file_data": b64encode(raw).decode("ascii"),
                "srv_send_msg": False,
            },
        }
    ]
    assert channel._client.api.c2c_calls == [
        {
            "openid": "user1",
            "msg_type": 7,
            "content": "hello",
            "media": {"file_info": "uploaded"},
            "msg_id": "m1",
            "msg_seq": 2,
        }
    ]


@pytest.mark.asyncio
async def test_post_remote_media_message_uses_group_file_api(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)

    await channel._post_remote_media_message(
        chat_id="group1",
        msg_type="group",
        file_type=2,
        media_url="https://example.com/demo.mp4",
        content="watch",
        msg_id="m1",
    )

    assert channel._client.api.group_file_calls == [
        {
            "group_openid": "group1",
            "file_type": 2,
            "url": "https://example.com/demo.mp4",
            "srv_send_msg": False,
        }
    ]
    assert channel._client.api.group_calls == [
        {
            "group_openid": "group1",
            "msg_type": 7,
            "content": "watch",
            "media": {"file_info": "group-file"},
            "msg_id": "m1",
            "msg_seq": 2,
        }
    ]
