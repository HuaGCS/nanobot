"""OpenAI-compatible text-to-speech provider."""

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
