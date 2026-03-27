"""Tests for the image generation tool."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.tools import image_gen as image_gen_module
from nanobot.agent.tools.image_gen import ImageGenTool
from nanobot.config.schema import Config, ImageGenConfig

_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF9sAAAAASUVORK5CYII="
_PNG_BYTES = base64.b64decode(_PNG_B64)


class _FakeResponse:
    def __init__(
        self,
        payload: dict | None = None,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        status_code: int = 200,
    ) -> None:
        self._payload = payload or {}
        self.headers = headers or {}
        self.content = content or b""
        self.status_code = status_code
        self.text = json.dumps(self._payload)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


@pytest.mark.asyncio
async def test_image_gen_tool_uses_persona_default_reference_for_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    ref_dir = workspace / "personas" / "coder" / "assets"
    manifest_dir = workspace / "personas" / "coder" / ".nanobot"
    ref_dir.mkdir(parents=True)
    manifest_dir.mkdir(parents=True)
    reference_path = ref_dir / "avatar.png"
    reference_path.write_bytes(_PNG_BYTES)
    (manifest_dir / "st_manifest.json").write_text(
        json.dumps({"reference_image": "assets/avatar.png"}),
        encoding="utf-8",
    )

    calls: list[dict] = []

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            self.proxy = kwargs.get("proxy")
            self.timeout = kwargs.get("timeout")

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, **kwargs):
            calls.append({"url": url, **kwargs})
            return _FakeResponse({"data": [{"b64_json": _PNG_B64}]})

    monkeypatch.setattr(image_gen_module.httpx, "AsyncClient", _FakeAsyncClient)

    tool = ImageGenTool(
        workspace=workspace,
        api_key="test-key",
        base_url="https://images.example.com/v1",
        model="gpt-image-1",
    )
    tool.set_persona("coder")

    result = await tool.execute(prompt="Draw a portrait", reference_image="__default__")

    assert "Image generated successfully." in result
    assert calls and calls[0]["url"] == "https://images.example.com/v1/images/edits"
    assert len(calls[0]["files"]) == 1
    assert calls[0]["files"][0][0] == "image"
    output_path = Path(result.split("File path: ", 1)[1].splitlines()[0])
    assert output_path.is_file()
    assert output_path.parent == workspace / "out" / "image_gen"
    assert output_path.read_bytes() == _PNG_BYTES


@pytest.mark.asyncio
async def test_image_gen_tool_restricts_reference_images_to_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_PNG_BYTES)

    tool = ImageGenTool(
        workspace=workspace,
        api_key="test-key",
        restrict_to_workspace=True,
    )

    result = await tool.execute(prompt="Draw a portrait", reference_image=str(outside))

    assert "outside the workspace" in result


def test_image_gen_config_accepts_camel_case_fields() -> None:
    config = Config.model_validate(
        {
            "tools": {
                "imageGen": {
                    "enabled": True,
                    "apiKey": "test-key",
                    "baseUrl": "https://images.example.com/v1",
                    "referenceImage": "personas/Aria/assets/default.png",
                }
            }
        }
    )

    assert config.tools.image_gen.enabled is True
    assert config.tools.image_gen.api_key == "test-key"
    assert config.tools.image_gen.base_url == "https://images.example.com/v1"
    assert config.tools.image_gen.reference_image == "personas/Aria/assets/default.png"


def test_agent_loop_registers_image_gen_tool_when_enabled(tmp_path: Path) -> None:
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = MagicMock(max_tokens=4096)

    with patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=tmp_path,
            image_gen_config=ImageGenConfig(enabled=True, api_key="test-key"),
        )

    assert loop.tools.has("image_gen")
