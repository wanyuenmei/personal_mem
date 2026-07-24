"""Dashboard tests: auth mode behavior, tenant scoping, and safe rendering.

The store is a plain stub (only .all is used — the page is read-only), and the
WorkOS client is a stub injected through DashboardApp's workos_client seam, so
none of these tests touch a real backend or the network.
"""

import dataclasses
import json
import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient

from context_layer import config
from context_layer.dashboard import DashboardApp


async def _inner_app(scope, receive, send):
    await PlainTextResponse("inner")(scope, receive, send)


def _client(app: DashboardApp) -> TestClient:
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def store():
    fake = MagicMock(name="fake_store")
    fake.all.return_value = [
        {"id": "m1", "memory": "likes dark roast", "created_at": "2026-07-01T00:00:00Z"},
        {"id": "m2", "memory": "lives in Berlin", "created_at": "2026-07-02T00:00:00Z"},
    ]
    return fake


@pytest.fixture
def oauth_config(monkeypatch):
    """Make config look like a fully-configured OAuth deploy with dashboard login."""
    monkeypatch.setattr(config, "WORKOS_CLIENT_ID", "client_x")
    monkeypatch.setattr(config, "WORKOS_AUTHKIT_DOMAIN", "https://x.authkit.app")
    monkeypatch.setattr(config, "PUBLIC_SERVER_URL", "https://ctx.example.com/mcp")
    monkeypatch.setattr(config, "WORKOS_API_KEY", "sk_test_x")
    # A real Fernet key, so the callback test exercises the SDK's actual
    # session sealing rather than a stub of it.
    monkeypatch.setattr(config, "WORKOS_COOKIE_PASSWORD", Fernet.generate_key().decode())


# --- capability mode -----------------------------------------------------


def test_capability_mode_renders_default_users_memories(store):
    app = DashboardApp(_inner_app, store, oauth_mode=False)

    resp = _client(app).get("/dashboard")

    assert resp.status_code == 200
    store.all.assert_called_once_with(config.DEFAULT_USER_ID)
    assert "likes dark roast" in resp.text
    assert "lives in Berlin" in resp.text


def test_capability_mode_page_has_no_absolute_dashboard_links(store):
    """The guard strips /<token> from the path, so any absolute /dashboard/*
    link in the page would escape the token prefix and 404 in the browser."""
    app = DashboardApp(_inner_app, store, oauth_mode=False)

    resp = _client(app).get("/dashboard")

    assert 'href="/dashboard' not in resp.text


def test_trailing_slash_reaches_the_page(store):
    app = DashboardApp(_inner_app, store, oauth_mode=False)

    assert _client(app).get("/dashboard/").status_code == 200


def test_non_dashboard_paths_pass_through(store):
    app = DashboardApp(_inner_app, store, oauth_mode=False)

    resp = _client(app).get("/mcp")

    assert resp.text == "inner"
    store.all.assert_not_called()


def test_non_get_is_rejected(store):
    app = DashboardApp(_inner_app, store, oauth_mode=False)

    assert _client(app).post("/dashboard").status_code == 405


def test_memory_text_cannot_break_out_of_the_data_block(store):
    store.all.return_value = [
        {"id": "m1", "memory": "</script><script>alert(1)</script>", "created_at": ""}
    ]
    app = DashboardApp(_inner_app, store, oauth_mode=False)

    resp = _client(app).get("/dashboard")

    # A <script type="application/json"> block is raw text that only a literal
    # "</script>" can terminate — and the payload's "/" chars are escaped, so
    # the only closing tags in the document are the page's own two (the data
    # block and the renderer). The memory text itself survives, escaped.
    assert resp.text.count("</script>") == 2
    assert "<\\/script><script>alert(1)<\\/script>" in resp.text


def test_store_failure_is_a_friendly_500(store):
    store.all.side_effect = RuntimeError("db down")
    app = DashboardApp(_inner_app, store, oauth_mode=False)

    resp = _client(app).get("/dashboard")

    assert resp.status_code == 500
    assert "db down" not in resp.text


# --- OAuth mode ----------------------------------------------------------


def test_oauth_mode_redirects_anonymous_viewer_to_login(store, oauth_config):
    app = DashboardApp(_inner_app, store, oauth_mode=True)

    resp = _client(app).get("/dashboard")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard/login"


def test_oauth_mode_without_login_config_says_so(store, oauth_config, monkeypatch):
    monkeypatch.setattr(config, "WORKOS_COOKIE_PASSWORD", "")
    app = DashboardApp(_inner_app, store, oauth_mode=True)

    resp = _client(app).get("/dashboard")

    assert resp.status_code == 503
    assert "WORKOS_COOKIE_PASSWORD" in resp.text


def test_login_redirects_to_authkit_and_sets_state_cookie(store, oauth_config):
    workos = MagicMock()
    workos.user_management.get_authorization_url.return_value = (
        "https://x.authkit.app/oauth2/authorize?..."
    )
    app = DashboardApp(_inner_app, store, oauth_mode=True, workos_client=workos)

    resp = _client(app).get("/dashboard/login")

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://x.authkit.app/")
    assert "wos_oauth_state" in resp.cookies
    kwargs = workos.user_management.get_authorization_url.call_args.kwargs
    assert kwargs["provider"] == "authkit"
    assert kwargs["redirect_uri"] == "https://ctx.example.com/dashboard/callback"
    assert kwargs["state"] == resp.cookies["wos_oauth_state"]


def test_callback_rejects_mismatched_state(store, oauth_config):
    app = DashboardApp(_inner_app, store, oauth_mode=True, workos_client=MagicMock())
    client = _client(app)
    client.cookies.set("wos_oauth_state", "expected")

    resp = client.get("/dashboard/callback?code=abc&state=forged")

    assert resp.status_code == 400


def test_signed_in_viewer_sees_their_own_namespace_only(store, oauth_config):
    """The cookie's WorkOS user id must map to the same prefixed mem0 user_id
    the MCP bearer path uses — and only that id is ever passed to the store."""
    workos = MagicMock()
    session = workos.user_management.load_sealed_session.return_value
    session.authenticate.return_value = SimpleNamespace(
        authenticated=True,
        user={"id": "user_123", "email": "mei@example.com"},
    )
    app = DashboardApp(_inner_app, store, oauth_mode=True, workos_client=workos)
    client = _client(app)
    client.cookies.set("wos_session", "sealed-blob")

    resp = client.get("/dashboard")

    assert resp.status_code == 200
    store.all.assert_called_once_with(f"{config.WORKOS_USER_ID_PREFIX}user_123")
    assert "mei@example.com" in resp.text
    assert "Sign out" in resp.text


def test_expired_session_is_refreshed_and_cookie_reset(store, oauth_config):
    workos = MagicMock()
    session = workos.user_management.load_sealed_session.return_value
    session.authenticate.return_value = SimpleNamespace(authenticated=False, reason="invalid_jwt")
    session.refresh.return_value = SimpleNamespace(
        authenticated=True,
        sealed_session="fresh-blob",
        user={"id": "user_123", "email": "mei@example.com"},
    )
    app = DashboardApp(_inner_app, store, oauth_mode=True, workos_client=workos)
    client = _client(app)
    client.cookies.set("wos_session", "stale-blob")

    resp = client.get("/dashboard")

    assert resp.status_code == 200
    assert resp.cookies["wos_session"] == "fresh-blob"


@dataclasses.dataclass
class _FakeUser:
    id: str
    email: str
    # The real workos User model parses timestamps into datetimes, which the
    # SDK's json.dumps-based sealing cannot serialize — a fake with only
    # strings would hide that (and did, in prod).
    created_at: datetime = datetime(2026, 7, 24, 12, 0, 0)


@dataclasses.dataclass
class _FakeAuthResponse:
    access_token: str
    refresh_token: str
    user: _FakeUser
    impersonator: None = None


def test_callback_success_seals_session_cookie_and_redirects(store, oauth_config):
    """The happy-path code exchange: real SDK sealing (real Fernet key), the
    session cookie set, the state cookie cleared, and a redirect to the page."""
    workos = MagicMock()
    workos.user_management.authenticate_with_code.return_value = _FakeAuthResponse(
        access_token="at", refresh_token="rt",
        user=_FakeUser(id="user_123", email="mei@example.com"),
    )
    app = DashboardApp(_inner_app, store, oauth_mode=True, workos_client=workos)
    client = _client(app)
    client.cookies.set("wos_oauth_state", "expected")

    resp = client.get("/dashboard/callback?code=abc&state=expected")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard"
    workos.user_management.authenticate_with_code.assert_called_once_with(code="abc")
    sealed = resp.cookies["wos_session"]
    from workos.session import unseal_data

    unsealed = unseal_data(sealed, config.WORKOS_COOKIE_PASSWORD)
    assert unsealed["access_token"] == "at"
    assert unsealed["user"]["id"] == "user_123"
    assert unsealed["user"]["created_at"] == "2026-07-24 12:00:00"
    state_cookie = [h for h in resp.headers.get_list("set-cookie") if "wos_oauth_state" in h]
    assert state_cookie
    assert '=""' in state_cookie[0] or "Max-Age=0" in state_cookie[0]


def test_logout_redirects_to_workos_and_clears_the_cookie(store, oauth_config):
    workos = MagicMock()
    session = workos.user_management.load_sealed_session.return_value
    session.authenticate.return_value = SimpleNamespace(
        authenticated=True, user={"id": "user_123"}, session_id="sess_1"
    )
    session.get_logout_url.return_value = "https://x.authkit.app/logout?session_id=sess_1"
    app = DashboardApp(_inner_app, store, oauth_mode=True, workos_client=workos)
    client = _client(app)
    client.cookies.set("wos_session", "sealed-blob")

    resp = client.get("/dashboard/logout")

    assert resp.status_code == 302
    assert resp.headers["location"] == "https://x.authkit.app/logout?session_id=sess_1"
    cleared = [h for h in resp.headers.get_list("set-cookie") if "wos_session" in h]
    assert cleared
    assert '=""' in cleared[0] or "Max-Age=0" in cleared[0]


def test_capability_mode_404s_the_auth_subpaths(store):
    """login/logout/callback exist only for the AuthKit flow; under capability
    paths their redirects would escape the token prefix, so they 404 instead."""
    app = DashboardApp(_inner_app, store, oauth_mode=False)
    client = _client(app)

    for path in ("/dashboard/login", "/dashboard/callback", "/dashboard/logout"):
        assert client.get(path).status_code == 404


def test_capability_mode_logs_the_guard_stamped_client_label(store, caplog):
    """The access log must keep per-token attribution: the guard's stamped
    label (request.state.client) wins over the browser's User-Agent."""
    caplog.set_level(logging.INFO, logger="context_layer.access")

    async def stamped(scope, receive, send):
        scope.setdefault("state", {})["client"] = "claude"
        await DashboardApp(_inner_app, store, oauth_mode=False)(scope, receive, send)

    resp = TestClient(stamped).get("/dashboard")

    assert resp.status_code == 200
    assert json.loads(caplog.records[-1].getMessage())["client"] == "claude"


def test_dead_session_redirects_to_login(store, oauth_config):
    workos = MagicMock()
    session = workos.user_management.load_sealed_session.return_value
    session.authenticate.return_value = SimpleNamespace(authenticated=False, reason="invalid_jwt")
    session.refresh.return_value = SimpleNamespace(authenticated=False, reason="refresh_denied")
    app = DashboardApp(_inner_app, store, oauth_mode=True, workos_client=workos)
    client = _client(app)
    client.cookies.set("wos_session", "dead-blob")

    resp = client.get("/dashboard")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard/login"
    store.all.assert_not_called()
