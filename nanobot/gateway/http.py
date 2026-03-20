"""Minimal HTTP server for gateway health checks."""

from __future__ import annotations

from aiohttp import web
from loguru import logger


def create_http_app() -> web.Application:
    """Create the gateway HTTP app."""
    app = web.Application()

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app.router.add_get("/healthz", health)
    return app


class GatewayHttpServer:
    """Small aiohttp server exposing health checks."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._app = create_http_app()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        """Start serving the HTTP routes."""
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self.host, port=self.port)
        await self._site.start()
        logger.info("Gateway HTTP server listening on {}:{} (/healthz)", self.host, self.port)

    async def stop(self) -> None:
        """Stop the HTTP server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
