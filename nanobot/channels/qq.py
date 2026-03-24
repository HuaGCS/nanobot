"""QQ channel implementation using botpy SDK."""

import asyncio
import base64
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiohttp
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import QQConfig, QQInstanceConfig
from nanobot.security.network import validate_url_target
from nanobot.utils.delivery import delivery_artifacts_root, is_image_file

try:
    from nanobot.config.paths import get_media_dir
except Exception:  # pragma: no cover
    get_media_dir = None  # type: ignore

try:
    import botpy
    from botpy.http import Route
    from botpy.message import C2CMessage, GroupMessage

    QQ_AVAILABLE = True
except ImportError:
    QQ_AVAILABLE = False
    botpy = None
    Route = None
    C2CMessage = None
    GroupMessage = None

if TYPE_CHECKING:
    from botpy.http import Route
    from botpy.message import C2CMessage, GroupMessage


_IMAGE_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
    ".ico",
    ".svg",
}
_SAFE_NAME_RE = re.compile(r"[^\w.\-()\[\]（）【】\u4e00-\u9fff]+", re.UNICODE)


def _sanitize_filename(name: str) -> str:
    """Sanitize filename to avoid traversal and problematic characters."""
    name = Path(name or "").name.strip()
    name = _SAFE_NAME_RE.sub("_", name).strip("._ ")
    return name


def _is_image_name(name: str) -> bool:
    """Return whether the file name looks like an image."""
    return Path(name).suffix.lower() in _IMAGE_EXTS


def _make_bot_class(channel: "QQChannel") -> "type[botpy.Client]":
    """Create a botpy Client subclass bound to the given channel."""
    intents = botpy.Intents(public_messages=True, direct_message=True)
    http_timeout_seconds = 20

    class _Bot(botpy.Client):
        def __init__(self):
            # Disable botpy's file log — nanobot uses loguru; default "botpy.log" fails on read-only fs
            super().__init__(intents=intents, timeout=http_timeout_seconds, ext_handlers=False)

        async def on_ready(self):
            logger.info("QQ bot ready: {}", self.robot.name)

        async def on_c2c_message_create(self, message: "C2CMessage"):
            await channel._on_message(message, is_group=False)

        async def on_group_at_message_create(self, message: "GroupMessage"):
            await channel._on_message(message, is_group=True)

        async def on_direct_message_create(self, message):
            await channel._on_message(message, is_group=False)

    return _Bot


class QQChannel(BaseChannel):
    """QQ channel using botpy SDK with WebSocket connection."""

    name = "qq"
    display_name = "QQ"

    @classmethod
    def default_config(cls) -> dict[str, object]:
        return QQConfig().model_dump(by_alias=True)

    def __init__(
        self,
        config: QQConfig | QQInstanceConfig | dict,
        bus: MessageBus,
        workspace: str | Path | None = None,
    ):
        if isinstance(config, dict):
            config = QQConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: QQConfig | QQInstanceConfig = config
        self._client: "botpy.Client | None" = None
        self._http: aiohttp.ClientSession | None = None
        self._processed_ids: deque[str] = deque(maxlen=1000)
        self._msg_seq: int = 1  # 消息序列号，避免被 QQ API 去重
        self._chat_type_cache: dict[str, str] = {}
        self._workspace = Path(workspace).expanduser() if workspace is not None else None
        self._media_root = self._init_media_root()

    @staticmethod
    def _is_remote_media(path: str) -> bool:
        """Return True when the outbound media reference is a remote URL."""
        return path.startswith(("http://", "https://"))

    @staticmethod
    def _failed_media_notice(path: str, reason: str | None = None) -> str:
        """Render a user-visible fallback notice for unsent QQ media."""
        name = Path(path).name or path
        return f"[Failed to send: {name}{f' - {reason}' if reason else ''}]"

    def _workspace_root(self) -> Path:
        """Return the active workspace root used by QQ publishing."""
        return (self._workspace or Path.cwd()).resolve(strict=False)

    def _resolve_local_media(
        self,
        media_path: str,
    ) -> tuple[Path | None, int | None, str | None]:
        """Resolve a local delivery artifact and infer the QQ rich-media file type."""
        source = Path(media_path).expanduser()
        try:
            resolved = source.resolve(strict=True)
        except FileNotFoundError:
            return None, None, "local file not found"
        except OSError as e:
            logger.warning("Failed to resolve local QQ media path {}: {}", media_path, e)
            return None, None, "local file unavailable"

        if not resolved.is_file():
            return None, None, "local file not found"

        artifacts_root = delivery_artifacts_root(self._workspace_root())
        try:
            resolved.relative_to(artifacts_root)
        except ValueError:
            return None, None, f"local delivery media must stay under {artifacts_root}"

        suffix = resolved.suffix.lower()
        if is_image_file(resolved):
            return resolved, 1, None
        if suffix == ".mp4":
            return resolved, 2, None
        if suffix == ".silk":
            return resolved, 3, None
        return None, None, "local delivery media must be an image, .mp4 video, or .silk voice"

    @staticmethod
    def _remote_media_file_type(media_url: str) -> int | None:
        """Infer a QQ rich-media file type from a remote URL."""
        path = urlparse(media_url).path.lower()
        if path.endswith(".mp4"):
            return 2
        if path.endswith(".silk"):
            return 3
        image_exts = (".jpg", ".jpeg", ".png", ".gif", ".webp")
        if path.endswith(image_exts):
            return 1
        return None

    def _next_msg_seq(self) -> int:
        """Return the next QQ message sequence number."""
        self._msg_seq += 1
        return self._msg_seq

    @staticmethod
    def _encode_file_data(path: Path) -> str:
        """Encode a local media file as base64 for QQ rich-media upload."""
        return base64.b64encode(path.read_bytes()).decode("ascii")

    async def _post_text_message(
        self,
        chat_id: str,
        msg_type: str,
        content: str,
        msg_id: str | None,
    ) -> None:
        """Send a plain-text or markdown QQ message."""
        use_markdown = self.config.msg_format == "markdown"
        payload: dict[str, Any] = {
            "msg_type": 2 if use_markdown else 0,
            "msg_id": msg_id,
            "msg_seq": self._next_msg_seq(),
        }
        if use_markdown:
            payload["markdown"] = {"content": content}
        else:
            payload["content"] = content
        if msg_type == "group":
            await self._client.api.post_group_message(group_openid=chat_id, **payload)
        else:
            await self._client.api.post_c2c_message(openid=chat_id, **payload)

    async def _post_remote_media_message(
        self,
        chat_id: str,
        msg_type: str,
        file_type: int,
        media_url: str,
        content: str | None,
        msg_id: str | None,
    ) -> None:
        """Send one QQ remote rich-media URL as a rich-media message."""
        if msg_type == "group":
            media = await self._client.api.post_group_file(
                group_openid=chat_id,
                file_type=file_type,
                url=media_url,
                srv_send_msg=False,
            )
            await self._client.api.post_group_message(
                group_openid=chat_id,
                msg_type=7,
                content=content,
                media=media,
                msg_id=msg_id,
                msg_seq=self._next_msg_seq(),
            )
        else:
            media = await self._client.api.post_c2c_file(
                openid=chat_id,
                file_type=file_type,
                url=media_url,
                srv_send_msg=False,
            )
            await self._client.api.post_c2c_message(
                openid=chat_id,
                msg_type=7,
                content=content,
                media=media,
                msg_id=msg_id,
                msg_seq=self._next_msg_seq(),
            )

    async def _post_local_media_message(
        self,
        chat_id: str,
        msg_type: str,
        file_type: int,
        local_path: Path,
        content: str | None,
        msg_id: str | None,
    ) -> None:
        """Upload a local QQ rich-media file using file_data."""
        if not self._client or Route is None:
            raise RuntimeError("QQ client not initialized")

        payload = {
            "file_type": file_type,
            "file_data": self._encode_file_data(local_path),
            "srv_send_msg": False,
        }
        if msg_type == "group":
            route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=chat_id)
            media = await self._client.api._http.request(route, json=payload)
            await self._client.api.post_group_message(
                group_openid=chat_id,
                msg_type=7,
                content=content,
                media=media,
                msg_id=msg_id,
                msg_seq=self._next_msg_seq(),
            )
        else:
            route = Route("POST", "/v2/users/{openid}/files", openid=chat_id)
            media = await self._client.api._http.request(route, json=payload)
            await self._client.api.post_c2c_message(
                openid=chat_id,
                msg_type=7,
                content=content,
                media=media,
                msg_id=msg_id,
                msg_seq=self._next_msg_seq(),
            )

    def _init_media_root(self) -> Path:
        """Choose a directory for saving inbound attachments."""
        if self.config.media_dir:
            root = Path(self.config.media_dir).expanduser()
        elif get_media_dir:
            try:
                root = Path(get_media_dir("qq"))
            except Exception:
                root = Path.home() / ".nanobot" / "media" / "qq"
        else:
            root = Path.home() / ".nanobot" / "media" / "qq"

        root.mkdir(parents=True, exist_ok=True)
        logger.info("QQ media directory: {}", str(root))
        return root

    async def start(self) -> None:
        """Start the QQ bot with auto-reconnect."""
        if not QQ_AVAILABLE:
            logger.error("QQ SDK not installed. Run: pip install qq-botpy")
            return

        if not self.config.app_id or not self.config.secret:
            logger.error("QQ app_id and secret not configured")
            return

        self._running = True
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        self._client = _make_bot_class(self)()
        logger.info("QQ bot started (C2C & Group supported)")
        await self._run_bot()

    async def _run_bot(self) -> None:
        """Run the bot connection with auto-reconnect."""
        while self._running:
            try:
                await self._client.start(appid=self.config.app_id, secret=self.config.secret)
            except Exception as e:
                logger.warning("QQ bot error: {}", e)
            if self._running:
                logger.info("Reconnecting QQ bot in 5 seconds...")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop bot and cleanup resources."""
        self._running = False
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
        self._client = None
        if self._http:
            try:
                await self._http.close()
            except Exception:
                pass
        self._http = None
        logger.info("QQ bot stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through QQ."""
        if not self._client:
            logger.warning("QQ client not initialized")
            return

        try:
            msg_id = msg.metadata.get("message_id")
            msg_type = self._chat_type_cache.get(msg.chat_id, "c2c")
            content_sent = False
            fallback_lines: list[str] = []

            for media_path in msg.media or []:
                local_media_path: Path | None = None
                local_file_type: int | None = None
                if not self._is_remote_media(media_path):
                    local_media_path, local_file_type, publish_error = self._resolve_local_media(
                        media_path
                    )
                    if local_media_path is None:
                        logger.warning(
                            "QQ outbound local media could not be uploaded directly: {} ({})",
                            media_path,
                            publish_error,
                        )
                        fallback_lines.append(
                            self._failed_media_notice(media_path, publish_error)
                        )
                        continue
                else:
                    ok, error = validate_url_target(media_path)
                    if not ok:
                        logger.warning("QQ outbound media blocked by URL validation: {}", error)
                        fallback_lines.append(self._failed_media_notice(media_path, error))
                        continue
                    remote_file_type = self._remote_media_file_type(media_path)
                    if remote_file_type is None:
                        fallback_lines.append(
                            self._failed_media_notice(
                                media_path,
                                "remote QQ media must be an image URL, .mp4 video, or .silk voice",
                            )
                        )
                        continue

                try:
                    if local_media_path is not None:
                        await self._post_local_media_message(
                            msg.chat_id,
                            msg_type,
                            local_file_type or 1,
                            local_media_path.resolve(strict=True),
                            msg.content if msg.content and not content_sent else None,
                            msg_id,
                        )
                    else:
                        await self._post_remote_media_message(
                            msg.chat_id,
                            msg_type,
                            remote_file_type,
                            media_path,
                            msg.content if msg.content and not content_sent else None,
                            msg_id,
                        )
                    if msg.content and not content_sent:
                        content_sent = True
                except Exception as media_error:
                    logger.error("Error sending QQ media {}: {}", media_path, media_error)
                    if local_media_path is not None:
                        fallback_lines.append(
                            self._failed_media_notice(
                                media_path, "QQ local file_data upload failed"
                            )
                        )
                    else:
                        fallback_lines.append(self._failed_media_notice(media_path))

            text_parts: list[str] = []
            if msg.content and not content_sent:
                text_parts.append(msg.content)
            if fallback_lines:
                text_parts.extend(fallback_lines)

            if text_parts:
                await self._post_text_message(msg.chat_id, msg_type, "\n".join(text_parts), msg_id)
        except Exception as e:
            logger.error("Error sending QQ message: {}", e)

    async def _on_message(self, data: "C2CMessage | GroupMessage", is_group: bool = False) -> None:
        """Handle incoming message from QQ."""
        try:
            if data.id in self._processed_ids:
                return
            self._processed_ids.append(data.id)

            if is_group:
                chat_id = data.group_openid
                user_id = data.author.member_openid
                self._chat_type_cache[chat_id] = "group"
            else:
                chat_id = str(
                    getattr(data.author, "id", None)
                    or getattr(data.author, "user_openid", "unknown")
                )
                user_id = chat_id
                self._chat_type_cache[chat_id] = "c2c"

            content = (data.content or "").strip()
            attachments = getattr(data, "attachments", None) or []
            media_paths, recv_lines, att_meta = await self._handle_attachments(attachments)
            if recv_lines:
                tag = "[Image]" if any(_is_image_name(Path(p).name) for p in media_paths) else "[File]"
                file_block = "Received files:\n" + "\n".join(recv_lines)
                content = f"{content}\n\n{file_block}".strip() if content else f"{tag}\n{file_block}"

            if not content and not media_paths:
                return

            await self._handle_message(
                sender_id=user_id,
                chat_id=chat_id,
                content=content,
                media=media_paths or None,
                metadata={
                    "message_id": data.id,
                    "attachments": att_meta,
                },
            )
        except Exception:
            logger.exception("Error handling QQ message")

    async def _handle_attachments(self, attachments: list[Any]) -> tuple[list[str], list[str], list[dict[str, Any]]]:
        """Extract, download, and format QQ attachments for downstream tools."""
        media_paths: list[str] = []
        recv_lines: list[str] = []
        att_meta: list[dict[str, Any]] = []
        if not attachments:
            return media_paths, recv_lines, att_meta

        for att in attachments:
            url = getattr(att, "url", None)
            filename = getattr(att, "filename", None)
            content_type = getattr(att, "content_type", None)
            local_path = (
                await self._download_to_media_dir_chunked(url, filename_hint=filename or "")
                if url
                else None
            )
            att_meta.append(
                {
                    "url": url,
                    "filename": filename,
                    "content_type": content_type,
                    "saved_path": local_path,
                }
            )
            shown_name = filename or url or "file"
            if local_path:
                media_paths.append(local_path)
                recv_lines.append(f"- {shown_name}\n  saved: {local_path}")
            else:
                recv_lines.append(f"- {shown_name}\n  saved: [download failed]")

        return media_paths, recv_lines, att_meta

    async def _download_to_media_dir_chunked(self, url: str, filename_hint: str = "") -> str | None:
        """Download an inbound attachment using chunked streaming writes."""
        if not self._http:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))

        safe = _sanitize_filename(filename_hint)
        timestamp_ms = int(time.time() * 1000)
        tmp_path: Path | None = None

        try:
            async with self._http.get(
                url,
                timeout=aiohttp.ClientTimeout(total=120),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    logger.warning("QQ download failed: status={} url={}", resp.status, url)
                    return None

                content_type = (resp.headers.get("Content-Type") or "").lower()
                ext = Path(urlparse(url).path).suffix or Path(filename_hint).suffix
                if not ext:
                    if "png" in content_type:
                        ext = ".png"
                    elif "jpeg" in content_type or "jpg" in content_type:
                        ext = ".jpg"
                    elif "gif" in content_type:
                        ext = ".gif"
                    elif "webp" in content_type:
                        ext = ".webp"
                    elif "pdf" in content_type:
                        ext = ".pdf"
                    else:
                        ext = ".bin"

                if safe and not Path(safe).suffix:
                    safe = safe + ext
                filename = safe or f"qq_file_{timestamp_ms}{ext}"
                target = self._media_root / filename
                if target.exists():
                    target = self._media_root / f"{target.stem}_{timestamp_ms}{target.suffix}"
                tmp_path = target.with_suffix(target.suffix + ".part")

                chunk_size = max(1024, int(self.config.download_chunk_size or 262144))
                max_bytes = max(
                    1024 * 1024,
                    int(self.config.download_max_bytes or (200 * 1024 * 1024)),
                )
                downloaded = 0

                def _open_tmp() -> Any:
                    tmp_path.parent.mkdir(parents=True, exist_ok=True)
                    return open(tmp_path, "wb")  # noqa: SIM115

                f = await asyncio.to_thread(_open_tmp)
                try:
                    async for chunk in resp.content.iter_chunked(chunk_size):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            logger.warning(
                                "QQ download exceeded max_bytes={} url={} -> abort",
                                max_bytes,
                                url,
                            )
                            return None
                        await asyncio.to_thread(f.write, chunk)
                finally:
                    await asyncio.to_thread(f.close)

                await asyncio.to_thread(os.replace, tmp_path, target)
                tmp_path = None
                logger.info("QQ file saved: {}", str(target))
                return str(target)
        except Exception as e:
            logger.error("QQ download error: {}", e)
            return None
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
