"""Minimal HTTP server for gateway health checks."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from html import escape
from pathlib import Path

from aiohttp import web
from loguru import logger

from nanobot.config.loader import load_config
from nanobot.gateway.admin import register_admin_routes, update_admin_runtime_workspace
from nanobot.gateway.runtime_status import GatewayRuntimeStatusTracker
from nanobot.heartbeat.service import HeartbeatService, HeartbeatStatusSnapshot
from nanobot.star_office import StarOfficeStatusTracker

_STAR_OFFICE_TRACKER_KEY = web.AppKey("star_office_tracker", object)
_RUNTIME_STATUS_TRACKER_KEY = web.AppKey("runtime_status_tracker", object)
_HEARTBEAT_SERVICE_KEY = web.AppKey("heartbeat_service", object)


def _status_tracker(app: web.Application) -> StarOfficeStatusTracker | None:
    tracker = app.get(_STAR_OFFICE_TRACKER_KEY)
    return tracker if isinstance(tracker, StarOfficeStatusTracker) else None


def _runtime_status_tracker(app: web.Application) -> GatewayRuntimeStatusTracker | None:
    tracker = app.get(_RUNTIME_STATUS_TRACKER_KEY)
    return tracker if isinstance(tracker, GatewayRuntimeStatusTracker) else None


def _heartbeat_service(app: web.Application) -> HeartbeatService | None:
    service = app.get(_HEARTBEAT_SERVICE_KEY)
    return service if isinstance(service, HeartbeatService) else None


def _is_authorized(request: web.Request, auth_key: str) -> bool:
    if not auth_key:
        return True
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header.removeprefix("Bearer ").strip()
    return hmac.compare_digest(token, auth_key)


def _wants_html(request: web.Request) -> bool:
    format_arg = request.query.get("format", "").strip().lower()
    if format_arg == "html":
        return True
    if format_arg == "json":
        return False

    accept = request.headers.get("Accept", "").lower()
    return "text/html" in accept and "application/json" not in accept


def _default_heartbeat_snapshot() -> HeartbeatStatusSnapshot:
    return HeartbeatStatusSnapshot(
        enabled=False,
        running=False,
        model="",
        interval_s=0,
        last_status="unavailable",
        last_detail="Heartbeat status unavailable",
        last_checked_at=None,
        last_checked_at_ms=None,
    )


def _status_badge_class(status: str) -> str:
    normalized = (status or "").strip().lower()
    if normalized in {"ok", "idle", "running"}:
        return "badge ok"
    if normalized in {"checking", "skipped"}:
        return "badge warn"
    return "badge err"


def _runtime_health_text(health: str) -> str:
    return "正常运行" if health == "ok" else "运行异常"


def _task_status_text(status: str) -> str:
    mapping = {
        "running": "处理中",
        "ok": "已完成",
        "error": "失败",
    }
    return mapping.get(status, status or "未知")


def _heartbeat_status_text(status: str) -> str:
    mapping = {
        "disabled": "已关闭",
        "idle": "等待首次检测",
        "checking": "检测中",
        "missing": "未找到 HEARTBEAT.md",
        "skipped": "无待处理任务",
        "running": "执行中",
        "ok": "最近一次成功",
        "error": "最近一次失败",
        "unavailable": "不可用",
    }
    return mapping.get(status, status or "未知")


def _render_status_page(
    *,
    runtime_snapshot,
    heartbeat_snapshot: HeartbeatStatusSnapshot,
) -> str:
    task = runtime_snapshot.recent_task
    task_html = (
        (
            f'<div class="stack">'
            f'<div class="task-title">{escape(task.summary)}</div>'
            f'<div class="meta-row"><span class="{_status_badge_class(task.status)}">{escape(_task_status_text(task.status))}</span>'
            f'<span>开始于 <code>{escape(task.started_at or "-")}</code></span></div>'
            f'<div class="meta-row"><span>结束于 <code>{escape(task.finished_at or "仍在处理中")}</code></span></div>'
            f'<div class="muted">{escape(task.response_preview or "暂无响应摘要")}</div>'
            f"</div>"
        )
        if task is not None
        else '<div class="muted">当前实例启动后还没有处理过任务。</div>'
    )
    heartbeat_model = heartbeat_snapshot.model or runtime_snapshot.model or "unknown"
    heartbeat_interval = (
        f"{heartbeat_snapshot.interval_s}s"
        if heartbeat_snapshot.interval_s > 0
        else "未配置"
    )
    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>nanobot Status</title>
    <style>
      :root {{
        --bg: #f4efe6;
        --panel: rgba(255, 250, 241, 0.92);
        --panel-strong: #fffdf9;
        --text: #1f2328;
        --muted: #6e6255;
        --ok: #1e7a46;
        --ok-bg: #e3f6e8;
        --warn: #8a5a00;
        --warn-bg: #fff0cf;
        --err: #a52a2a;
        --err-bg: #ffe4e1;
        --line: rgba(61, 43, 23, 0.12);
        --shadow: 0 18px 48px rgba(88, 56, 21, 0.12);
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        font-family: "Avenir Next", "PingFang SC", "Noto Sans CJK SC", sans-serif;
        color: var(--text);
        background:
          radial-gradient(circle at top left, rgba(226, 182, 104, 0.35), transparent 34%),
          radial-gradient(circle at top right, rgba(120, 180, 140, 0.18), transparent 28%),
          linear-gradient(180deg, #fbf6ee 0%, var(--bg) 100%);
      }}
      main {{
        width: min(1120px, calc(100vw - 32px));
        margin: 32px auto;
        display: grid;
        gap: 18px;
      }}
      .hero, .card {{
        border: 1px solid var(--line);
        background: var(--panel);
        backdrop-filter: blur(12px);
        box-shadow: var(--shadow);
        border-radius: 24px;
      }}
      .hero {{
        padding: 28px;
        display: grid;
        gap: 10px;
      }}
      .eyebrow {{
        letter-spacing: 0.18em;
        text-transform: uppercase;
        color: var(--muted);
        font-size: 12px;
      }}
      h1 {{
        margin: 0;
        font-size: clamp(30px, 4vw, 48px);
        line-height: 1.05;
      }}
      .sub {{
        margin: 0;
        max-width: 70ch;
        color: var(--muted);
        line-height: 1.6;
      }}
      .grid {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 18px;
      }}
      .card {{
        padding: 22px;
        display: grid;
        gap: 14px;
      }}
      .card h2 {{
        margin: 0;
        font-size: 20px;
      }}
      .kpi {{
        font-size: clamp(28px, 3vw, 40px);
        font-weight: 700;
      }}
      .badge {{
        display: inline-flex;
        width: fit-content;
        align-items: center;
        gap: 8px;
        padding: 6px 12px;
        border-radius: 999px;
        font-size: 13px;
        font-weight: 700;
      }}
      .badge.ok {{ background: var(--ok-bg); color: var(--ok); }}
      .badge.warn {{ background: var(--warn-bg); color: var(--warn); }}
      .badge.err {{ background: var(--err-bg); color: var(--err); }}
      .meta {{
        display: grid;
        gap: 8px;
        color: var(--muted);
      }}
      .meta-row {{
        display: flex;
        flex-wrap: wrap;
        gap: 10px 14px;
        align-items: center;
        color: var(--muted);
      }}
      .muted {{
        color: var(--muted);
        line-height: 1.6;
      }}
      .stack {{
        display: grid;
        gap: 12px;
      }}
      .task-title {{
        font-size: 18px;
        font-weight: 700;
      }}
      code {{
        font-family: "JetBrains Mono", "SFMono-Regular", monospace;
        font-size: 0.92em;
      }}
      @media (max-width: 860px) {{
        main {{ width: min(100vw - 20px, 1120px); margin: 20px auto; }}
        .grid {{ grid-template-columns: 1fr; }}
        .hero, .card {{ border-radius: 20px; }}
      }}
    </style>
  </head>
  <body>
    <main>
      <section class="hero">
        <div class="eyebrow">nanobot gateway status</div>
        <h1>运行状态页</h1>
        <p class="sub">当前实例会继续对脚本访问保持 `/status` JSON 兼容；浏览器访问时显示这个状态页，汇总运行健康度、连续运行时间、最近一次任务和 heartbeat 检测情况。</p>
      </section>
      <section class="grid">
        <article class="card">
          <h2>nanobot 是否正常运行</h2>
          <div class="kpi">{escape(_runtime_health_text(runtime_snapshot.health))}</div>
          <div class="meta">
            <div class="meta-row">
              <span class="{_status_badge_class(runtime_snapshot.current_state)}">{escape(runtime_snapshot.current_state)}</span>
              <span>活跃任务数 <strong>{runtime_snapshot.active_runs}</strong></span>
            </div>
            <div>{escape(runtime_snapshot.current_detail or "暂无详细状态")}</div>
            <div>当前模型 <code>{escape(runtime_snapshot.model or "unknown")}</code></div>
          </div>
        </article>
        <article class="card">
          <h2>连续运行时间</h2>
          <div class="kpi">{escape(runtime_snapshot.uptime_text)}</div>
          <div class="meta">
            <div>启动时间 <code>{escape(runtime_snapshot.started_at)}</code></div>
            <div>已持续运行 <strong>{runtime_snapshot.uptime_s}</strong> 秒</div>
          </div>
        </article>
        <article class="card">
          <h2>最近一次处理的任务</h2>
          {task_html}
        </article>
        <article class="card">
          <h2>模型心跳测试状态</h2>
          <div class="meta-row">
            <span class="{_status_badge_class(heartbeat_snapshot.last_status)}">{escape(_heartbeat_status_text(heartbeat_snapshot.last_status))}</span>
            <span>模型 <code>{escape(heartbeat_model)}</code></span>
          </div>
          <div class="meta">
            <div>Heartbeat 开关 <strong>{'开启' if heartbeat_snapshot.enabled else '关闭'}</strong></div>
            <div>Heartbeat 运行态 <strong>{'运行中' if heartbeat_snapshot.running else '未运行'}</strong></div>
            <div>检测间隔 <code>{escape(heartbeat_interval)}</code></div>
            <div>最近检测时间 <code>{escape(heartbeat_snapshot.last_checked_at or "-")}</code></div>
            <div>{escape(heartbeat_snapshot.last_detail or "暂无 heartbeat 检测记录")}</div>
          </div>
        </article>
      </section>
    </main>
  </body>
</html>
"""


def create_http_app(
    *,
    config_path: Path | None = None,
    workspace: Path | None = None,
    reload_runtime: Callable[[], Awaitable[None]] | None = None,
    star_office_tracker: StarOfficeStatusTracker | None = None,
    runtime_status_tracker: GatewayRuntimeStatusTracker | None = None,
    heartbeat_service: HeartbeatService | None = None,
) -> web.Application:
    """Create the gateway HTTP app."""
    app = web.Application()
    if config_path is not None and star_office_tracker is None:
        star_office_tracker = StarOfficeStatusTracker()
    if config_path is not None and runtime_status_tracker is None:
        runtime_status_tracker = GatewayRuntimeStatusTracker()
    if star_office_tracker is not None:
        app[_STAR_OFFICE_TRACKER_KEY] = star_office_tracker
    if runtime_status_tracker is not None:
        app[_RUNTIME_STATUS_TRACKER_KEY] = runtime_status_tracker
    if heartbeat_service is not None:
        app[_HEARTBEAT_SERVICE_KEY] = heartbeat_service

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def status(request: web.Request) -> web.Response:
        if config_path is None:
            raise web.HTTPNotFound()

        config = load_config(config_path)
        status_cfg = config.gateway.status
        if not status_cfg.enabled:
            raise web.HTTPNotFound()
        if not _is_authorized(request, status_cfg.auth_key):
            return web.json_response(
                {"error": "unauthorized"},
                status=401,
                headers={"WWW-Authenticate": 'Bearer realm="nanobot-status"'},
            )

        tracker = _status_tracker(request.app)
        star_snapshot = (
            tracker.snapshot()
            if tracker is not None
            else StarOfficeStatusTracker().snapshot()
        )
        if _wants_html(request):
            runtime_tracker = _runtime_status_tracker(request.app) or GatewayRuntimeStatusTracker()
            heartbeat_snapshot = (
                _heartbeat_service(request.app).snapshot()
                if _heartbeat_service(request.app) is not None
                else _default_heartbeat_snapshot()
            )
            html = _render_status_page(
                runtime_snapshot=runtime_tracker.snapshot(star_snapshot),
                heartbeat_snapshot=heartbeat_snapshot,
            )
            return web.Response(text=html, content_type="text/html")
        payload = star_snapshot.to_payload()
        return web.json_response(payload)

    app.router.add_get("/healthz", health)
    if config_path is not None:
        app.router.add_get("/status", status)
    if config_path is not None and workspace is not None:
        register_admin_routes(
            app,
            config_path=config_path,
            workspace=workspace,
            reload_runtime=reload_runtime,
        )
    return app


class GatewayHttpServer:
    """Small aiohttp server exposing health checks."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        config_path: Path | None = None,
        workspace: Path | None = None,
        reload_runtime: Callable[[], Awaitable[None]] | None = None,
        star_office_tracker: StarOfficeStatusTracker | None = None,
        runtime_status_tracker: GatewayRuntimeStatusTracker | None = None,
        heartbeat_service: HeartbeatService | None = None,
    ):
        self.host = host
        self.port = port
        self._app = create_http_app(
            config_path=config_path,
            workspace=workspace,
            reload_runtime=reload_runtime,
            star_office_tracker=star_office_tracker,
            runtime_status_tracker=runtime_status_tracker,
            heartbeat_service=heartbeat_service,
        )
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    def update_runtime_workspace(self, workspace: Path) -> None:
        """Update the admin UI runtime-workspace pointer after a hot reload."""
        update_admin_runtime_workspace(self._app, workspace)

    async def start(self) -> None:
        """Start serving the HTTP routes."""
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self.host, port=self.port)
        await self._site.start()
        logger.info(
            "Gateway HTTP server listening on {}:{} (/healthz, optional /status, optional /admin)",
            self.host,
            self.port,
        )

    async def stop(self) -> None:
        """Stop the HTTP server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
