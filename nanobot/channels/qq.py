"""QQ channel implementation using botpy SDK."""

import asyncio
import base64
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import QQConfig, QQInstanceConfig
from nanobot.security.network import validate_url_target
from nanobot.utils.delivery import delivery_artifacts_root, is_image_file

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
        config: QQConfig | QQInstanceConfig,
        bus: MessageBus,
        workspace: str | Path | None = None,
    ):
        super().__init__(config, bus)
        self.config: QQConfig | QQInstanceConfig = config
        self._client: "botpy.Client | None" = None
        self._processed_ids: deque = deque(maxlen=1000)
        self._msg_seq: int = 1  # 消息序列号，避免被 QQ API 去重
        self._chat_type_cache: dict[str, str] = {}
        self._workspace = Path(workspace).expanduser() if workspace is not None else None

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

    async def _post_text_message(self, chat_id: str, msg_type: str, content: str, msg_id: str | None) -> None:
        """Send a plain-text QQ message."""
        payload = {
            "msg_type": 0,
            "content": content,
            "msg_id": msg_id,
            "msg_seq": self._next_msg_seq(),
        }
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

    async def start(self) -> None:
        """Start the QQ bot."""
        if not QQ_AVAILABLE:
            logger.error("QQ SDK not installed. Run: pip install qq-botpy")
            return

        if not self.config.app_id or not self.config.secret:
            logger.error("QQ app_id and secret not configured")
            return

        self._running = True
        bot_class = _make_bot_class(self)
        self._client = bot_class()
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
        """Stop the QQ bot."""
        self._running = False
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
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

            for media_path in msg.media:
                local_media_path: Path | None = None
                local_file_type: int | None = None
                if not self._is_remote_media(media_path):
                    local_media_path, local_file_type, publish_error = self._resolve_local_media(media_path)
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
                            self._failed_media_notice(media_path, "QQ local file_data upload failed")
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
            # Dedup by message ID
            if data.id in self._processed_ids:
                return
            self._processed_ids.append(data.id)

            content = (data.content or "").strip()
            if not content:
                return

            if is_group:
                chat_id = data.group_openid
                user_id = data.author.member_openid
                self._chat_type_cache[chat_id] = "group"
            else:
                chat_id = str(getattr(data.author, 'id', None) or getattr(data.author, 'user_openid', 'unknown'))
                user_id = chat_id
                self._chat_type_cache[chat_id] = "c2c"

            await self._handle_message(
                sender_id=user_id,
                chat_id=chat_id,
                content=content,
                metadata={"message_id": data.id},
            )
        except Exception:
            logger.exception("Error handling QQ message")
