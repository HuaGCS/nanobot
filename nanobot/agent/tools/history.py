"""Tools for searching and expanding archived chat history."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.agent.history_archive import HistoryArchiveStore, content_to_text
from nanobot.agent.tools.base import Tool


class _HistoryTool(Tool):
    """Shared persona/session-bound archive tool behavior."""

    def __init__(self, workspace: Path):
        self._workspace = workspace
        self._channel = ""
        self._chat_id = ""
        self._persona: str | None = None

    def update_workspace(self, workspace: Path) -> None:
        self._workspace = workspace

    def set_context(self, channel: str, chat_id: str, persona: str | None = None) -> None:
        self._channel = channel
        self._chat_id = chat_id
        self._persona = persona

    def _store(self) -> HistoryArchiveStore:
        return HistoryArchiveStore(self._workspace, self._persona)

    def _current_session_key(self) -> str | None:
        if not self._channel or not self._chat_id:
            return None
        return f"{self._channel}:{self._chat_id}"


class HistorySearchTool(_HistoryTool):
    """Find archived history chunks for the active persona."""

    @property
    def name(self) -> str:
        return "history_search"

    @property
    def description(self) -> str:
        return (
            "Search archived chat-history chunks for the active persona. "
            "Returns archive ids, summaries, and time ranges. Use history_expand "
            "with a returned id when you need the transcript details."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look for in archived history.",
                    "minLength": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of archive hits to return (1-10).",
                    "minimum": 1,
                    "maximum": 10,
                },
                "sessionKey": {
                    "type": ["string", "null"],
                    "description": "Optional exact session key filter, e.g. 'cli:direct'.",
                },
                "since": {
                    "type": ["string", "null"],
                    "description": "Optional lower time bound, such as '2026-03-01' or ISO datetime.",
                },
                "until": {
                    "type": ["string", "null"],
                    "description": "Optional upper time bound, such as '2026-03-30' or ISO datetime.",
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        limit: int = 5,
        session_key: str | None = None,
        since: str | None = None,
        until: str | None = None,
        **kwargs: Any,
    ) -> str:
        if session_key is None:
            session_key = kwargs.get("sessionKey")
        results = self._store().search(
            query=query,
            limit=limit,
            session_key=session_key,
            preferred_session_key=None if session_key else self._current_session_key(),
            since=since,
            until=until,
        )
        if not results:
            return f'No archived history matches for "{query}".'

        lines = [f'Archived history matches for "{query}":\n']
        for idx, entry in enumerate(results, 1):
            lines.append(f'{idx}. ID: {entry.get("id", "")}')
            lines.append(
                "   Session: "
                f'{entry.get("sessionKey", "")} | '
                f'{entry.get("timeStart", "")} -> {entry.get("timeEnd", "")}'
            )
            summary = str(entry.get("summary", "")).strip()
            if summary:
                lines.append(f"   Summary: {summary}")
            keywords = [str(item) for item in entry.get("keywords") or [] if str(item).strip()]
            if keywords:
                lines.append(f"   Keywords: {', '.join(keywords[:8])}")
            lines.append(f'   Next: call history_expand with id="{entry.get("id", "")}"')
        return "\n".join(lines)


class HistoryExpandTool(_HistoryTool):
    """Return a transcript view for one archived history chunk."""

    @property
    def name(self) -> str:
        return "history_expand"

    @property
    def description(self) -> str:
        return (
            "Expand one archived history chunk by id and return a compact transcript "
            "for the active persona."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Archive id returned by history_search.",
                    "minLength": 1,
                },
                "maxMessages": {
                    "type": "integer",
                    "description": (
                        "Maximum number of transcript messages to show. When the chunk is longer, "
                        "the tool shows the beginning and end with an omission marker."
                    ),
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": ["id"],
        }

    async def execute(self, id: str, max_messages: int = 20, **kwargs: Any) -> str:
        if "maxMessages" in kwargs and "max_messages" not in kwargs:
            max_messages = kwargs["maxMessages"]
        record = self._store().load_entry(id)
        if record is None:
            return f'Error: Archived history id not found: "{id}"'
        if isinstance(record, dict) and record.get("error"):
            return f'Error: {record["error"]}'

        entry = record.get("entry") if isinstance(record, dict) else None
        messages = record.get("messages") if isinstance(record, dict) else None
        if not isinstance(entry, dict) or not isinstance(messages, list):
            return f'Error: Archived history payload is invalid for id "{id}"'

        selected, omitted = self._select_messages(messages, max_messages)
        lines = [f'Archived transcript for "{id}":']
        lines.append(
            f'Session: {entry.get("sessionKey", "")} | '
            f'{entry.get("timeStart", "")} -> {entry.get("timeEnd", "")}'
        )
        summary = str(entry.get("summary", "")).strip()
        if summary:
            lines.append(f"Summary: {summary}")
        lines.append("")

        if omitted > 0:
            lines.append(
                f"[showing {len(selected) - 1} of {len(messages)} messages; "
                f"{omitted} omitted in the middle]"
            )

        for message in selected:
            if message is None:
                lines.append(f"... [{omitted} messages omitted] ...")
                continue
            role = str(message.get("role", "?")).upper()
            timestamp = str(message.get("timestamp", ""))[:16]
            name = str(message.get("name", "")).strip()
            label = f"{role}({name})" if name and role == "TOOL" else role
            content = content_to_text(message.get("content")).strip() or "(no content)"
            lines.append(f"[{timestamp}] {label}: {content}")
            if message.get("tool_calls"):
                lines.append(f"   tool_calls: {message.get('tool_calls')}")
        return "\n".join(lines)

    @staticmethod
    def _select_messages(
        messages: list[dict[str, Any]],
        max_messages: int,
    ) -> tuple[list[dict[str, Any] | None], int]:
        if max_messages <= 0 or len(messages) <= max_messages:
            return list(messages), 0
        if max_messages < 4:
            selected = list(messages[-max_messages:])
            return selected, len(messages) - len(selected)

        head = max_messages // 2
        tail = max_messages - head
        omitted = max(0, len(messages) - head - tail)
        return [*messages[:head], None, *messages[-tail:]], omitted
