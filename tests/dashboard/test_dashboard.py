"""Dashboard tests: auth mode behavior, tenant scoping, safe rendering, and
the mutating endpoints (scope create/delete, per-memory tag add/remove).

The store is a stub (.all for the page, .update_metadata for tag writes), the
WorkOS client is a stub injected through DashboardApp's workos_client seam,
and the scope registry is a REAL ScopeRegistry over temp SQLite (its rows are
what the mutation tests assert on), so none of these tests touch a real
memory backend or the network.
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
from context_layer.consent import RESERVED_OWNER_SLUG, ScopeRegistry, SweepStatus
from context_layer.dashboard import DashboardApp
from context_layer.dashboard import app as app_module
from context_layer.memory import TenantIsolationError

# What a browser sends on a same-origin form POST to the TestClient host.
_ORIGIN = {"origin": "http://testserver"}


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
    fake.update_metadata.return_value = {"updated": True, "id": "m1"}
    return fake


@pytest.fixture
def registry(tmp_path):
    return ScopeRegistry(sqlite_path=str(tmp_path / "consent.db"))


@pytest.fixture
def capability_app(store, registry):
    return DashboardApp(_inner_app, store, oauth_mode=False, registry=registry)


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


# --- the scopes panel on the page ----------------------------------------


def test_page_embeds_registered_scopes_and_memory_tags(store, registry, capability_app):
    registry.register(
        config.DEFAULT_USER_ID,
        owner_type="third_party",
        owner_slug="tastebuds",
        scopes=[("dietary", "food preferences")],
    )
    store.all.return_value = [
        {
            "id": "m1",
            "memory": "likes dark roast",
            "created_at": "2026-07-01T00:00:00Z",
            "metadata": {"cs_dietary__tastebuds": "user"},
        }
    ]

    resp = _client(capability_app).get("/dashboard")

    assert resp.status_code == 200
    assert "dietary__tastebuds" in resp.text
    assert "food preferences" in resp.text


def test_removed_tags_read_as_untagged_in_the_payload(store, capability_app):
    store.all.return_value = [
        {
            "id": "m1",
            "memory": "x",
            "created_at": "",
            "metadata": {"cs_health__user": "user_removed"},
        }
    ]

    resp = _client(capability_app).get("/dashboard")

    assert '"tags": {}' in resp.text


def test_scope_description_cannot_break_out_of_the_data_block(
    store, registry, capability_app
):
    """Scope names/descriptions are third-party-supplied text and ride the
    same JSON data block as memory text — same escape guarantees required."""
    registry.register(
        config.DEFAULT_USER_ID,
        owner_type="third_party",
        owner_slug="evil",
        scopes=[("dietary", "</script><script>alert(1)</script>")],
    )

    resp = _client(capability_app).get("/dashboard")

    assert resp.text.count("</script>") == 2


# --- mutations: scope create/delete ---------------------------------------


def test_create_scope_registers_under_the_reserved_owner(registry, capability_app):
    resp = _client(capability_app).post(
        "/dashboard/scopes",
        data={"action": "create", "name": "Journaling", "description": "private notes"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "../dashboard"
    [scope] = registry.all(config.DEFAULT_USER_ID)
    assert scope.key == "journaling__user"
    assert scope.owner_type == "user"
    assert scope.owner_name == RESERVED_OWNER_SLUG
    assert scope.description == "private notes"


def test_create_scope_rejects_an_unsluggable_name(registry, capability_app):
    resp = _client(capability_app).post(
        "/dashboard/scopes",
        data={"action": "create", "name": "!!!"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 400
    assert registry.all(config.DEFAULT_USER_ID) == []


def test_delete_scope_removes_only_your_own(registry, capability_app):
    registry.register(
        config.DEFAULT_USER_ID,
        owner_type="user",
        owner_slug=RESERVED_OWNER_SLUG,
        scopes=[("journaling", "")],
    )

    resp = _client(capability_app).post(
        "/dashboard/scopes",
        data={"action": "delete", "key": "journaling__user"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 303
    assert registry.all(config.DEFAULT_USER_ID) == []


def test_delete_scope_refuses_a_third_partys_scope(registry, capability_app):
    registry.register(
        config.DEFAULT_USER_ID,
        owner_type="third_party",
        owner_slug="tastebuds",
        scopes=[("dietary", "")],
    )

    resp = _client(capability_app).post(
        "/dashboard/scopes",
        data={"action": "delete", "key": "dietary__tastebuds"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 403
    assert len(registry.all(config.DEFAULT_USER_ID)) == 1


def test_delete_of_unknown_scope_is_404(capability_app):
    resp = _client(capability_app).post(
        "/dashboard/scopes",
        data={"action": "delete", "key": "nope__user"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 404


# --- mutations: tag add/remove --------------------------------------------


def _register_dietary(registry, user_id=None):
    registry.register(
        user_id or config.DEFAULT_USER_ID,
        owner_type="third_party",
        owner_slug="tastebuds",
        scopes=[("dietary", "")],
    )


def test_tag_add_writes_user_provenance(store, registry, capability_app):
    _register_dietary(registry)

    resp = _client(capability_app).post(
        "/dashboard/tags",
        data={"action": "add", "memory_id": "m1", "scope_key": "dietary__tastebuds"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "../dashboard"
    store.update_metadata.assert_called_once_with(
        "m1", {"cs_dietary__tastebuds": "user"}, config.DEFAULT_USER_ID
    )


def test_tag_remove_writes_the_user_removed_tombstone(store, registry, capability_app):
    """Removal is a tombstone, not a key deletion: mem0's update merges
    metadata (keys can't be removed through it), and the tombstone is what
    stops the classifier from re-applying a vetoed tag."""
    _register_dietary(registry)

    resp = _client(capability_app).post(
        "/dashboard/tags",
        data={"action": "remove", "memory_id": "m1", "scope_key": "dietary__tastebuds"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 303
    store.update_metadata.assert_called_once_with(
        "m1", {"cs_dietary__tastebuds": "user_removed"}, config.DEFAULT_USER_ID
    )


def test_tagging_with_an_unregistered_scope_is_404(store, capability_app):
    resp = _client(capability_app).post(
        "/dashboard/tags",
        data={"action": "add", "memory_id": "m1", "scope_key": "nope__nobody"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 404
    store.update_metadata.assert_not_called()


def test_tagging_anothers_memory_is_404_not_an_error_leak(
    store, registry, capability_app
):
    """The store's tenant guard raises on a foreign memory id; the browser
    surface must translate that to the same 404 an absent id gets."""
    _register_dietary(registry)
    store.update_metadata.side_effect = TenantIsolationError("cross-tenant")

    resp = _client(capability_app).post(
        "/dashboard/tags",
        data={"action": "add", "memory_id": "m9", "scope_key": "dietary__tastebuds"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 404
    assert "cross-tenant" not in resp.text


def test_tagging_an_absent_memory_is_404(store, registry, capability_app):
    _register_dietary(registry)
    store.update_metadata.return_value = {
        "updated": False, "id": "gone", "reason": "not_found",
    }

    resp = _client(capability_app).post(
        "/dashboard/tags",
        data={"action": "add", "memory_id": "gone", "scope_key": "dietary__tastebuds"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 404


def test_tag_action_must_be_add_or_remove(store, registry, capability_app):
    _register_dietary(registry)

    resp = _client(capability_app).post(
        "/dashboard/tags",
        data={"action": "purge", "memory_id": "m1", "scope_key": "dietary__tastebuds"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 400
    store.update_metadata.assert_not_called()


# --- mutation guards -------------------------------------------------------


def test_posts_without_origin_or_referer_are_rejected(store, registry, capability_app):
    _register_dietary(registry)

    resp = _client(capability_app).post(
        "/dashboard/tags",
        data={"action": "add", "memory_id": "m1", "scope_key": "dietary__tastebuds"},
    )

    assert resp.status_code == 403
    store.update_metadata.assert_not_called()


def test_cross_origin_posts_are_rejected(store, registry, capability_app):
    _register_dietary(registry)

    resp = _client(capability_app).post(
        "/dashboard/tags",
        data={"action": "add", "memory_id": "m1", "scope_key": "dietary__tastebuds"},
        headers={"origin": "https://evil.example"},
    )

    assert resp.status_code == 403
    store.update_metadata.assert_not_called()


def test_same_origin_referer_passes_when_origin_is_absent(
    store, registry, capability_app
):
    _register_dietary(registry)

    resp = _client(capability_app).post(
        "/dashboard/tags",
        data={"action": "add", "memory_id": "m1", "scope_key": "dietary__tastebuds"},
        headers={"referer": "http://testserver/tok123/dashboard"},
    )

    assert resp.status_code == 303


def test_get_on_a_mutation_endpoint_is_405(capability_app):
    assert _client(capability_app).get("/dashboard/tags").status_code == 405
    assert _client(capability_app).get("/dashboard/scopes").status_code == 405


def test_oauth_post_without_a_session_is_403(store, registry, oauth_config):
    """A POST can't bounce through the sign-in flow, so an expired/absent
    session refuses rather than redirects — and nothing is written."""
    app = DashboardApp(
        _inner_app, store, oauth_mode=True,
        workos_client=MagicMock(), registry=registry,
    )

    resp = _client(app).post(
        "/dashboard/scopes",
        data={"action": "create", "name": "journaling"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 403
    assert registry.all(config.DEFAULT_USER_ID) == []


def test_oauth_post_writes_under_the_signed_in_namespace(
    store, registry, oauth_config
):
    workos = MagicMock()
    session = workos.user_management.load_sealed_session.return_value
    session.authenticate.return_value = SimpleNamespace(
        authenticated=True,
        user={"id": "user_123", "email": "mei@example.com"},
    )
    app = DashboardApp(
        _inner_app, store, oauth_mode=True, workos_client=workos, registry=registry,
    )
    client = _client(app)
    client.cookies.set("wos_session", "sealed-blob")

    resp = client.post(
        "/dashboard/scopes",
        data={"action": "create", "name": "journaling"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 303
    user_id = f"{config.WORKOS_USER_ID_PREFIX}user_123"
    assert [s.key for s in registry.all(user_id)] == ["journaling__user"]


def test_mutations_log_a_dashboard_action(store, registry, capability_app, caplog):
    _register_dietary(registry)
    caplog.set_level(logging.INFO, logger="context_layer.access")

    _client(capability_app).post(
        "/dashboard/tags",
        data={"action": "add", "memory_id": "m1", "scope_key": "dietary__tastebuds"},
        headers=_ORIGIN,
    )

    [record] = [r for r in caplog.records if "dashboard_action" in r.getMessage()]
    logged = json.loads(record.getMessage())
    assert logged["action"] == "tags_add"
    assert logged["user"] == config.DEFAULT_USER_ID


# --- the classifier sweep -------------------------------------------------


def _page_data(page: str) -> dict:
    """The JSON block the page hands to its client-side rendering. Safe to
    slice on "</script>": the embed escapes every "/", so the first one after
    the block is the real closing tag."""
    start = page.index('id="data">') + len('id="data">')
    return json.loads(page[start : page.index("</script>", start)])


class _FakeRunner:
    """Stands in for the process-wide SweepRunner: records start() calls and
    reports whatever status a test wants the page to render."""

    def __init__(self, started=True, status=None):
        self.started = started
        self._status = status or SweepStatus()
        self.calls = []

    def start(self, store, registry, user_id):
        self.calls.append(user_id)
        return self.started

    def status(self, user_id):
        return self._status


@pytest.fixture
def tagging_on(monkeypatch):
    """A server where the classifier is configured to run."""
    monkeypatch.setattr(app_module, "classifier_enabled", lambda: True)


def _install_runner(monkeypatch, runner):
    monkeypatch.setattr(app_module, "get_sweep_runner", lambda: runner)
    return runner


def test_sweep_starts_a_background_pass(capability_app, monkeypatch, tagging_on):
    runner = _install_runner(monkeypatch, _FakeRunner())

    resp = _client(capability_app).post("/dashboard/sweep", headers=_ORIGIN)

    assert resp.status_code == 303
    assert resp.headers["location"] == "../dashboard"
    assert runner.calls == [config.DEFAULT_USER_ID]


def test_sweep_is_refused_when_the_classifier_is_off(capability_app, monkeypatch):
    """Nothing is sent to a model outside EXTRACTION_MODE=anthropic, so the
    endpoint says so rather than starting a pass that would tag nothing."""
    monkeypatch.setattr(app_module, "classifier_enabled", lambda: False)
    runner = _install_runner(monkeypatch, _FakeRunner())

    resp = _client(capability_app).post("/dashboard/sweep", headers=_ORIGIN)

    assert resp.status_code == 409
    assert runner.calls == []


def test_a_sweep_already_running_is_logged_not_an_error(
    capability_app, monkeypatch, caplog, tagging_on
):
    _install_runner(monkeypatch, _FakeRunner(started=False))
    caplog.set_level(logging.INFO, logger="context_layer.access")

    resp = _client(capability_app).post("/dashboard/sweep", headers=_ORIGIN)

    assert resp.status_code == 303
    [record] = [r for r in caplog.records if "dashboard_action" in r.getMessage()]
    assert json.loads(record.getMessage())["action"] == "sweep_busy"


def test_cross_origin_sweep_is_rejected(capability_app, monkeypatch, tagging_on):
    runner = _install_runner(monkeypatch, _FakeRunner())

    resp = _client(capability_app).post(
        "/dashboard/sweep", headers={"origin": "https://evil.example"}
    )

    assert resp.status_code == 403
    assert runner.calls == []


def test_get_on_the_sweep_endpoint_is_405(capability_app):
    assert _client(capability_app).get("/dashboard/sweep").status_code == 405


def test_page_renders_the_sweep_status(capability_app, monkeypatch, tagging_on):
    _install_runner(
        monkeypatch,
        _FakeRunner(status=SweepStatus(state="running", total=7, processed=3)),
    )

    resp = _client(capability_app).get("/dashboard")

    data = _page_data(resp.text)
    assert data["tagging_enabled"] is True
    assert data["sweep"]["state"] == "running"
    assert (data["sweep"]["total"], data["sweep"]["processed"]) == (7, 3)


def test_page_carries_the_scope_count_a_sweep_ran_with(
    capability_app, monkeypatch, tagging_on
):
    """A stored "0 of 0" means one of two things — no scopes to tag into, or a
    pass that matched nothing — so the count travels with the status."""
    _install_runner(
        monkeypatch,
        _FakeRunner(status=SweepStatus(state="done", scope_count=2, total=5, changed=1)),
    )

    data = _page_data(_client(capability_app).get("/dashboard").text)

    assert data["sweep"]["scope_count"] == 2


def test_page_points_at_scope_creation_when_none_are_registered(
    capability_app, monkeypatch, tagging_on
):
    """With an empty registry a re-tag could only ever be a no-op, so the panel
    leads with the step that unblocks it instead of offering the button."""
    _install_runner(monkeypatch, _FakeRunner())

    resp = _client(capability_app).get("/dashboard")

    assert _page_data(resp.text)["scopes"] == []
    assert "Nothing to tag into yet" in resp.text


def test_page_renders_a_sweep_that_could_not_classify_anything(
    capability_app, monkeypatch, tagging_on
):
    """A sweep where every classification call failed must reach the page as
    an error with a count, not as "0 of 4 memories updated"."""
    _install_runner(
        monkeypatch,
        _FakeRunner(
            status=SweepStatus(
                state="error",
                total=4,
                processed=4,
                failed=4,
                # The exact value renderSweep branches on to explain the
                # likely cause (credentials or model config) to the user.
                error="all_failed",
            )
        ),
    )

    data = _page_data(_client(capability_app).get("/dashboard").text)

    assert data["sweep"]["state"] == "error"
    assert (data["sweep"]["failed"], data["sweep"]["error"]) == (4, "all_failed")


def test_page_reports_when_automatic_tagging_is_off(capability_app, monkeypatch):
    monkeypatch.setattr(app_module, "classifier_enabled", lambda: False)
    _install_runner(monkeypatch, _FakeRunner())

    data = _page_data(_client(capability_app).get("/dashboard").text)

    assert data["tagging_enabled"] is False
