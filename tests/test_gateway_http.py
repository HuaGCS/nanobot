import pytest
from aiohttp.test_utils import make_mocked_request

from nanobot.gateway.http import create_http_app


@pytest.mark.asyncio
async def test_gateway_health_route_exists() -> None:
    app = create_http_app()
    request = make_mocked_request("GET", "/healthz", app=app)
    match = await app.router.resolve(request)

    assert match.route.resource.canonical == "/healthz"


@pytest.mark.asyncio
async def test_gateway_public_route_is_not_registered() -> None:
    app = create_http_app()
    request = make_mocked_request("GET", "/public/hello.txt", app=app)
    match = await app.router.resolve(request)

    assert match.http_exception.status == 404
    assert [resource.canonical for resource in app.router.resources()] == ["/healthz"]
