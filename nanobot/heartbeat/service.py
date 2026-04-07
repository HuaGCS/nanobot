"""Heartbeat service - periodic agent wake-up to check for tasks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from loguru import logger

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider

_HEARTBEAT_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "heartbeat",
            "description": "Report heartbeat decision after reviewing tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["skip", "run"],
                        "description": "skip = nothing to do, run = has active tasks",
                    },
                    "tasks": {
                        "type": "string",
                        "description": "Natural-language summary of active tasks (required for run)",
                    },
                },
                "required": ["action"],
            },
        },
    }
]


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


@dataclass(slots=True, frozen=True)
class HeartbeatStatusSnapshot:
    """Serializable heartbeat runtime state for the gateway status page."""

    enabled: bool
    running: bool
    model: str
    interval_s: int
    last_status: str
    last_detail: str
    last_checked_at: str | None
    last_checked_at_ms: int | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "model": self.model,
            "intervalS": self.interval_s,
            "interval_s": self.interval_s,
            "lastStatus": self.last_status,
            "last_status": self.last_status,
            "lastDetail": self.last_detail,
            "last_detail": self.last_detail,
            "lastCheckedAt": self.last_checked_at,
            "last_checked_at": self.last_checked_at,
            "lastCheckedAtMs": self.last_checked_at_ms,
            "last_checked_at_ms": self.last_checked_at_ms,
        }


class HeartbeatService:
    """
    Periodic heartbeat service that wakes the agent to check for tasks.

    Phase 1 (decision): reads HEARTBEAT.md and asks the LLM — via a virtual
    tool call — whether there are active tasks.  This avoids free-text parsing
    and the unreliable HEARTBEAT_OK token.

    Phase 2 (execution): only triggered when Phase 1 returns ``run``.  The
    ``on_execute`` callback runs the task through the full agent loop and
    returns the result to deliver.
    """

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        on_execute: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        on_notify: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        interval_s: int = 30 * 60,
        enabled: bool = True,
        timezone: str | None = None,
    ):
        self.workspace = workspace
        self.provider = provider
        self.model = model
        self.on_execute = on_execute
        self.on_notify = on_notify
        self.interval_s = interval_s
        self.enabled = enabled
        self.timezone = timezone
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_status = "idle" if enabled else "disabled"
        self._last_detail = "Heartbeat has not run yet" if enabled else "Heartbeat disabled"
        self._last_checked_at_ms: int | None = None

    @property
    def heartbeat_file(self) -> Path:
        return self.workspace / "HEARTBEAT.md"

    def _read_heartbeat_file(self) -> str | None:
        if self.heartbeat_file.exists():
            try:
                return self.heartbeat_file.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    def _set_status(self, status: str, detail: str, *, checked: bool = False) -> None:
        self._last_status = status
        self._last_detail = _trim_text(detail)
        if checked:
            self._last_checked_at_ms = _now_ms()

    def snapshot(self) -> HeartbeatStatusSnapshot:
        return HeartbeatStatusSnapshot(
            enabled=self.enabled,
            running=self._running,
            model=self.model,
            interval_s=self.interval_s,
            last_status=self._last_status,
            last_detail=self._last_detail,
            last_checked_at=_isoformat_utc(self._last_checked_at_ms),
            last_checked_at_ms=self._last_checked_at_ms,
        )

    async def _decide(self, content: str) -> tuple[str, str]:
        """Phase 1: ask LLM to decide skip/run via virtual tool call.

        Returns (action, tasks) where action is 'skip' or 'run'.
        """
        from nanobot.utils.helpers import current_time_str

        response = await self.provider.chat_with_retry(
            messages=[
                {"role": "system", "content": "You are a heartbeat agent. Call the heartbeat tool to report your decision."},
                {"role": "user", "content": (
                    f"Current Time: {current_time_str(self.timezone)}\n\n"
                    "Review the following HEARTBEAT.md and decide whether there are active tasks.\n\n"
                    f"{content}"
                )},
            ],
            tools=_HEARTBEAT_TOOL,
            model=self.model,
        )

        if not response.has_tool_calls:
            return "skip", ""

        args = response.tool_calls[0].arguments
        return args.get("action", "skip"), args.get("tasks", "")

    async def start(self) -> None:
        """Start the heartbeat service."""
        if not self.enabled:
            self._set_status("disabled", "Heartbeat disabled")
            logger.info("Heartbeat disabled")
            return
        if self._running:
            logger.warning("Heartbeat already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        self._set_status("idle", "Heartbeat service running")
        logger.info("Heartbeat started (every {}s)", self.interval_s)

    def stop(self) -> None:
        """Stop the heartbeat service."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        if not self.enabled:
            self._set_status("disabled", "Heartbeat disabled")

    async def apply_runtime_config(
        self,
        *,
        workspace: Path,
        model: str,
        interval_s: int,
        enabled: bool,
        timezone: str | None,
    ) -> None:
        """Apply runtime-configurable heartbeat settings in place."""
        interval_changed = self.interval_s != interval_s
        enabled_changed = self.enabled != enabled

        self.workspace = workspace
        self.model = model
        self.interval_s = interval_s
        self.enabled = enabled
        self.timezone = timezone

        if self._running and not enabled:
            self.stop()
            return

        if not self._running and enabled:
            await self.start()
            return

        if self._running and (interval_changed or enabled_changed):
            self.stop()
            await self.start()
            return

        if not enabled:
            self._set_status("disabled", "Heartbeat disabled")

    async def _run_loop(self) -> None:
        """Main heartbeat loop."""
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Heartbeat error: {}", e)

    async def _tick(self) -> None:
        """Execute a single heartbeat tick."""
        content = self._read_heartbeat_file()
        if not content:
            self._set_status("missing", "HEARTBEAT.md missing or empty", checked=True)
            logger.debug("Heartbeat: HEARTBEAT.md missing or empty")
            return

        self._set_status("checking", "Checking HEARTBEAT.md", checked=True)
        logger.info("Heartbeat: checking for tasks...")

        try:
            action, tasks = await self._decide(content)

            if action != "run":
                self._set_status("skipped", "No active heartbeat tasks detected", checked=True)
                logger.info("Heartbeat: OK (nothing to report)")
                return

            self._set_status(
                "running",
                tasks or "Heartbeat tasks found, executing",
                checked=True,
            )
            logger.info("Heartbeat: tasks found, executing...")
            if self.on_execute:
                response = await self.on_execute(tasks)
                if response and self.on_notify:
                    logger.info("Heartbeat: completed, delivering response")
                    await self.on_notify(response)
                self._set_status(
                    "ok",
                    response or tasks or "Heartbeat tasks completed",
                    checked=True,
                )
                return
            self._set_status("ok", tasks or "Heartbeat tasks completed", checked=True)
        except Exception:
            self._set_status("error", "Heartbeat execution failed", checked=True)
            logger.exception("Heartbeat execution failed")

    async def trigger_now(self) -> str | None:
        """Manually trigger a heartbeat."""
        content = self._read_heartbeat_file()
        if not content:
            self._set_status("missing", "HEARTBEAT.md missing or empty", checked=True)
            return None
        action, tasks = await self._decide(content)
        if action != "run" or not self.on_execute:
            self._set_status("skipped", "No active heartbeat tasks detected", checked=True)
            return None
        self._set_status("running", tasks or "Heartbeat tasks found, executing", checked=True)
        result = await self.on_execute(tasks)
        self._set_status("ok", result or tasks or "Heartbeat tasks completed", checked=True)
        return result
