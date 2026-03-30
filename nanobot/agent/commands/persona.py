"""Persona command helpers for AgentLoop."""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from nanobot.agent.i18n import text
from nanobot.bus.events import InboundMessage, OutboundMessage

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop
    from nanobot.session.manager import Session


class PersonaCommandHandler:
    """Encapsulates `/persona` subcommand behavior for AgentLoop."""

    def __init__(self, loop: AgentLoop) -> None:
        self.loop = loop

    @staticmethod
    def _response(msg: InboundMessage, content: str) -> OutboundMessage:
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)

    def current(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        language = self.loop._get_session_language(session)
        current = self.loop._get_session_persona(session)
        return self._response(msg, text(language, "current_persona", persona=current))

    def list(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        language = self.loop._get_session_language(session)
        current = self.loop._get_session_persona(session)
        marker = text(language, "current_marker")
        personas = [
            f"{name} ({marker})" if name == current else name
            for name in self.loop.context.list_personas()
        ]
        return self._response(
            msg,
            text(language, "available_personas", items="\n".join(f"- {name}" for name in personas)),
        )

    async def set(self, msg: InboundMessage, session: Session, target_raw: str) -> OutboundMessage:
        language = self.loop._get_session_language(session)
        target = self.loop.context.find_persona(target_raw)
        if target is None:
            personas = ", ".join(self.loop.context.list_personas())
            return self._response(
                msg,
                text(
                    language,
                    "unknown_persona",
                    name=target_raw,
                    personas=personas,
                    path=self.loop.workspace / "personas" / target_raw,
                ),
            )

        current = self.loop._get_session_persona(session)
        if target == current:
            return self._response(msg, text(language, "persona_already_active", persona=target))

        try:
            if not await self.loop.memory_consolidator.archive_unconsolidated(session):
                return self._response(msg, text(language, "memory_archival_failed_persona"))
            await self.loop._flush_memory_session(
                session,
                channel=msg.channel,
                chat_id=msg.chat_id,
                sender_id=msg.sender_id,
                persona=current,
                language=language,
            )
        except Exception:
            logger.exception("/persona archival failed for {}", session.key)
            return self._response(msg, text(language, "memory_archival_failed_persona"))

        session.clear()
        self.loop._set_session_persona(session, target)
        self.loop.sessions.save(session)
        self.loop.sessions.invalidate(session.key)
        return self._response(msg, text(language, "switched_persona", persona=target))
