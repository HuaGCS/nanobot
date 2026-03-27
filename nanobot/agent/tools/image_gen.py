"""Image generation tool with persona reference-image support."""

from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from nanobot.agent.personas import resolve_persona_reference_image
from nanobot.agent.tools.base import Tool
from nanobot.utils.helpers import detect_image_mime, ensure_dir

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_OPENAI_SIZE_DEFAULT = "1024x1024"
_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_SUFFIX_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


@dataclass(frozen=True)
class _ReferenceImage:
    path: Path
    data: bytes
    mime_type: str


class ImageGenTool(Tool):
    """Generate or edit images and save them under workspace/out."""

    def __init__(
        self,
        *,
        workspace: Path,
        api_key: str = "",
        base_url: str = _DEFAULT_BASE_URL,
        model: str = "gpt-image-1",
        proxy: str | None = None,
        timeout: int = 180,
        reference_image: str = "",
        restrict_to_workspace: bool = False,
    ) -> None:
        self._workspace = workspace
        self._api_key = api_key
        self.base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self.model = model
        self.proxy = proxy
        self.timeout = timeout
        self._default_reference = reference_image
        self.restrict_to_workspace = restrict_to_workspace
        self._persona: str | None = None

    def update_config(
        self,
        *,
        workspace: Path,
        api_key: str,
        base_url: str,
        model: str,
        proxy: str | None,
        timeout: int,
        reference_image: str,
        restrict_to_workspace: bool,
    ) -> None:
        """Update the tool configuration in place for hot reloads."""
        self._workspace = workspace
        self._api_key = api_key
        self.base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self.model = model
        self.proxy = proxy
        self.timeout = timeout
        self._default_reference = reference_image
        self.restrict_to_workspace = restrict_to_workspace

    def set_persona(self, persona: str | None) -> None:
        """Bind the current session persona for __default__ reference resolution."""
        self._persona = persona

    @property
    def name(self) -> str:
        return "image_gen"

    @property
    def description(self) -> str:
        return (
            "Generate an image from a prompt, or edit existing image files when "
            "'reference_image' is provided. Separate multiple reference images with '|'. "
            "Use '__default__' to load the active persona's default reference image, or "
            "'__default__:scene' for a scene-specific reference configured in that persona's "
            ".nanobot/st_manifest.json. Generated files are saved under workspace/out, and "
            "you must call the 'message' tool with the returned path in 'media' to send the "
            "image to the user."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "Describe the image to generate, or the edit/composition instructions to "
                        "apply to the reference image(s)."
                    ),
                },
                "reference_image": {
                    "type": "string",
                    "description": (
                        "Optional local file path(s) used as reference input. Separate multiple "
                        "paths with '|'. Use '__default__' or '__default__:scene' to resolve the "
                        "active persona's configured reference images."
                    ),
                },
                "size": {
                    "type": "string",
                    "description": "Optional size such as '1024x1024', '1792x1024', or '1024x1792'.",
                },
                "quality": {
                    "type": "string",
                    "enum": ["standard", "hd"],
                    "description": "Optional quality hint for OpenAI-compatible image APIs.",
                },
                "style": {
                    "type": "string",
                    "enum": ["vivid", "natural"],
                    "description": "Optional style hint for OpenAI-compatible image APIs.",
                },
            },
            "required": ["prompt"],
        }

    def _is_gemini_model(self) -> bool:
        lower = self.model.lower()
        return "gemini" in lower and "image" in lower

    def _is_grok_model(self) -> bool:
        lower = self.model.lower()
        return "grok" in lower or "aurora" in lower

    def _default_output_dir(self) -> Path:
        return ensure_dir(self._workspace / "out" / "image_gen")

    def _resolve_default_reference(self, scene: str | None = None) -> Path | None:
        resolved = resolve_persona_reference_image(self._workspace, self._persona, scene)
        if resolved:
            return Path(resolved)

        if self._default_reference:
            return self._resolve_literal_path(self._default_reference)
        return None

    def _resolve_literal_path(self, raw: str) -> Path | None:
        cleaned = raw.strip()
        if not cleaned:
            return None

        path = Path(cleaned).expanduser()
        candidate = path.resolve(strict=False) if path.is_absolute() else (self._workspace / path).resolve(
            strict=False
        )
        if self.restrict_to_workspace:
            workspace_root = self._workspace.resolve(strict=False)
            if not candidate.is_relative_to(workspace_root):
                raise PermissionError(
                    f"Reference image path is outside the workspace: {candidate}"
                )
        return candidate

    def _resolve_reference_token(self, token: str) -> Path | None:
        cleaned = token.strip()
        if not cleaned:
            return None

        if cleaned == "__default__":
            return self._resolve_default_reference()
        if cleaned.startswith("__default__:"):
            scene = cleaned.split(":", 1)[1].strip()
            return self._resolve_default_reference(scene or None)
        return self._resolve_literal_path(cleaned)

    def _reference_tokens(self, value: str | None) -> list[str]:
        if not value:
            return []
        return [token.strip() for token in value.split("|") if token.strip()]

    def _load_reference_images(self, reference_image: str | None) -> list[_ReferenceImage]:
        refs: list[_ReferenceImage] = []
        for token in self._reference_tokens(reference_image):
            path = self._resolve_reference_token(token)
            if path is None:
                raise FileNotFoundError(f"No reference image configured for selector: {token}")
            if not path.exists() or not path.is_file():
                raise FileNotFoundError(f"Reference image not found: {path}")
            data = path.read_bytes()
            mime = detect_image_mime(data) or _SUFFIX_MIME.get(path.suffix.lower()) or "image/jpeg"
            refs.append(_ReferenceImage(path=path, data=data, mime_type=mime))
        return refs

    async def execute(
        self,
        prompt: str,
        reference_image: str | None = None,
        size: str | None = None,
        quality: str = "standard",
        style: str = "vivid",
        **kwargs: Any,
    ) -> str:
        if not self._api_key:
            return "Error: Image generation API key not configured. Set tools.imageGen.apiKey in config."

        try:
            ref_images = self._load_reference_images(reference_image)
        except PermissionError as exc:
            return f"Error: {exc}"
        except OSError as exc:
            return f"Error: {exc}"

        if self._is_gemini_model():
            return await self._execute_gemini(prompt, size, ref_images)
        if ref_images:
            if self._is_grok_model():
                return await self._execute_grok_edit(prompt, ref_images)
            return await self._execute_openai_edit(prompt, size, quality, style, ref_images)
        return await self._execute_openai(prompt, size, quality, style)

    async def _execute_openai(
        self,
        prompt: str,
        size: str | None,
        quality: str,
        style: str,
    ) -> str:
        url = f"{self.base_url}/images/generations"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "size": size or _OPENAI_SIZE_DEFAULT,
        }
        if not self._is_grok_model():
            if quality and quality != "standard":
                body["quality"] = quality
            if style and style != "vivid":
                body["style"] = style

        logger.info("ImageGen: generating image with model {}", self.model)
        try:
            async with httpx.AsyncClient(
                proxy=self.proxy,
                timeout=float(self.timeout),
                trust_env=True,
            ) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                return await self._handle_openai_image_payload(resp.json())
        except httpx.HTTPStatusError as exc:
            return self._format_http_error(exc)
        except httpx.TimeoutException:
            return f"Error: Request timed out after {self.timeout}s."
        except Exception as exc:
            logger.exception("Image generation failed")
            return f"Error generating image: {exc}"

    async def _execute_openai_edit(
        self,
        prompt: str,
        size: str | None,
        quality: str,
        style: str,
        ref_images: list[_ReferenceImage],
    ) -> str:
        url = f"{self.base_url}/images/edits"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        data: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "size": size or _OPENAI_SIZE_DEFAULT,
        }
        if quality and quality != "standard":
            data["quality"] = quality
        if style and style != "vivid":
            data["style"] = style

        files = [
            (
                "image",
                (
                    ref.path.name or f"reference{index}{_MIME_EXTENSIONS.get(ref.mime_type, '.png')}",
                    ref.data,
                    ref.mime_type,
                ),
            )
            for index, ref in enumerate(ref_images, start=1)
        ]

        logger.info("ImageGen: editing image with {} reference inputs", len(ref_images))
        try:
            async with httpx.AsyncClient(
                proxy=self.proxy,
                timeout=float(self.timeout),
                trust_env=True,
            ) as client:
                resp = await client.post(url, data=data, files=files, headers=headers)
                resp.raise_for_status()
                return await self._handle_openai_image_payload(resp.json())
        except httpx.HTTPStatusError as exc:
            return self._format_http_error(exc)
        except httpx.TimeoutException:
            return f"Error: Request timed out after {self.timeout}s."
        except Exception as exc:
            logger.exception("Image edit failed")
            return f"Error editing image: {exc}"

    async def _execute_gemini(
        self,
        prompt: str,
        size: str | None,
        ref_images: list[_ReferenceImage],
    ) -> str:
        aspect_ratio = "1:1"
        image_size = "2K"
        if size:
            parts = size.lower().split("x")
            if len(parts) == 2:
                try:
                    width, height = int(parts[0]), int(parts[1])
                    if width == height:
                        aspect_ratio = "1:1"
                    elif width > height:
                        aspect_ratio = "16:9" if width / height >= 1.7 else "4:3"
                    else:
                        aspect_ratio = "9:16" if height / width >= 1.7 else "3:4"
                    image_size = "1K" if max(width, height) <= 1024 else "2K"
                    if max(width, height) > 2048:
                        image_size = "4K"
                except ValueError:
                    pass

        body_parts: list[dict[str, Any]] = []
        for ref in ref_images:
            body_parts.append(
                {
                    "inlineData": {
                        "mimeType": ref.mime_type,
                        "data": base64.b64encode(ref.data).decode(),
                    }
                }
            )
        body_parts.append({"text": prompt})
        body = {
            "contents": [{"parts": body_parts}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {
                    "aspectRatio": aspect_ratio,
                    "image_size": image_size,
                },
            },
        }
        url = f"{self.base_url}/v1beta/models/{self.model}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        try:
            async with httpx.AsyncClient(
                proxy=self.proxy,
                timeout=float(max(self.timeout, 300)),
                trust_env=True,
            ) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                parts = data["candidates"][0]["content"]["parts"]
                image_part = next(part for part in parts if "inlineData" in part)
                return await self._save_base64(image_part["inlineData"]["data"])
        except httpx.HTTPStatusError as exc:
            return self._format_http_error(exc)
        except httpx.TimeoutException:
            return f"Error: Request timed out after {max(self.timeout, 300)}s."
        except Exception as exc:
            logger.exception("Gemini image generation failed")
            return f"Error generating image: {exc}"

    async def _execute_grok_edit(self, prompt: str, ref_images: list[_ReferenceImage]) -> str:
        url = f"{self.base_url}/images/edits"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        images = [
            {
                "type": "image_url",
                "url": f"data:{ref.mime_type};base64,{base64.b64encode(ref.data).decode()}",
            }
            for ref in ref_images
        ]
        body: dict[str, Any] = {"model": self.model, "prompt": prompt}
        body["image" if len(images) == 1 else "images"] = images[0] if len(images) == 1 else images

        try:
            async with httpx.AsyncClient(
                proxy=self.proxy,
                timeout=float(self.timeout),
                trust_env=True,
            ) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                return await self._handle_openai_image_payload(resp.json())
        except httpx.HTTPStatusError as exc:
            return self._format_http_error(exc)
        except httpx.TimeoutException:
            return f"Error: Request timed out after {self.timeout}s."
        except Exception as exc:
            logger.exception("Grok image edit failed")
            return f"Error editing image: {exc}"

    async def _handle_openai_image_payload(self, data: dict[str, Any]) -> str:
        images = data.get("data")
        if not isinstance(images, list) or not images:
            return "Error: No image data returned by the API."
        image_payload = images[0]
        if not isinstance(image_payload, dict):
            return "Error: Unexpected image payload shape."
        if isinstance(image_payload.get("b64_json"), str):
            return await self._save_base64(image_payload["b64_json"])
        if isinstance(image_payload.get("url"), str):
            return await self._download_image(image_payload["url"])
        return "Error: No usable image payload returned by the API."

    def _success_message(self, path: Path) -> str:
        return (
            f"Image generated successfully.\nFile path: {path}\n\n"
            f"Next step: call the 'message' tool with media=['{path}'] to send it to the user."
        )

    async def _save_base64(self, b64_data: str) -> str:
        try:
            path = self._default_output_dir() / f"gen_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.png"
            path.write_bytes(base64.b64decode(b64_data))
            return self._success_message(path)
        except Exception as exc:
            return f"Error saving image: {exc}"

    async def _download_image(self, url: str) -> str:
        try:
            async with httpx.AsyncClient(
                proxy=self.proxy,
                timeout=float(min(self.timeout, 60)),
                trust_env=True,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                mime = resp.headers.get("content-type", "").split(";", 1)[0].strip()
                suffix = _MIME_EXTENSIONS.get(mime, ".png")
                path = self._default_output_dir() / f"gen_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}{suffix}"
                path.write_bytes(resp.content)
                return self._success_message(path)
        except httpx.HTTPStatusError as exc:
            return self._format_http_error(exc)
        except httpx.TimeoutException:
            return f"Error: Download timed out after {min(self.timeout, 60)}s."
        except Exception as exc:
            return f"Error downloading image: {exc}"

    def _format_http_error(self, exc: httpx.HTTPStatusError) -> str:
        try:
            payload = exc.response.json()
        except Exception:
            payload = {}
        message = ""
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "")
            elif isinstance(error, str):
                message = error
        if not message:
            try:
                message = exc.response.text[:300]
            except Exception:
                message = str(exc)
        return f"Error: API {exc.response.status_code} - {message}"
