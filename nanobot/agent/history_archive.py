"""Structured archive store for lossless chat-history recall."""

from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any
from uuid import uuid4

from nanobot.agent.personas import DEFAULT_PERSONA, persona_workspace, resolve_persona_name
from nanobot.utils.helpers import ensure_dir, safe_filename

_BACKTICK_RE = re.compile(r"`([^`\n]{1,80})`")
_SLASH_CMD_RE = re.compile(r"(?<!\w)(/[A-Za-z][\w-]{0,31})")
_PATH_RE = re.compile(r"\b(?:[\w.-]+/)+[\w.-]+\b|\b[\w.-]+\.[A-Za-z0-9]{1,8}\b")
_WORD_RE = re.compile(r"[A-Za-z0-9_./:-]{3,64}")
_TIMESTAMP_PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*")
_STOPWORDS = {
    "about",
    "after",
    "agent",
    "assistant",
    "before",
    "between",
    "code",
    "current",
    "details",
    "history",
    "message",
    "messages",
    "please",
    "project",
    "query",
    "reply",
    "session",
    "summary",
    "system",
    "their",
    "there",
    "these",
    "they",
    "this",
    "tool",
    "tools",
    "turn",
    "user",
    "what",
    "when",
    "with",
    "would",
}


def content_to_text(value: Any) -> str:
    """Flatten stored multimodal content into plain text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif block is not None:
                parts.append(json.dumps(block, ensure_ascii=False))
        return "\n".join(part for part in parts if part)
    return json.dumps(value, ensure_ascii=False)


def parse_datetime(raw: str | None, *, end_of_day: bool = False) -> datetime | None:
    """Parse YYYY-MM-DD or ISO-ish strings into UTC datetimes."""
    if not isinstance(raw, str):
        return None

    text = raw.strip()
    if not text:
        return None

    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None

    if parsed is None:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        return None

    if parsed.tzinfo is None:
        if len(text) == 10:
            parsed = datetime.combine(
                parsed.date(),
                time.max if end_of_day else time.min,
            )
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _entry_datetime(raw: str | None) -> datetime:
    return parse_datetime(raw, end_of_day=False) or datetime.min.replace(tzinfo=UTC)


def tokenize_query(query: str) -> list[str]:
    """Normalize a search query into stable tokens."""
    lowered = query.lower().strip()
    if not lowered:
        return []
    seen: set[str] = set()
    tokens: list[str] = []
    for token in re.split(r"[^a-z0-9_./:-]+", lowered):
        cleaned = token.strip()
        if len(cleaned) < 2 or cleaned in seen:
            continue
        seen.add(cleaned)
        tokens.append(cleaned)
    return tokens


class HistoryArchiveStore:
    """Manage persona-scoped archived history chunks and their index."""

    INDEX_FILENAME = "index.jsonl"
    CHUNKS_DIRNAME = "chunks"

    def __init__(self, workspace: Path, persona: str | None = None) -> None:
        resolved = resolve_persona_name(workspace, persona) or DEFAULT_PERSONA
        self._workspace = workspace
        self._persona = resolved
        self._persona_workspace = persona_workspace(workspace, resolved)
        self._archive_dir = self._persona_workspace / "memory" / "archive"
        self._chunks_dir = self._archive_dir / self.CHUNKS_DIRNAME
        self._index_path = self._archive_dir / self.INDEX_FILENAME

    def update_workspace(self, workspace: Path) -> None:
        """Rebind the root workspace while keeping the current persona selection."""
        self.__init__(workspace, self._persona)

    def set_persona(self, persona: str | None) -> None:
        """Switch the active persona archive scope."""
        self.__init__(self._workspace, persona)

    def write_archive(
        self,
        *,
        session_key: str,
        messages: list[dict[str, Any]],
        history_entry: str,
        source: str,
        raw_archive: bool = False,
    ) -> str | None:
        """Persist one archived chunk plus an index record."""
        if not messages:
            return None

        ensure_dir(self._chunks_dir)
        archived_at = datetime.now().astimezone().isoformat()
        time_start, time_end = self._time_bounds(messages, fallback=archived_at)
        archive_id = self._build_archive_id(time_end, session_key)
        summary = history_entry.strip() or f"[{time_end[:16]}] Archived {len(messages)} messages."
        normalized_messages = copy.deepcopy(messages)
        tools = self._extract_tools(normalized_messages)
        keywords = self._extract_keywords(normalized_messages, summary, tools)
        title = self._build_title(summary, normalized_messages)

        record = {
            "id": archive_id,
            "version": 1,
            "persona": self._persona,
            "sessionKey": session_key,
            "source": source,
            "rawArchive": raw_archive,
            "archivedAt": archived_at,
            "messages": normalized_messages,
        }
        chunk_name = f"{archive_id}.json"
        chunk_path = self._chunks_dir / chunk_name
        chunk_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

        entry = {
            "id": archive_id,
            "version": 1,
            "persona": self._persona,
            "sessionKey": session_key,
            "source": source,
            "rawArchive": raw_archive,
            "timeStart": time_start,
            "timeEnd": time_end,
            "messageCount": len(normalized_messages),
            "title": title,
            "summary": summary,
            "keywords": keywords,
            "tools": tools,
            "chunkPath": f"{self.CHUNKS_DIRNAME}/{chunk_name}",
        }
        with open(self._index_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return archive_id

    def search(
        self,
        *,
        query: str,
        limit: int = 5,
        session_key: str | None = None,
        preferred_session_key: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return archive index hits ranked by simple lexical relevance."""
        entries = self._load_index_entries()
        if not entries:
            return []

        query_text = query.strip().lower()
        query_tokens = tokenize_query(query)
        since_dt = parse_datetime(since, end_of_day=False)
        until_dt = parse_datetime(until, end_of_day=True)
        ranked: list[tuple[int, datetime, dict[str, Any]]] = []

        for entry in entries:
            if session_key and entry.get("sessionKey") != session_key:
                continue

            entry_dt = _entry_datetime(entry.get("timeEnd"))
            if since_dt and entry_dt < since_dt:
                continue
            if until_dt and entry_dt > until_dt:
                continue

            score = self._score_entry(
                entry,
                query_text=query_text,
                query_tokens=query_tokens,
                preferred_session_key=preferred_session_key,
            )
            if score <= 0:
                continue
            ranked.append((score, entry_dt, entry))

        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [entry for _, _, entry in ranked[: max(1, min(limit, 20))]]

    def load_entry(self, archive_id: str) -> dict[str, Any] | None:
        """Load a single archive chunk by id."""
        archive_id = archive_id.strip()
        if not archive_id:
            return None
        for entry in reversed(self._load_index_entries()):
            if entry.get("id") != archive_id:
                continue
            chunk_rel = entry.get("chunkPath")
            if not isinstance(chunk_rel, str) or not chunk_rel:
                return None
            chunk_path = (self._archive_dir / chunk_rel).resolve(strict=False)
            if not chunk_path.exists():
                return {
                    "error": f"Archived chunk file is missing: {chunk_path}",
                    "entry": entry,
                }
            try:
                payload = json.loads(chunk_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                return {
                    "error": f"Failed to read archived chunk {chunk_path}: {exc}",
                    "entry": entry,
                }
            payload["entry"] = entry
            return payload
        return None

    def _load_index_entries(self) -> list[dict[str, Any]]:
        if not self._index_path.exists():
            return []

        entries: list[dict[str, Any]] = []
        with open(self._index_path, encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    data = json.loads(text)
                except ValueError:
                    continue
                if isinstance(data, dict):
                    entries.append(data)
        return entries

    @staticmethod
    def _build_archive_id(timestamp: str, session_key: str) -> str:
        dt = parse_datetime(timestamp, end_of_day=False) or datetime.now().astimezone()
        prefix = dt.strftime("%Y%m%dT%H%M%S")
        safe_session = safe_filename(session_key.replace(":", "_"))[:40].strip("_") or "session"
        return f"{prefix}_{safe_session}_{uuid4().hex[:6]}"

    @staticmethod
    def _time_bounds(messages: list[dict[str, Any]], *, fallback: str) -> tuple[str, str]:
        stamps = [
            stamp
            for message in messages
            if isinstance((stamp := message.get("timestamp")), str) and stamp.strip()
        ]
        if not stamps:
            return fallback, fallback
        return stamps[0], stamps[-1]

    @staticmethod
    def _build_title(summary: str, messages: list[dict[str, Any]]) -> str:
        cleaned = _TIMESTAMP_PREFIX_RE.sub("", summary.strip())
        cleaned = re.split(r"[\n。！？!?;；]", cleaned, 1)[0].strip()
        if cleaned:
            return cleaned[:120]

        for message in messages:
            content = content_to_text(message.get("content")).strip()
            if content:
                return content.replace("\n", " ")[:120]
        return "Archived conversation chunk"

    @staticmethod
    def _extract_tools(messages: list[dict[str, Any]]) -> list[str]:
        seen: set[str] = set()
        tools: list[str] = []

        def _remember(name: Any) -> None:
            if not isinstance(name, str):
                return
            cleaned = name.strip()
            if not cleaned or cleaned in seen:
                return
            seen.add(cleaned)
            tools.append(cleaned)

        for message in messages:
            for raw in message.get("tools_used") or []:
                _remember(raw)
            if message.get("role") == "tool":
                _remember(message.get("name"))
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                _remember(call.get("name"))
                function = call.get("function")
                if isinstance(function, dict):
                    _remember(function.get("name"))
        return tools[:12]

    @classmethod
    def _extract_keywords(
        cls,
        messages: list[dict[str, Any]],
        summary: str,
        tools: list[str],
    ) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()

        def _remember(value: str) -> None:
            cleaned = value.strip().strip(".,:;()[]{}")
            if len(cleaned) < 3:
                return
            lowered = cleaned.lower()
            if lowered in _STOPWORDS or lowered in seen:
                return
            seen.add(lowered)
            ordered.append(cleaned)

        for tool in tools:
            _remember(tool)

        for text in [summary, *(content_to_text(msg.get("content")) for msg in messages)]:
            if not text:
                continue
            for pattern in (_BACKTICK_RE, _SLASH_CMD_RE, _PATH_RE):
                for match in pattern.findall(text):
                    _remember(match if isinstance(match, str) else match[0])
            for token in _WORD_RE.findall(text):
                if "/" in token or "." in token or "_" in token or token.startswith("/"):
                    _remember(token)

        return ordered[:16]

    @staticmethod
    def _score_entry(
        entry: dict[str, Any],
        *,
        query_text: str,
        query_tokens: list[str],
        preferred_session_key: str | None,
    ) -> int:
        title = str(entry.get("title", "")).lower()
        summary = str(entry.get("summary", "")).lower()
        session = str(entry.get("sessionKey", "")).lower()
        keywords = [str(item).lower() for item in entry.get("keywords") or []]
        tools = [str(item).lower() for item in entry.get("tools") or []]
        score = 0

        if preferred_session_key and session == preferred_session_key.lower():
            score += 3
        if query_text and query_text in title:
            score += 10
        if query_text and query_text in summary:
            score += 6

        for token in query_tokens:
            if token in title:
                score += 8
            if token in summary:
                score += 3
            if any(token == keyword or token in keyword for keyword in keywords):
                score += 6
            if any(token == tool or token in tool for tool in tools):
                score += 5
            if token in session:
                score += 2
        return score
