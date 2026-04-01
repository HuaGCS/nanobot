"""Star Office UI status tracking helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext

_DEFAULT_IDLE_DETAIL = "Ready"
_DEFAULT_ERROR_DETAIL = "Agent run failed"
_PUSH_MODE_GUEST = "guest"
_PUSH_MODE_MAIN = "main"
_PUSH_ROUTE_JOIN = "/join-agent"
_PUSH_ROUTE_UPDATE = "/agent-push"
_PUSH_ROUTE_MAIN_UPDATE = "/set_state"
_READ_ONLY_TOOL_NAMES = {
    "history_expand",
    "history_search",
    "list_dir",
    "read_file",
    "web_fetch",
    "web_search",
}
_WRITE_TOOL_NAMES = {
    "edit_file",
    "image_gen",
    "message",
    "write_file",
}
_EXEC_TOOL_NAMES = {
    "cron",
    "exec",
}
_STATE_PRIORITY = {
    "error": 5,
    "executing": 4,
    "writing": 3,
    "researching": 2,
    "syncing": 1,
    "idle": 0,
}
_VALID_STATES = frozenset(_STATE_PRIORITY)


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _isoformat_utc(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _normalize_state(state: str) -> str:
    normalized = (state or "").strip().lower()
    return normalized if normalized in _VALID_STATES else "syncing"


def _trim_detail(detail: str | None, *, max_chars: int = 240) -> str:
    text = (detail or "").strip()
    if not text:
        return ""
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


def _normalize_push_mode(mode: Any) -> str:
    normalized = str(mode or _PUSH_MODE_GUEST).strip().lower()
    if normalized not in {_PUSH_MODE_GUEST, _PUSH_MODE_MAIN}:
        return _PUSH_MODE_GUEST
    return normalized


@dataclass(slots=True, frozen=True)
class StarOfficeSnapshot:
    """Serializable Star Office status view."""

    state: str
    detail: str
    updated_at: str
    updated_at_ms: int
    active_runs: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": "nanobot",
            "state": self.state,
            "detail": self.detail,
            "updatedAt": self.updated_at,
            "updated_at": self.updated_at,
            "updatedAtMs": self.updated_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "activeRuns": self.active_runs,
        }


@dataclass(slots=True, frozen=True)
class StarOfficePushSettings:
    """HTTP push settings for Star Office UI integration."""

    enabled: bool = False
    mode: str = _PUSH_MODE_GUEST
    office_url: str = ""
    join_key: str = ""
    agent_name: str = "nanobot"
    timeout: float = 10.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", _normalize_push_mode(self.mode))

    @classmethod
    def from_status_config(cls, status_config: Any) -> "StarOfficePushSettings":
        push = getattr(status_config, "push", None)
        if push is None:
            return cls()
        return cls(
            enabled=bool(getattr(push, "enabled", False)),
            mode=_normalize_push_mode(getattr(push, "mode", _PUSH_MODE_GUEST)),
            office_url=str(getattr(push, "office_url", "") or "").strip(),
            join_key=str(getattr(push, "join_key", "") or "").strip(),
            agent_name=(str(getattr(push, "agent_name", "") or "").strip() or "nanobot"),
            timeout=float(getattr(push, "timeout", 10.0) or 10.0),
        )

    def is_main_mode(self) -> bool:
        return self.mode == _PUSH_MODE_MAIN

    def is_ready(self) -> bool:
        if not self.enabled or not self.office_url:
            return False
        if self.is_main_mode():
            return True
        return bool(self.join_key)


class StarOfficeRemoteRelay:
    """Push snapshots to a Star Office UI instance."""

    def __init__(self, settings: StarOfficePushSettings | None = None) -> None:
        self._settings = settings or StarOfficePushSettings()
        self._agent_id: str | None = None
        self._pending: StarOfficeSnapshot | None = None
        self._worker: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._last_failure: str | None = None

    def update_settings(self, settings: StarOfficePushSettings) -> None:
        self._settings = settings
        self._agent_id = None
        if not settings.enabled:
            self._pending = None
            self._last_failure = None

    def publish(self, snapshot: StarOfficeSnapshot) -> None:
        if self._closed:
            return
        settings = self._settings
        if not settings.is_ready():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._pending = snapshot
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._drain())

    async def flush(self) -> None:
        task = self._worker
        if task is not None:
            await asyncio.shield(task)

    async def aclose(self) -> None:
        self._closed = True
        task = self._worker
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _drain(self) -> None:
        while not self._closed:
            snapshot = self._pending
            self._pending = None
            if snapshot is None:
                return
            try:
                await self._push(snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_failure(f"Star Office push failed: {type(exc).__name__}: {exc}")
            if self._pending is None:
                return

    def _log_failure(self, message: str) -> None:
        if message == self._last_failure:
            return
        self._last_failure = message
        logger.warning(message)

    def _mark_success(self) -> None:
        self._last_failure = None

    async def _push(self, snapshot: StarOfficeSnapshot) -> None:
        async with self._lock:
            settings = self._settings
            if not settings.is_ready():
                return
            if settings.is_main_mode():
                if await self._push_main_update(settings, snapshot):
                    self._mark_success()
                return
            if self._agent_id is None and not await self._join(settings, snapshot):
                return
            if await self._push_update(settings, snapshot):
                self._mark_success()
                return
            self._agent_id = None
            if await self._join(settings, snapshot) and await self._push_update(settings, snapshot):
                self._mark_success()

    async def _join(self, settings: StarOfficePushSettings, snapshot: StarOfficeSnapshot) -> bool:
        payload = {
            "name": settings.agent_name,
            "joinKey": settings.join_key,
            "state": snapshot.state,
            "detail": snapshot.detail,
        }
        data, status = await self._post_json(self._build_url(settings, _PUSH_ROUTE_JOIN), payload, settings)
        agent_id = data.get("agentId")
        if status in {200, 201} and data.get("ok") is True and isinstance(agent_id, (int, str)):
            self._agent_id = str(agent_id)
            self._mark_success()
            return True
        self._log_failure(
            f"Star Office join failed ({status}): {data.get('error') or data.get('message') or 'unknown error'}"
        )
        return False

    async def _push_update(self, settings: StarOfficePushSettings, snapshot: StarOfficeSnapshot) -> bool:
        if not self._agent_id:
            return False
        payload = {
            "agentId": self._agent_id,
            "joinKey": settings.join_key,
            "state": snapshot.state,
            "detail": snapshot.detail,
        }
        data, status = await self._post_json(self._build_url(settings, _PUSH_ROUTE_UPDATE), payload, settings)
        if status in {200, 201} and data.get("ok") is True:
            return True
        self._log_failure(
            f"Star Office agent push failed ({status}): {data.get('error') or data.get('message') or 'unknown error'}"
        )
        return False

    async def _push_main_update(
        self,
        settings: StarOfficePushSettings,
        snapshot: StarOfficeSnapshot,
    ) -> bool:
        payload = {
            "state": snapshot.state,
            "detail": snapshot.detail,
        }
        data, status = await self._post_json(
            self._build_url(settings, _PUSH_ROUTE_MAIN_UPDATE),
            payload,
            settings,
        )
        if status in {200, 201} and (data.get("status") == "ok" or data.get("ok") is True):
            return True
        self._log_failure(
            f"Star Office main-state push failed ({status}): {data.get('error') or data.get('message') or 'unknown error'}"
        )
        return False

    @staticmethod
    def _build_url(settings: StarOfficePushSettings, suffix: str) -> str:
        return settings.office_url.rstrip("/") + suffix

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        settings: StarOfficePushSettings,
    ) -> tuple[dict[str, Any], int]:
        try:
            async with httpx.AsyncClient(timeout=settings.timeout, follow_redirects=True) as client:
                response = await client.post(url, json=payload)
        except httpx.RequestError as exc:
            self._log_failure(f"Star Office request error for {url}: {exc}")
            return {}, 0

        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        return data, response.status_code


class StarOfficeStatusTracker:
    """Track a single shared Star Office presence snapshot for the runtime."""

    def __init__(
        self,
        *,
        idle_detail: str = _DEFAULT_IDLE_DETAIL,
        push_settings: StarOfficePushSettings | None = None,
    ) -> None:
        self._idle_detail = _trim_detail(idle_detail) or _DEFAULT_IDLE_DETAIL
        timestamp_ms = _now_ms()
        self._latest = StarOfficeSnapshot(
            state="idle",
            detail=self._idle_detail,
            updated_at=_isoformat_utc(timestamp_ms),
            updated_at_ms=timestamp_ms,
            active_runs=0,
        )
        self._active_runs: dict[int, StarOfficeSnapshot] = {}
        self._relay = StarOfficeRemoteRelay(push_settings)

    def _snapshot(self, state: str, detail: str, *, active_runs: int = 0) -> StarOfficeSnapshot:
        timestamp_ms = _now_ms()
        return StarOfficeSnapshot(
            state=_normalize_state(state),
            detail=_trim_detail(detail),
            updated_at=_isoformat_utc(timestamp_ms),
            updated_at_ms=timestamp_ms,
            active_runs=active_runs,
        )

    def _publish_current(self) -> StarOfficeSnapshot:
        snapshot = self.snapshot()
        self._relay.publish(snapshot)
        return snapshot

    def apply_push_settings(self, settings: StarOfficePushSettings) -> None:
        self._relay.update_settings(settings)
        self._publish_current()

    def publish_current(self) -> None:
        self._publish_current()

    async def flush(self) -> None:
        await self._relay.flush()

    async def aclose(self) -> None:
        await self._relay.aclose()

    def update(self, run_id: int, *, state: str, detail: str = "") -> StarOfficeSnapshot:
        snapshot = self._snapshot(state, detail)
        self._active_runs[run_id] = snapshot
        return self._publish_current()

    def finish(self, run_id: int, *, state: str = "idle", detail: str = "") -> StarOfficeSnapshot:
        self._active_runs.pop(run_id, None)
        normalized_detail = detail or (self._idle_detail if state == "idle" else _DEFAULT_ERROR_DETAIL)
        self._latest = self._snapshot(state, normalized_detail)
        return self._publish_current()

    def snapshot(self) -> StarOfficeSnapshot:
        if not self._active_runs:
            return self._latest

        newest = max(
            self._active_runs.values(),
            key=lambda snapshot: (snapshot.updated_at_ms, _STATE_PRIORITY.get(snapshot.state, -1)),
        )
        return replace(newest, active_runs=len(self._active_runs))


def _tool_label(name: str) -> str:
    return name.replace("_", " ")


def _extract_latest_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return _trim_detail(content)
    return ""


def _classify_tool_calls(tool_calls: list[Any]) -> tuple[str, str]:
    names = [getattr(tool_call, "name", "") for tool_call in tool_calls if getattr(tool_call, "name", "")]
    if not names:
        return "syncing", "Synchronizing tool results"

    labels = ", ".join(_tool_label(name) for name in names[:3])
    if len(names) > 3:
        labels += ", ..."

    if any(name in _EXEC_TOOL_NAMES or name.startswith("mcp_") for name in names):
        return "executing", f"Running {labels}"
    if any(name in _WRITE_TOOL_NAMES for name in names):
        return "writing", f"Updating via {labels}"
    if all(name in _READ_ONLY_TOOL_NAMES for name in names):
        return "researching", f"Researching with {labels}"
    return "syncing", f"Using {labels}"


def _summarize_tool_events(events: list[dict[str, str]]) -> tuple[str, str]:
    if not events:
        return "syncing", "Synchronizing tool results"

    failed = [event for event in events if event.get("status") == "error"]
    if failed:
        event = failed[0]
        detail = event.get("detail") or _DEFAULT_ERROR_DETAIL
        return "error", _trim_detail(f"{event.get('name', 'tool')} failed: {detail}")

    names = ", ".join(event.get("name", "tool") for event in events[:3])
    if len(events) > 3:
        names += ", ..."
    return "syncing", f"Synchronizing {names}"


class StarOfficeHook(AgentHook):
    """Map agent lifecycle events onto Star Office presence states."""

    def __init__(self, tracker: StarOfficeStatusTracker, *, idle_detail: str = _DEFAULT_IDLE_DETAIL) -> None:
        self._tracker = tracker
        self._idle_detail = _trim_detail(idle_detail) or _DEFAULT_IDLE_DETAIL

    @staticmethod
    def _run_id() -> int:
        task = asyncio.current_task()
        return id(task) if task is not None else 0

    def _update(self, *, state: str, detail: str) -> None:
        self._tracker.update(self._run_id(), state=state, detail=detail)

    def _finish(self, *, state: str, detail: str) -> None:
        self._tracker.finish(self._run_id(), state=state, detail=detail)

    async def before_iteration(self, context: AgentHookContext) -> None:
        detail = _extract_latest_user_text(context.messages) or "Processing request"
        if context.iteration == 0:
            self._update(state="writing", detail=detail)
            return
        self._update(state="syncing", detail="Preparing final response")

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        state, detail = _classify_tool_calls(context.tool_calls)
        self._update(state=state, detail=detail)

    async def after_iteration(self, context: AgentHookContext) -> None:
        if context.error or context.stop_reason in {"error", "tool_error", "max_iterations"}:
            detail = context.error or context.final_content or _DEFAULT_ERROR_DETAIL
            self._finish(state="error", detail=detail)
            return
        if context.final_content is not None:
            self._finish(state="idle", detail=self._idle_detail)
            return
        state, detail = _summarize_tool_events(context.tool_events)
        self._update(state=state, detail=detail)
