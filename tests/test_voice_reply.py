"""Tests for optional outbound voice replies."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.bus.events import InboundMessage
from nanobot.config.schema import Config
from nanobot.providers.base import LLMResponse
from nanobot.providers.speech import (
    EdgeSpeechProvider,
    GPTSoVITSSpeechProvider,
    OpenAISpeechProvider,
)


def _make_loop(workspace: Path, *, channels_payload: dict | None = None):
    """Create an AgentLoop with lightweight mocks and configurable channels."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="hello", tool_calls=[]))
    provider.api_key = ""
    provider.api_base = None

    config = Config.model_validate({"channels": channels_payload or {}})

    with patch("nanobot.agent.loop.SubagentManager"):
        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=workspace,
            channels_config=config.channels,
        )
    return loop, provider


def test_voice_reply_config_parses_camel_case() -> None:
    config = Config.model_validate(
        {
            "channels": {
                "voiceReply": {
                    "enabled": True,
                    "channels": ["telegram/main"],
                    "model": "gpt-4o-mini-tts",
                    "voice": "alloy",
                    "instructions": "sound calm",
                    "speed": 1.1,
                    "responseFormat": "mp3",
                    "apiKey": "tts-key",
                    "url": "https://tts.example.com/v1",
                }
            }
        }
    )

    voice_reply = config.channels.voice_reply
    assert voice_reply.enabled is True
    assert voice_reply.channels == ["telegram/main"]
    assert voice_reply.instructions == "sound calm"
    assert voice_reply.speed == 1.1
    assert voice_reply.response_format == "mp3"
    assert voice_reply.api_key == "tts-key"
    assert voice_reply.api_base == "https://tts.example.com/v1"


def test_voice_reply_config_parses_provider_specific_fields() -> None:
    config = Config.model_validate(
        {
            "channels": {
                "voiceReply": {
                    "enabled": True,
                    "provider": "sovits",
                    "sovitsApiUrl": "http://127.0.0.1:9880",
                    "sovitsReferWavPath": "/tmp/ref.wav",
                    "sovitsPromptText": "hello there",
                    "sovitsPromptLanguage": "en",
                    "sovitsTextLanguage": "en",
                    "sovitsCutPunc": ",.",
                    "sovitsTopK": 10,
                    "sovitsTopP": 0.9,
                    "sovitsTemperature": 0.7,
                    "edgeVoice": "en-US-JennyNeural",
                    "edgeRate": "+10%",
                    "edgeVolume": "+5%",
                }
            }
        }
    )

    voice_reply = config.channels.voice_reply
    assert voice_reply.provider == "sovits"
    assert voice_reply.sovits_api_url == "http://127.0.0.1:9880"
    assert voice_reply.sovits_refer_wav_path == "/tmp/ref.wav"
    assert voice_reply.sovits_prompt_text == "hello there"
    assert voice_reply.sovits_prompt_language == "en"
    assert voice_reply.sovits_text_language == "en"
    assert voice_reply.sovits_cut_punc == ",."
    assert voice_reply.sovits_top_k == 10
    assert voice_reply.sovits_top_p == 0.9
    assert voice_reply.sovits_temperature == 0.7
    assert voice_reply.edge_voice == "en-US-JennyNeural"
    assert voice_reply.edge_rate == "+10%"
    assert voice_reply.edge_volume == "+5%"


def test_openai_speech_provider_accepts_direct_endpoint_url() -> None:
    provider = OpenAISpeechProvider(
        api_key="tts-key",
        api_base="https://tts.example.com/v1/audio/speech",
    )

    assert provider._speech_url() == "https://tts.example.com/v1/audio/speech"


@pytest.mark.asyncio
async def test_telegram_voice_reply_attaches_audio_for_multi_instance_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "SOUL.md").write_text("default soul voice", encoding="utf-8")
    loop, provider = _make_loop(
        tmp_path,
        channels_payload={
            "voiceReply": {
                "enabled": True,
                "channels": ["telegram"],
                "instructions": "keep the delivery warm",
                "speed": 1.05,
                "responseFormat": "opus",
            }
        },
    )
    provider.api_key = "provider-tts-key"
    provider.api_base = "https://provider.example.com/v1"

    captured: dict[str, str | float | None] = {}

    async def fake_synthesize_to_file(
        self,
        text: str,
        *,
        model: str,
        voice: str,
        instructions: str | None,
        speed: float | None,
        response_format: str,
        output_path: str | Path,
    ) -> Path:
        path = Path(output_path)
        path.write_bytes(b"voice-bytes")
        captured["api_key"] = self.api_key
        captured["api_base"] = self.api_base
        captured["text"] = text
        captured["model"] = model
        captured["voice"] = voice
        captured["instructions"] = instructions
        captured["speed"] = speed
        captured["response_format"] = response_format
        return path

    monkeypatch.setattr(OpenAISpeechProvider, "synthesize_to_file", fake_synthesize_to_file)

    response = await loop._process_message(
        InboundMessage(
            channel="telegram/main",
            sender_id="user-1",
            chat_id="chat-1",
            content="hello",
        )
    )

    assert response is not None
    assert response.content == "hello"
    assert len(response.media) == 1

    media_path = Path(response.media[0])
    assert media_path.parent == tmp_path / "out" / "voice"
    assert media_path.suffix == ".ogg"
    assert media_path.read_bytes() == b"voice-bytes"

    assert captured == {
        "api_key": "provider-tts-key",
        "api_base": "https://provider.example.com/v1",
        "text": "hello",
        "model": "gpt-4o-mini-tts",
        "voice": "alloy",
        "instructions": (
            "Speak as the active persona 'default'. Match that persona's tone, attitude, pacing, "
            "and emotional style while keeping the reply natural and conversational. keep the "
            "delivery warm Persona guidance: default soul voice"
        ),
        "speed": 1.05,
        "response_format": "opus",
    }


@pytest.mark.asyncio
async def test_persona_voice_settings_override_global_voice_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "SOUL.md").write_text("default soul", encoding="utf-8")
    persona_dir = tmp_path / "personas" / "coder"
    persona_dir.mkdir(parents=True)
    (persona_dir / "SOUL.md").write_text("speak like a sharp engineer", encoding="utf-8")
    (persona_dir / "USER.md").write_text("be concise and technical", encoding="utf-8")
    (persona_dir / "VOICE.json").write_text(
        '{"voice":"nova","instructions":"use a crisp and confident delivery","speed":1.2}',
        encoding="utf-8",
    )

    loop, provider = _make_loop(
        tmp_path,
        channels_payload={
            "voiceReply": {
                "enabled": True,
                "channels": ["telegram"],
                "voice": "alloy",
                "instructions": "keep the pacing steady",
            }
        },
    )
    provider.api_key = "provider-tts-key"

    session = loop.sessions.get_or_create("telegram:chat-1")
    session.metadata["persona"] = "coder"
    loop.sessions.save(session)

    captured: dict[str, str | float | None] = {}

    async def fake_synthesize_to_file(
        self,
        text: str,
        *,
        model: str,
        voice: str,
        instructions: str | None,
        speed: float | None,
        response_format: str,
        output_path: str | Path,
    ) -> Path:
        path = Path(output_path)
        path.write_bytes(b"voice-bytes")
        captured["voice"] = voice
        captured["instructions"] = instructions
        captured["speed"] = speed
        return path

    monkeypatch.setattr(OpenAISpeechProvider, "synthesize_to_file", fake_synthesize_to_file)

    response = await loop._process_message(
        InboundMessage(
            channel="telegram",
            sender_id="user-1",
            chat_id="chat-1",
            content="hello",
        )
    )

    assert response is not None
    assert len(response.media) == 1
    assert captured["voice"] == "nova"
    assert captured["speed"] == 1.2
    assert isinstance(captured["instructions"], str)
    assert "active persona 'coder'" in captured["instructions"]
    assert "keep the pacing steady" in captured["instructions"]
    assert "use a crisp and confident delivery" in captured["instructions"]
    assert "speak like a sharp engineer" in captured["instructions"]
    assert "be concise and technical" in captured["instructions"]


@pytest.mark.asyncio
async def test_qq_voice_reply_config_keeps_text_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop, provider = _make_loop(
        tmp_path,
        channels_payload={
            "voiceReply": {
                "enabled": True,
                "channels": ["qq"],
                "apiKey": "tts-key",
            }
        },
    )
    provider.api_key = "provider-tts-key"

    synthesize = AsyncMock()
    monkeypatch.setattr(OpenAISpeechProvider, "synthesize_to_file", synthesize)

    response = await loop._process_message(
        InboundMessage(
            channel="qq",
            sender_id="user-1",
            chat_id="chat-1",
            content="hello",
        )
    )

    assert response is not None
    assert response.content == "hello"
    assert response.media == []
    synthesize.assert_not_awaited()


@pytest.mark.asyncio
async def test_qq_voice_reply_uses_silk_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop, provider = _make_loop(
        tmp_path,
        channels_payload={
            "voiceReply": {
                "enabled": True,
                "channels": ["qq"],
                "apiKey": "tts-key",
                "responseFormat": "silk",
            }
        },
    )
    provider.api_key = "provider-tts-key"

    captured: dict[str, str | None] = {}

    async def fake_synthesize_to_file(
        self,
        text: str,
        *,
        model: str,
        voice: str,
        instructions: str | None,
        speed: float | None,
        response_format: str,
        output_path: str | Path,
    ) -> Path:
        path = Path(output_path)
        path.write_bytes(b"fake-silk")
        captured["response_format"] = response_format
        return path

    monkeypatch.setattr(OpenAISpeechProvider, "synthesize_to_file", fake_synthesize_to_file)

    response = await loop._process_message(
        InboundMessage(
            channel="qq",
            sender_id="user-1",
            chat_id="chat-1",
            content="hello",
        )
    )

    assert response is not None
    assert response.content == "hello"
    assert len(response.media) == 1
    assert Path(response.media[0]).suffix == ".silk"
    assert captured["response_format"] == "silk"


@pytest.mark.asyncio
async def test_edge_voice_reply_uses_edge_provider_without_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop, provider = _make_loop(
        tmp_path,
        channels_payload={
            "voiceReply": {
                "enabled": True,
                "channels": ["telegram"],
                "provider": "edge",
                "edgeVoice": "en-US-JennyNeural",
                "edgeRate": "+15%",
                "edgeVolume": "+10%",
            }
        },
    )
    provider.api_key = ""

    captured: dict[str, str] = {}

    async def fake_synthesize_to_file(self, text: str, *, output_path: str | Path) -> Path:
        path = Path(output_path)
        path.write_bytes(b"edge-voice")
        captured["voice"] = self.voice
        captured["rate"] = self.rate
        captured["volume"] = self.volume
        return path

    monkeypatch.setattr(EdgeSpeechProvider, "synthesize_to_file", fake_synthesize_to_file)

    response = await loop._process_message(
        InboundMessage(
            channel="telegram",
            sender_id="user-1",
            chat_id="chat-1",
            content="hello",
        )
    )

    assert response is not None
    assert len(response.media) == 1
    assert Path(response.media[0]).suffix == ".mp3"
    assert captured == {
        "voice": "en-US-JennyNeural",
        "rate": "+15%",
        "volume": "+10%",
    }


@pytest.mark.asyncio
async def test_sovits_voice_reply_uses_persona_voice_override_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persona_dir = tmp_path / "personas" / "companion"
    persona_dir.mkdir(parents=True)
    (persona_dir / "VOICE.json").write_text(
        (
            '{"provider":"sovits","apiBase":"http://localhost:9880","referWavPath":"/tmp/ref.wav",'
            '"promptText":"reference transcript","promptLanguage":"zh","textLanguage":"zh",'
            '"cutPunc":"，。","topK":8,"topP":0.85,"temperature":0.65,"speed":1.1}'
        ),
        encoding="utf-8",
    )

    loop, provider = _make_loop(
        tmp_path,
        channels_payload={
            "voiceReply": {
                "enabled": True,
                "channels": ["telegram"],
            }
        },
    )
    provider.api_key = ""

    session = loop.sessions.get_or_create("telegram:chat-1")
    session.metadata["persona"] = "companion"
    loop.sessions.save(session)

    captured: dict[str, str | float | int] = {}

    async def fake_synthesize_to_file(self, text: str, *, output_path: str | Path) -> Path:
        path = Path(output_path)
        path.write_bytes(b"sovits-voice")
        captured["api_url"] = self.api_url
        captured["refer_wav_path"] = self.refer_wav_path
        captured["prompt_text"] = self.prompt_text
        captured["prompt_language"] = self.prompt_language
        captured["text_language"] = self.text_language
        captured["cut_punc"] = self.cut_punc
        captured["top_k"] = self.top_k
        captured["top_p"] = self.top_p
        captured["temperature"] = self.temperature
        captured["speed"] = self.speed
        return path

    monkeypatch.setattr(
        GPTSoVITSSpeechProvider,
        "synthesize_to_file",
        fake_synthesize_to_file,
    )

    response = await loop._process_message(
        InboundMessage(
            channel="telegram",
            sender_id="user-1",
            chat_id="chat-1",
            content="hello",
        )
    )

    assert response is not None
    assert len(response.media) == 1
    assert Path(response.media[0]).suffix == ".wav"
    assert captured == {
        "api_url": "http://localhost:9880",
        "refer_wav_path": "/tmp/ref.wav",
        "prompt_text": "reference transcript",
        "prompt_language": "zh",
        "text_language": "zh",
        "cut_punc": "，。",
        "top_k": 8,
        "top_p": 0.85,
        "temperature": 0.65,
        "speed": 1.1,
    }
