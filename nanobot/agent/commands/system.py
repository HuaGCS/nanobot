"""Lightweight system command helpers for AgentLoop."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanobot import __version__
from nanobot.agent.i18n import help_lines, text
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.utils.helpers import build_status_content

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop
    from nanobot.session.manager import Session


class SystemCommandHandler:
    """Encapsulates lightweight `/new`, `/help`, and `/status` behavior for AgentLoop."""

    def __init__(self, loop: AgentLoop) -> None:
        self.loop = loop

    @staticmethod
    def _response(
        msg: InboundMessage,
        content: str,
        *,
        metadata: dict[str, str] | None = None,
    ) -> OutboundMessage:
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=content,
            metadata=metadata,
        )

    def help(self, msg: InboundMessage, language: str) -> OutboundMessage:
        return self._response(
            msg,
            "\n".join(help_lines(language)),
            metadata={"render_as": "text"},
        )

    def new_session(self, msg: InboundMessage, session: Session, language: str) -> OutboundMessage:
        snapshot = session.messages[session.last_consolidated:]
        session.clear()
        self.loop.sessions.save(session)
        self.loop.sessions.invalidate(session.key)

        if snapshot:
            self.loop._schedule_background(
                self.loop.memory_consolidator.archive_messages(
                    session,
                    snapshot,
                    source="new_session",
                )
            )

        return self._response(msg, text(language, "new_session_started"))

    def status(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        ctx_est = 0
        try:
            ctx_est, _ = self.loop.memory_consolidator.estimate_session_prompt_tokens(session)
        except Exception:
            pass
        if ctx_est <= 0:
            ctx_est = self.loop._last_usage.get("prompt_tokens", 0)
        return self._response(
            msg,
            build_status_content(
                version=__version__,
                model=self.loop.model,
                start_time=self.loop._start_time,
                last_usage=self.loop._last_usage,
                context_window_tokens=self.loop.context_window_tokens,
                session_msg_count=len(session.get_history(max_messages=0)),
                context_tokens_estimate=ctx_est,
            ),
            metadata={"render_as": "text"},
        )
