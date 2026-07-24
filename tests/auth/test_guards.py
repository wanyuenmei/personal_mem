"""CapabilityPathGuard routing: which external paths reach the inner app."""

import pytest
from starlette.testclient import TestClient

from context_layer.auth import CapabilityPathGuard


async def _echo_app(scope, receive, send):
    """Reply with the (rewritten) path and the stamped client label."""
    body = f"{scope['path']}|{scope.get('state', {}).get('client', '')}".encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": body})


@pytest.fixture
def client():
    guard = CapabilityPathGuard(_echo_app, {"sekrit": "claude"}, rpm=1000)
    return TestClient(guard)


@pytest.mark.parametrize("suffix", ["/mcp", "/dashboard", "/dashboard/logout"])
def test_token_prefixed_paths_are_rewritten_and_labeled(client, suffix):
    resp = client.get(f"/sekrit{suffix}")

    assert resp.status_code == 200
    assert resp.text == f"{suffix}|claude"


@pytest.mark.parametrize("path", ["/mcp", "/dashboard", "/wrong/dashboard", "/sekrit/other"])
def test_everything_else_is_404(client, path):
    assert client.get(path).status_code == 404
