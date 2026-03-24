"""Language command helpers for AgentLoop."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanobot.agent.i18n import language_label, list_languages, normalize_language_code, text
from nanobot.bus.events import InboundMessage, OutboundMessage

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop
    from nanobot.session.manager import Session


class LanguageCommandHandler:
    """Encapsulates `/lang` subcommand behavior for AgentLoop."""

    def __init__(self, loop: AgentLoop) -> None:
        self.loop = loop

    @staticmethod
    def _response(msg: InboundMessage, content: str) -> OutboundMessage:
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)

    def current(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        current = self.loop._get_session_language(session)
        return self._response(
            msg,
            text(current, "current_language", language_name=language_label(current, current)),
        )

    def list(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        current = self.loop._get_session_language(session)
        items = "\n".join(
            f"- {language_label(code, current)}"
            + (f" ({text(current, 'current_marker')})" if code == current else "")
            for code in list_languages()
        )
        return self._response(msg, text(current, "available_languages", items=items))

    def set(self, msg: InboundMessage, session: Session, target_raw: str) -> OutboundMessage:
        current = self.loop._get_session_language(session)
        target = normalize_language_code(target_raw)
        if target is None:
            languages = ", ".join(language_label(code, current) for code in list_languages())
            return self._response(
                msg,
                text(current, "unknown_language", name=target_raw, languages=languages),
            )

        if target == current:
            return self._response(
                msg,
                text(current, "language_already_active", language_name=language_label(target, current)),
            )

        self.loop._set_session_language(session, target)
        self.loop.sessions.save(session)
        return self._response(
            msg,
            text(target, "switched_language", language_name=language_label(target, target)),
        )
