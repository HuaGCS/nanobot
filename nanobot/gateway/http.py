"""Minimal HTTP server for workspace-scoped public files."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web
from loguru import logger

from nanobot.utils.helpers import ensure_dir


def get_public_dir(workspace: Path) -> Path:
    """Return the fixed public directory served by the gateway."""
    return ensure_dir(workspace / "public")


def create_http_app(workspace: Path) -> web.Application:
    """Create the gateway HTTP app serving workspace/public."""
    public_dir = get_public_dir(workspace)
    app = web.Application()

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app.router.add_get("/healthz", health)
    app.router.add_static("/public/", path=str(public_dir), follow_symlinks=False, show_index=False)
    return app


class GatewayHttpServer:
    """Small aiohttp server exposing only workspace/public."""

    def __init__(self, workspace: Path, host: str, port: int):
        self.workspace = workspace
        self.host = host
        self.port = port
        self._app = create_http_app(workspace)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def public_dir(self) -> Path:
        """Return the served public directory."""
        return get_public_dir(self.workspace)

    async def start(self) -> None:
        """Start serving the HTTP routes."""
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self.host, port=self.port)
        await self._site.start()
        logger.info(
            "Gateway HTTP server listening on {}:{} (public dir: {})",
            self.host,
            self.port,
            self.public_dir,
        )

    async def stop(self) -> None:
        """Stop the HTTP server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
