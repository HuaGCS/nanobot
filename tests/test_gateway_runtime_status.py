from __future__ import annotations

import pytest

from nanobot.agent.hook import AgentHookContext
from nanobot.gateway.runtime_status import GatewayRuntimeStatusTracker, GatewayStatusHook
from nanobot.star_office import StarOfficeSnapshot


def _idle_snapshot() -> StarOfficeSnapshot:
    return StarOfficeSnapshot(
        state="idle",
        detail="Ready",
        updated_at="2026-04-07T00:00:00Z",
        updated_at_ms=1,
        active_runs=0,
    )


@pytest.mark.asyncio
async def test_gateway_status_hook_tracks_latest_completed_task() -> None:
    tracker = GatewayRuntimeStatusTracker(model="openrouter/sonnet")
    hook = GatewayStatusHook(tracker)
    context = AgentHookContext(
        iteration=0,
        messages=[{"role": "user", "content": "Review the latest incident report"}],
    )

    await hook.before_iteration(context)
    context.final_content = "Incident report reviewed."
    context.stop_reason = "completed"
    await hook.after_iteration(context)

    snapshot = tracker.snapshot(_idle_snapshot())

    assert snapshot.model == "openrouter/sonnet"
    assert snapshot.recent_task is not None
    assert snapshot.recent_task.summary == "Review the latest incident report"
    assert snapshot.recent_task.status == "ok"
    assert snapshot.recent_task.response_preview == "Incident report reviewed."
