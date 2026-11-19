import asyncio

import httpx
import pytest

from ccsync_scheduler.main import app

API_KEY_HEADERS = {"X-CCSync-Key": "test-scheduler-key"}


async def _get_with_lifespan(
    path: str,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://local.test",
        ) as client:
            return await client.get(path, headers=headers)


def test_application_imports_and_starts() -> None:
    response = asyncio.run(_get_with_lifespan("/health/live"))

    assert response.status_code == 200


def test_health_live() -> None:
    response = asyncio.run(_get_with_lifespan("/health/live"))

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_health_ready_and_ortools_runtime() -> None:
    response = asyncio.run(_get_with_lifespan("/health/ready", API_KEY_HEADERS))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["application"] == "ready"
    assert body["checks"]["ortools"]


def test_health_live_is_public_but_ready_requires_api_key() -> None:
    live = asyncio.run(_get_with_lifespan("/health/live"))
    assert live.status_code == 200

    missing = asyncio.run(_get_with_lifespan("/health/ready"))
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "invalid_api_key"

    invalid = asyncio.run(
        _get_with_lifespan("/health/ready", {"X-CCSync-Key": "wrong"})
    )
    assert invalid.status_code == 401


def test_health_ready_fails_closed_when_api_key_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SCHEDULER_API_KEY")

    response = asyncio.run(_get_with_lifespan("/health/ready", API_KEY_HEADERS))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "scheduler_auth_not_configured"
