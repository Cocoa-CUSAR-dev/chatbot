from httpx import AsyncClient

from src.exceptions import ServiceException, UpstreamServiceError
from src.main import service_exception_handler


async def test_service_exception_handler_returns_its_status_code_and_detail() -> None:
    response = await service_exception_handler(None, ServiceException("something specific"))

    assert response.status_code == 400
    assert response.body == b'{"detail":"something specific"}'


async def test_service_exception_handler_uses_the_subclasss_own_status_code() -> None:
    response = await service_exception_handler(None, UpstreamServiceError())

    assert response.status_code == 502


async def test_dev_conversation_router_is_registered_in_local_environment(
    client: AsyncClient,
) -> None:
    # tests/conftest.py never sets ENVIRONMENT, so Settings() defaults to
    # Environment.LOCAL (not deployed) -- main.py only skips registering
    # this router when settings.ENVIRONMENT.is_deployed is True.
    response = await client.get("/conversation/test/ui")
    assert response.status_code == 200


async def test_openapi_is_exposed_when_not_deployed(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
