"""QQ channel implementation using botpy SDK."""

import asyncio
import os
import secrets
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urljoin

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import QQConfig, QQInstanceConfig
from nanobot.security.network import validate_url_target
from nanobot.utils.helpers import detect_image_mime, ensure_dir

try:
    import botpy
    from botpy.message import C2CMessage, GroupMessage

    QQ_AVAILABLE = True
except ImportError:
    QQ_AVAILABLE = False
    botpy = None
    C2CMessage = None
    GroupMessage = None

if TYPE_CHECKING:
    from botpy.message import C2CMessage, GroupMessage


def _make_bot_class(channel: "QQChannel") -> "type[botpy.Client]":
    """Create a botpy Client subclass bound to the given channel."""
    intents = botpy.Intents(public_messages=True, direct_message=True)

    class _Bot(botpy.Client):
        def __init__(self):
            # Disable botpy's file log — nanobot uses loguru; default "botpy.log" fails on read-only fs
            super().__init__(intents=intents, ext_handlers=False)

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
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

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

    def _public_root(self) -> Path:
        """Return the fixed public tree served by the gateway HTTP route."""
        return ensure_dir(self._workspace_root() / "public")

    def _out_root(self) -> Path:
        """Return the default workspace out directory used for generated artifacts."""
        return self._workspace_root() / "out"

    def _resolve_media_public_dir(self) -> tuple[Path | None, str | None]:
        """Resolve the local publish directory for QQ media under workspace/public."""
        configured = Path(self.config.media_public_dir).expanduser()
        if configured.is_absolute():
            resolved = configured.resolve(strict=False)
        else:
            resolved = (self._workspace_root() / configured).resolve(strict=False)
        public_root = self._public_root()
        try:
            resolved.relative_to(public_root)
        except ValueError:
            return None, f"QQ mediaPublicDir must stay under {public_root}"
        return ensure_dir(resolved), None

    @staticmethod
    def _guess_image_suffix(path: Path, mime_type: str | None) -> str:
        """Pick a reasonable output suffix for published QQ images."""
        if path.suffix:
            return path.suffix.lower()
        return {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }.get(mime_type or "", ".bin")

    @staticmethod
    def _is_image_file(path: Path) -> bool:
        """Validate that a local file looks like an image supported by QQ rich media."""
        try:
            with path.open("rb") as f:
                header = f.read(16)
        except OSError:
            return False
        return detect_image_mime(header) is not None

    @staticmethod
    def _detect_image_mime(path: Path) -> str | None:
        """Detect image mime type from the leading bytes of a file."""
        try:
            with path.open("rb") as f:
                return detect_image_mime(f.read(16))
        except OSError:
            return None

    async def _delete_published_media_later(self, path: Path, delay_seconds: int) -> None:
        """Delete an auto-published QQ media file after a grace period."""
        try:
            await asyncio.sleep(delay_seconds)
            path.unlink(missing_ok=True)
        except Exception as e:
            logger.debug("Failed to delete published QQ media {}: {}", path, e)

    def _schedule_media_cleanup(self, path: Path) -> None:
        """Best-effort cleanup for auto-published local QQ media."""
        if self.config.media_ttl_seconds <= 0:
            return
        task = asyncio.create_task(
            self._delete_published_media_later(path, self.config.media_ttl_seconds)
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    def _try_link_out_media_into_public(
        self,
        source: Path,
        public_dir: Path,
    ) -> tuple[Path | None, str | None]:
        """Hard-link a generated workspace/out media file into public/qq."""
        out_root = self._out_root().resolve(strict=False)
        try:
            source.relative_to(out_root)
        except ValueError:
            return None, f"QQ local media must stay under {public_dir} or {out_root}"

        if not self._is_image_file(source):
            return None, "QQ local media must be an image"

        mime_type = self._detect_image_mime(source)
        suffix = self._guess_image_suffix(source, mime_type)
        published = public_dir / f"{source.stem}-{secrets.token_urlsafe(6)}{suffix}"
        try:
            os.link(source, published)
        except OSError as e:
            logger.warning("Failed to hard-link QQ media {} -> {}: {}", source, published, e)
            return None, "failed to publish local file"
        self._schedule_media_cleanup(published)
        return published, None

    async def _publish_local_media(self, media_path: str) -> tuple[str | None, str | None]:
        """Map a local public QQ media file, or a generated out file, to its served URL."""
        if not self.config.media_base_url:
            return None, "QQ local media publishing is not configured"

        source = Path(media_path).expanduser()
        try:
            resolved = source.resolve(strict=True)
        except FileNotFoundError:
            return None, "local file not found"
        except OSError as e:
            logger.warning("Failed to resolve QQ media path {}: {}", media_path, e)
            return None, "local file unavailable"

        if not resolved.is_file():
            return None, "local file not found"

        public_dir, dir_error = self._resolve_media_public_dir()
        if public_dir is None:
            return None, dir_error

        try:
            relative_path = resolved.relative_to(public_dir)
        except ValueError:
            published, publish_error = self._try_link_out_media_into_public(resolved, public_dir)
            if published is None:
                return None, publish_error
            relative_path = published.relative_to(public_dir)

        media_url = urljoin(
            f"{self.config.media_base_url.rstrip('/')}/",
            quote(relative_path.as_posix(), safe="/"),
        )
        return media_url, None

    def _next_msg_seq(self) -> int:
        """Return the next QQ message sequence number."""
        self._msg_seq += 1
        return self._msg_seq

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
        media_url: str,
        content: str | None,
        msg_id: str | None,
    ) -> None:
        """Send one QQ remote image URL as a rich-media message."""
        if msg_type == "group":
            media = await self._client.api.post_group_file(
                group_openid=chat_id,
                file_type=1,
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
                file_type=1,
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
        for task in list(self._cleanup_tasks):
            task.cancel()
        self._cleanup_tasks.clear()
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
                resolved_media = media_path
                if not self._is_remote_media(media_path):
                    resolved_media, publish_error = await self._publish_local_media(media_path)
                    if not resolved_media:
                        logger.warning(
                            "QQ outbound local media could not be published: {} ({})",
                            media_path,
                            publish_error,
                        )
                        fallback_lines.append(
                            self._failed_media_notice(media_path, publish_error)
                        )
                        continue

                ok, error = validate_url_target(resolved_media)
                if not ok:
                    logger.warning("QQ outbound media blocked by URL validation: {}", error)
                    fallback_lines.append(self._failed_media_notice(media_path, error))
                    continue

                try:
                    await self._post_remote_media_message(
                        msg.chat_id,
                        msg_type,
                        resolved_media,
                        msg.content if msg.content and not content_sent else None,
                        msg_id,
                    )
                    if msg.content and not content_sent:
                        content_sent = True
                except Exception as media_error:
                    logger.error("Error sending QQ media {}: {}", resolved_media, media_error)
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
