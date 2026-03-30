"""Minimal HTTP server for gateway health checks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web
from loguru import logger

from nanobot.gateway.admin import register_admin_routes, update_admin_runtime_workspace


def create_http_app(
    *,
    config_path: Path | None = None,
    workspace: Path | None = None,
    reload_runtime: Callable[[], Awaitable[None]] | None = None,
) -> web.Application:
    """Create the gateway HTTP app."""
    app = web.Application()

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app.router.add_get("/healthz", health)
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
    ):
        self.host = host
        self.port = port
        self._app = create_http_app(
            config_path=config_path,
            workspace=workspace,
            reload_runtime=reload_runtime,
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
        logger.info("Gateway HTTP server listening on {}:{} (/healthz, optional /admin)", self.host, self.port)

    async def stop(self) -> None:
        """Stop the HTTP server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
