"""Gateway runtime status tracking for the human-readable status page."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.star_office import StarOfficeSnapshot

_DEFAULT_TASK_SUMMARY = "Processing request"


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _isoformat_utc(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _trim_text(text: str | None, *, max_chars: int = 240) -> str:
    value = (text or "").strip()
    if not value:
        return ""
    return value if len(value) <= max_chars else value[: max_chars - 3] + "..."


def _extract_latest_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return _trim_text(content)
    return ""


def _format_uptime(total_seconds: int) -> str:
    days, rem = divmod(max(total_seconds, 0), 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


@dataclass(slots=True, frozen=True)
class GatewayTaskSnapshot:
    """Latest visible task summary for the status page."""

    summary: str
    status: str
    started_at: str | None
    started_at_ms: int | None
    finished_at: str | None
    finished_at_ms: int | None
    response_preview: str


@dataclass(slots=True, frozen=True)
class GatewayRuntimeSnapshot:
    """Aggregated runtime status for the human-readable status page."""

    started_at: str
    started_at_ms: int
    uptime_s: int
    uptime_text: str
    model: str
    health: str
    current_state: str
    current_detail: str
    active_runs: int
    recent_task: GatewayTaskSnapshot | None


@dataclass(slots=True)
class _TrackedTask:
    summary: str
    status: str
    started_at_ms: int
    finished_at_ms: int | None = None
    response_preview: str = ""

    def to_snapshot(self) -> GatewayTaskSnapshot:
        return GatewayTaskSnapshot(
            summary=self.summary,
            status=self.status,
            started_at=_isoformat_utc(self.started_at_ms),
            started_at_ms=self.started_at_ms,
            finished_at=_isoformat_utc(self.finished_at_ms),
            finished_at_ms=self.finished_at_ms,
            response_preview=self.response_preview,
        )


class GatewayRuntimeStatusTracker:
    """Track uptime, recent tasks, and model name for `/status` HTML rendering."""

    def __init__(self, *, model: str = "") -> None:
        self._started_at_ms = _now_ms()
        self._model = (model or "").strip()
        self._active_tasks: dict[int, _TrackedTask] = {}
        self._last_task: _TrackedTask | None = None

    def set_model(self, model: str) -> None:
        self._model = (model or "").strip()

    def note_task_started(self, run_id: int, summary: str) -> None:
        self._active_tasks[run_id] = _TrackedTask(
            summary=_trim_text(summary) or _DEFAULT_TASK_SUMMARY,
            status="running",
            started_at_ms=_now_ms(),
        )

    def note_task_finished(
        self,
        run_id: int,
        *,
        status: str,
        response_preview: str = "",
    ) -> None:
        task = self._active_tasks.pop(run_id, None)
        if task is None:
            task = _TrackedTask(
                summary=_DEFAULT_TASK_SUMMARY,
                status=status,
                started_at_ms=_now_ms(),
            )
        task.status = status
        task.finished_at_ms = _now_ms()
        task.response_preview = _trim_text(response_preview)
        if self._last_task is None or (task.finished_at_ms or 0) >= (self._last_task.finished_at_ms or 0):
            self._last_task = task

    def _recent_task_snapshot(self) -> GatewayTaskSnapshot | None:
        if self._active_tasks:
            task = max(self._active_tasks.values(), key=lambda item: item.started_at_ms)
            return task.to_snapshot()
        if self._last_task is not None:
            return self._last_task.to_snapshot()
        return None

    def snapshot(self, star_snapshot: StarOfficeSnapshot) -> GatewayRuntimeSnapshot:
        now_ms = _now_ms()
        recent_task = self._recent_task_snapshot()
        health = "error" if star_snapshot.state == "error" else "ok"
        return GatewayRuntimeSnapshot(
            started_at=_isoformat_utc(self._started_at_ms) or "",
            started_at_ms=self._started_at_ms,
            uptime_s=max((now_ms - self._started_at_ms) // 1000, 0),
            uptime_text=_format_uptime(max((now_ms - self._started_at_ms) // 1000, 0)),
            model=self._model,
            health=health,
            current_state=star_snapshot.state,
            current_detail=star_snapshot.detail,
            active_runs=star_snapshot.active_runs,
            recent_task=recent_task,
        )


class GatewayStatusHook(AgentHook):
    """Record the most recent processed task for the gateway status page."""

    def __init__(self, tracker: GatewayRuntimeStatusTracker) -> None:
        self._tracker = tracker

    @staticmethod
    def _run_id() -> int:
        task = asyncio.current_task()
        return id(task) if task is not None else 0

    async def before_iteration(self, context: AgentHookContext) -> None:
        if context.iteration != 0:
            return
        summary = _extract_latest_user_text(context.messages) or _DEFAULT_TASK_SUMMARY
        self._tracker.note_task_started(self._run_id(), summary)

    async def after_iteration(self, context: AgentHookContext) -> None:
        if context.stop_reason is None and context.final_content is None and context.error is None:
            return
        status = "error" if context.error else "ok"
        preview = context.error or context.final_content or ""
        self._tracker.note_task_finished(
            self._run_id(),
            status=status,
            response_preview=preview,
        )
