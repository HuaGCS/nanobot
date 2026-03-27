"""Speech synthesis providers used by outbound voice replies."""

from __future__ import annotations

from pathlib import Path

import httpx


class OpenAISpeechProvider:
    """Minimal OpenAI-compatible TTS client."""

    _NO_INSTRUCTIONS_MODELS = {"tts-1", "tts-1-hd"}

    def __init__(self, api_key: str, api_base: str = "https://api.openai.com/v1"):
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")

    def _speech_url(self) -> str:
        """Return the final speech endpoint URL from a base URL or direct endpoint URL."""
        if self.api_base.endswith("/audio/speech"):
            return self.api_base
        return f"{self.api_base}/audio/speech"

    @classmethod
    def _supports_instructions(cls, model: str) -> bool:
        """Return True when the target TTS model accepts style instructions."""
        return model not in cls._NO_INSTRUCTIONS_MODELS

    async def synthesize(
        self,
        text: str,
        *,
        model: str,
        voice: str,
        instructions: str | None = None,
        speed: float | None = None,
        response_format: str,
    ) -> bytes:
        """Synthesize text into audio bytes."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "voice": voice,
            "input": text,
            "response_format": response_format,
        }
        if instructions and self._supports_instructions(model):
            payload["instructions"] = instructions
        if speed is not None:
            payload["speed"] = speed
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                self._speech_url(),
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            return response.content

    async def synthesize_to_file(
        self,
        text: str,
        *,
        model: str,
        voice: str,
        instructions: str | None = None,
        speed: float | None = None,
        response_format: str,
        output_path: str | Path,
    ) -> Path:
        """Synthesize text and write the audio payload to disk."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            await self.synthesize(
                text,
                model=model,
                voice=voice,
                instructions=instructions,
                speed=speed,
                response_format=response_format,
            )
        )
        return path


class EdgeSpeechProvider:
    """Microsoft Edge TTS provider."""

    def __init__(
        self,
        *,
        voice: str = "zh-CN-XiaoxiaoNeural",
        rate: str = "+0%",
        volume: str = "+0%",
    ) -> None:
        self.voice = voice
        self.rate = rate
        self.volume = volume

    async def synthesize_to_file(self, text: str, *, output_path: str | Path) -> Path:
        """Synthesize text with Edge TTS into an audio file."""
        try:
            import edge_tts
        except ImportError as exc:  # pragma: no cover - exercised via runtime env
            raise RuntimeError("edge-tts is not installed. Install it to use provider=edge.") from exc

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        communicate = edge_tts.Communicate(
            text,
            self.voice,
            rate=self.rate,
            volume=self.volume,
        )
        await communicate.save(str(path))
        return path


class GPTSoVITSSpeechProvider:
    """GPT-SoVITS HTTP provider for custom voice cloning."""

    def __init__(
        self,
        *,
        api_url: str = "http://127.0.0.1:9880",
        refer_wav_path: str = "",
        prompt_text: str = "",
        prompt_language: str = "zh",
        text_language: str = "zh",
        cut_punc: str = "，。",
        top_k: int = 5,
        top_p: float = 1.0,
        temperature: float = 1.0,
        speed: float = 1.0,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.refer_wav_path = refer_wav_path
        self.prompt_text = prompt_text
        self.prompt_language = prompt_language
        self.text_language = text_language
        self.cut_punc = cut_punc
        self.top_k = top_k
        self.top_p = top_p
        self.temperature = temperature
        self.speed = speed

    async def synthesize_to_file(self, text: str, *, output_path: str | Path) -> Path:
        """Synthesize text with GPT-SoVITS into an audio file."""
        if not self.refer_wav_path or not self.prompt_text:
            raise ValueError(
                "GPT-SoVITS requires refer_wav_path and prompt_text configuration."
            )

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        params = {
            "text": text,
            "text_language": self.text_language,
            "ref_audio_path": self.refer_wav_path,
            "prompt_text": self.prompt_text,
            "prompt_language": self.prompt_language,
            "cut_punc": self.cut_punc,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "temperature": self.temperature,
            "speed": self.speed,
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(f"{self.api_url}/", params=params)
            response.raise_for_status()
            path.write_bytes(response.content)
        return path
