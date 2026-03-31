"""Minimal HTTP server for gateway health checks."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web
from loguru import logger

from nanobot.config.loader import load_config
from nanobot.gateway.admin import register_admin_routes, update_admin_runtime_workspace
from nanobot.star_office import StarOfficeStatusTracker

_STAR_OFFICE_TRACKER_KEY = web.AppKey("star_office_tracker", object)


def _status_tracker(app: web.Application) -> StarOfficeStatusTracker | None:
    tracker = app.get(_STAR_OFFICE_TRACKER_KEY)
    return tracker if isinstance(tracker, StarOfficeStatusTracker) else None


def _is_authorized(request: web.Request, auth_key: str) -> bool:
    if not auth_key:
        return True
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header.removeprefix("Bearer ").strip()
    return hmac.compare_digest(token, auth_key)


def create_http_app(
    *,
    config_path: Path | None = None,
    workspace: Path | None = None,
    reload_runtime: Callable[[], Awaitable[None]] | None = None,
    star_office_tracker: StarOfficeStatusTracker | None = None,
) -> web.Application:
    """Create the gateway HTTP app."""
    app = web.Application()
    if config_path is not None and star_office_tracker is None:
        star_office_tracker = StarOfficeStatusTracker()
    if star_office_tracker is not None:
        app[_STAR_OFFICE_TRACKER_KEY] = star_office_tracker

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
        payload = (
            tracker.snapshot().to_payload()
            if tracker is not None
            else StarOfficeStatusTracker().snapshot().to_payload()
        )
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
    ):
        self.host = host
        self.port = port
        self._app = create_http_app(
            config_path=config_path,
            workspace=workspace,
            reload_runtime=reload_runtime,
            star_office_tracker=star_office_tracker,
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
