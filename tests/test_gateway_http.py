import os
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

from nanobot.gateway.http import create_http_app, get_public_dir


@pytest.mark.asyncio
async def test_gateway_public_route_maps_requests_into_workspace_public(tmp_path) -> None:
    public_dir = get_public_dir(tmp_path)
    file_path = public_dir / "hello.txt"
    file_path.write_text("hello", encoding="utf-8")

    app = create_http_app(tmp_path)
    request = make_mocked_request("GET", "/public/hello.txt", app=app)
    match = await app.router.resolve(request)

    assert match.route.resource.canonical == "/public"
    assert match["filename"] == "hello.txt"
    assert Path(getattr(match.route.resource, "_directory")) == public_dir


@pytest.mark.asyncio
async def test_gateway_public_route_disables_symlink_following_and_allows_hard_links(tmp_path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    source = out_dir / "shot.png"
    source.write_bytes(b"png")

    public_dir = get_public_dir(tmp_path) / "qq"
    public_dir.mkdir()
    published = public_dir / "shot.png"
    os.link(source, published)

    app = create_http_app(tmp_path)
    request = make_mocked_request("GET", "/public/qq/shot.png", app=app)
    match = await app.router.resolve(request)

    assert os.stat(source).st_ino == os.stat(published).st_ino
    assert match.route.resource.canonical == "/public"
    assert match["filename"] == "qq/shot.png"
    assert getattr(match.route.resource, "_follow_symlinks") is False
