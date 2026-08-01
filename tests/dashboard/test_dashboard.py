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
from context_layer.consent import (
    MAX_SAMPLE_MEMORIES,
    RESERVED_OWNER_SLUG,
    DiscoveryFailed,
    ProposalHolder,
    ScopeProposal,
    ScopeRegistry,
    ScopeSummary,
    SummaryFailed,
    SummaryHolder,
    SweepStatus,
)
from context_layer.curation import TriageStatus
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


def test_page_offers_a_scope_filter_over_the_tags_it_already_carries(
    store, registry, capability_app
):
    """Narrowing the list to a set of scopes is client-side over the data
    block, so this layer can only check that the control and its predicate
    are on the page and that the tags they read are in the payload — what a
    click does is not observable from here.
    """
    registry.register(
        config.DEFAULT_USER_ID,
        owner_type="user",
        owner_slug=RESERVED_OWNER_SLUG,
        scopes=[("dietary", "food preferences")],
    )
    store.all.return_value = [
        {
            "id": "m1",
            "memory": "allergic to peanuts",
            "created_at": "2026-07-01T00:00:00Z",
            "metadata": {"cs_dietary__user": "llm"},
        }
    ]

    page = _client(capability_app).get("/dashboard").text

    assert '<div id="filter"></div>' in page
    assert "matchesScopes(r)" in page
    # "Nothing has claimed this yet" is a bucket you can filter for, and it
    # is not a scope — so it can't come from the registry payload.
    assert '"Untagged"' in page
    # Keyed by SCOPE key, not the cs_ metadata key — which is what lets the
    # filter match a memory's tags straight against the registry payload.
    assert _page_data(page)["memories"][0]["tags"] == {"dietary__user": "llm"}


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


# --- hiding memory details for screen sharing -----------------------------


def test_page_offers_a_hide_toggle_for_every_memory_and_for_one(capability_app):
    """Two eyes: the header one masks every memory at once (what you press
    before recording), the per-card one reveals a single memory to demo.

    The per-card eye exists only once the page's script has run, so all this
    layer can check is that the control is built per row rather than merely
    defined. What it actually does when clicked is not observable from here.
    """
    page = _client(capability_app).get("/dashboard").text

    assert '<button id="hide-all" type="button">' in page
    assert "eyeButton(r)" in page


def test_hide_state_lives_in_the_browser_and_the_full_text_still_ships(
    store, capability_app
):
    """Masking is a display setting, not a consent one: it is kept in
    localStorage, the server is never told, and the text it hides is still in
    the data block. It defends against a camera, not against the page source.
    """
    page = _client(capability_app).get("/dashboard").text

    assert '"pcl.hide-details"' in page
    assert _page_data(page)["memories"][0]["text"] == "likes dark roast"
    store.update_metadata.assert_not_called()


# --- suggesting scopes from the memories ----------------------------------


@pytest.fixture
def holder(monkeypatch):
    """A fresh proposal holder per test — the real one is process-wide, so
    tests sharing it would see each other's pending candidates."""
    fresh = ProposalHolder()
    monkeypatch.setattr(app_module, "get_proposal_holder", lambda: fresh)
    return fresh


def _install_suggester(monkeypatch, result):
    """Make the batched discovery call return (or raise) `result`, and record
    the rows and scopes it was handed."""
    calls = []

    def fake(rows, scopes):
        calls.append((rows, scopes))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(app_module, "suggest_scopes", fake)
    return calls


def _proposal(name):
    return ScopeProposal(name=name, description=f"about {name}", key=f"{name}__user")


def test_a_run_holds_its_candidates_instead_of_registering_them(
    capability_app, registry, monkeypatch, holder, tagging_on
):
    """Raw model output never becomes registry state on its own — a scope key
    is what a future consent grant gates on."""
    calls = _install_suggester(monkeypatch, [_proposal("travel")])

    resp = _client(capability_app).post(
        "/dashboard/suggest", data={"action": "run"}, headers=_ORIGIN
    )

    assert resp.status_code == 303
    assert [p.name for p in holder.get(config.DEFAULT_USER_ID).proposals] == ["travel"]
    assert registry.all(config.DEFAULT_USER_ID) == []
    assert len(calls) == 1


def test_a_run_hands_discovery_the_memories_and_the_current_vocabulary(
    capability_app, registry, monkeypatch, holder, tagging_on
):
    calls = _install_suggester(monkeypatch, [])
    registry.register(
        config.DEFAULT_USER_ID,
        owner_type="user",
        owner_slug=RESERVED_OWNER_SLUG,
        scopes=[("dietary", "what I eat")],
    )

    _client(capability_app).post(
        "/dashboard/suggest", data={"action": "run"}, headers=_ORIGIN
    )

    rows, scopes = calls[0]
    assert [r["id"] for r in rows] == ["m1", "m2"]
    assert [s.key for s in scopes] == ["dietary__user"]
    # Only the sample is used, so only the sample is read back out of the store.
    assert capability_app.store.all.call_args.args[1] == MAX_SAMPLE_MEMORIES


def test_only_ticked_proposals_are_registered(
    capability_app, registry, monkeypatch, holder, tagging_on
):
    _install_suggester(monkeypatch, None)
    holder.put(
        config.DEFAULT_USER_ID, [_proposal("travel"), _proposal("work")]
    )

    resp = _client(capability_app).post(
        "/dashboard/suggest",
        data={"action": "confirm", "key": ["travel__user"]},
        headers=_ORIGIN,
    )

    assert resp.status_code == 303
    registered = registry.all(config.DEFAULT_USER_ID)
    assert [(s.key, s.owner_type, s.description) for s in registered] == [
        ("travel__user", "user", "about travel")
    ]


def test_confirming_nothing_registers_nothing_and_spends_the_list(
    capability_app, registry, monkeypatch, holder, tagging_on
):
    """Ticking none of them is a real answer — "no thanks" — not a reason to
    keep offering the same candidates."""
    _install_suggester(monkeypatch, None)
    holder.put(config.DEFAULT_USER_ID, [_proposal("travel")])

    _client(capability_app).post(
        "/dashboard/suggest", data={"action": "confirm"}, headers=_ORIGIN
    )

    assert registry.all(config.DEFAULT_USER_ID) == []
    assert holder.get(config.DEFAULT_USER_ID).proposals == ()


def test_confirm_skips_a_key_registered_since_the_proposal(
    capability_app, registry, monkeypatch, holder, tagging_on
):
    """register() upserts, so confirming a stale proposal would overwrite the
    description that landed under that key in the meantime."""
    _install_suggester(monkeypatch, None)
    holder.put(config.DEFAULT_USER_ID, [_proposal("travel")])
    registry.register(
        config.DEFAULT_USER_ID,
        owner_type="user",
        owner_slug=RESERVED_OWNER_SLUG,
        scopes=[("travel", "written by hand")],
    )

    _client(capability_app).post(
        "/dashboard/suggest",
        data={"action": "confirm", "key": ["travel__user"]},
        headers=_ORIGIN,
    )

    [scope] = registry.all(config.DEFAULT_USER_ID)
    assert scope.description == "written by hand"


def test_a_resubmitted_checklist_cannot_register_twice(
    capability_app, registry, monkeypatch, holder, tagging_on
):
    _install_suggester(monkeypatch, None)
    holder.put(config.DEFAULT_USER_ID, [_proposal("travel")])
    client = _client(capability_app)
    body = {"action": "confirm", "key": ["travel__user"]}

    client.post("/dashboard/suggest", data=body, headers=_ORIGIN)
    registry.delete(config.DEFAULT_USER_ID, "travel__user")
    client.post("/dashboard/suggest", data=body, headers=_ORIGIN)

    assert registry.all(config.DEFAULT_USER_ID) == []


def test_suggesting_is_refused_when_no_model_may_be_called(
    capability_app, monkeypatch, holder
):
    monkeypatch.setattr(app_module, "classifier_enabled", lambda: False)
    calls = _install_suggester(monkeypatch, [_proposal("travel")])

    resp = _client(capability_app).post(
        "/dashboard/suggest", data={"action": "run"}, headers=_ORIGIN
    )

    assert resp.status_code == 409
    assert calls == []


def test_confirming_does_not_need_a_model(
    capability_app, registry, monkeypatch, holder
):
    """Only `run` calls out. Refusing a confirm because no model may be called
    would strand a checklist the user is looking at, over an action that sends
    nothing anywhere."""
    monkeypatch.setattr(app_module, "classifier_enabled", lambda: False)
    holder.put(config.DEFAULT_USER_ID, [_proposal("travel")])

    resp = _client(capability_app).post(
        "/dashboard/suggest",
        data={"action": "confirm", "key": ["travel__user"]},
        headers=_ORIGIN,
    )

    assert resp.status_code == 303
    assert [s.key for s in registry.all(config.DEFAULT_USER_ID)] == ["travel__user"]


def test_a_failed_suggestion_call_says_so_rather_than_proposing_nothing(
    capability_app, monkeypatch, holder, tagging_on
):
    """An empty list means "no new categories in your memories"; a call that
    never got an answer must not be able to say that."""
    _install_suggester(monkeypatch, DiscoveryFailed("no api key"))

    resp = _client(capability_app).post(
        "/dashboard/suggest", data={"action": "run"}, headers=_ORIGIN
    )

    assert resp.status_code == 502
    assert holder.get(config.DEFAULT_USER_ID).generated_at == ""


def test_an_unknown_suggestion_action_is_400(capability_app, holder, tagging_on):
    resp = _client(capability_app).post(
        "/dashboard/suggest", data={"action": "register"}, headers=_ORIGIN
    )

    assert resp.status_code == 400


def test_cross_origin_suggest_is_rejected(
    capability_app, monkeypatch, holder, tagging_on
):
    calls = _install_suggester(monkeypatch, [_proposal("travel")])

    resp = _client(capability_app).post(
        "/dashboard/suggest",
        data={"action": "run"},
        headers={"origin": "https://evil.example"},
    )

    assert resp.status_code == 403
    assert calls == []


def test_get_on_the_suggest_endpoint_is_405(capability_app):
    assert _client(capability_app).get("/dashboard/suggest").status_code == 405


def test_page_renders_pending_proposals_for_ticking(
    capability_app, monkeypatch, holder, tagging_on
):
    _install_runner(monkeypatch, _FakeRunner())
    holder.put(config.DEFAULT_USER_ID, [_proposal("travel")])

    data = _page_data(_client(capability_app).get("/dashboard").text)

    assert data["suggestions"]["proposals"] == [
        {"name": "travel", "description": "about travel", "key": "travel__user"}
    ]


def test_page_separates_a_run_that_found_nothing_from_never_having_run_one(
    capability_app, monkeypatch, holder, tagging_on
):
    _install_runner(monkeypatch, _FakeRunner())

    before = _page_data(_client(capability_app).get("/dashboard").text)
    holder.put(config.DEFAULT_USER_ID, [])
    after = _page_data(_client(capability_app).get("/dashboard").text)

    assert before["suggestions"]["generated_at"] == ""
    assert after["suggestions"]["generated_at"] != ""


def test_page_offers_suggestion_when_no_scopes_are_registered(
    capability_app, monkeypatch, holder, tagging_on
):
    """The cold start: with an empty vocabulary this is the only control on the
    panel that can do anything."""
    _install_runner(monkeypatch, _FakeRunner())

    resp = _client(capability_app).get("/dashboard")

    assert _page_data(resp.text)["scopes"] == []
    assert "Suggest scopes from my memories" in resp.text


# --- memory triage: the retention endpoint (VC-94) ------------------------


class _FakeTriageRunner:
    """Stands in for the process-wide TriageRunner: records start() calls and
    reports whatever status a test wants the page to render."""

    def __init__(self, started=True, status=None):
        self.started = started
        self._status = status or TriageStatus()
        self.calls = []

    def start(self, store, user_id):
        self.calls.append(user_id)
        return self.started

    def status(self, user_id):
        return self._status


@pytest.fixture
def triage_on(monkeypatch):
    """A server where triage is configured to run."""
    monkeypatch.setattr(app_module, "triage_enabled", lambda: True)


def _install_triage_runner(monkeypatch, runner):
    monkeypatch.setattr(app_module, "get_triage_runner", lambda: runner)
    return runner


def test_triage_sweep_starts_a_background_pass(capability_app, monkeypatch, triage_on):
    runner = _install_triage_runner(monkeypatch, _FakeTriageRunner())

    resp = _client(capability_app).post(
        "/dashboard/retention", data={"action": "sweep"}, headers=_ORIGIN
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "../dashboard"
    assert runner.calls == [config.DEFAULT_USER_ID]


def test_triage_sweep_is_refused_when_no_model_may_be_called(
    capability_app, monkeypatch
):
    monkeypatch.setattr(app_module, "triage_enabled", lambda: False)
    runner = _install_triage_runner(monkeypatch, _FakeTriageRunner())

    resp = _client(capability_app).post(
        "/dashboard/retention", data={"action": "sweep"}, headers=_ORIGIN
    )

    assert resp.status_code == 409
    assert runner.calls == []


def test_a_triage_pass_already_running_is_logged_not_an_error(
    capability_app, monkeypatch, caplog, triage_on
):
    _install_triage_runner(monkeypatch, _FakeTriageRunner(started=False))
    caplog.set_level(logging.INFO, logger="context_layer.access")

    resp = _client(capability_app).post(
        "/dashboard/retention", data={"action": "sweep"}, headers=_ORIGIN
    )

    assert resp.status_code == 303
    [record] = [r for r in caplog.records if "dashboard_action" in r.getMessage()]
    assert json.loads(record.getMessage())["action"] == "retention_sweep_busy"


def test_setting_a_memory_aside_writes_user_provenance(store, capability_app):
    """What the user did by hand has to be recognizable as theirs, or the next
    pass would treat it as its own work and re-decide it."""
    resp = _client(capability_app).post(
        "/dashboard/retention",
        data={"action": "archive", "memory_id": "m1"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 303
    memory_id, updates, user_id = store.update_metadata.call_args[0]
    assert (memory_id, user_id) == ("m1", config.DEFAULT_USER_ID)
    assert updates["retention_state"] == "archived"
    assert updates["retention_source"] == "user"


def test_keeping_a_memory_writes_the_kept_state_and_clears_the_reason(
    store, capability_app
):
    resp = _client(capability_app).post(
        "/dashboard/retention",
        data={"action": "keep", "memory_id": "m1"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 303
    updates = store.update_metadata.call_args[0][1]
    assert updates["retention_state"] == "keep"
    assert updates["retention_reason"] == ""


def test_setting_a_memory_aside_does_not_need_a_model(
    store, capability_app, monkeypatch
):
    """Only the automatic pass calls out; refusing the manual control on a
    server with no model would leave the user unable to tidy their own store."""
    monkeypatch.setattr(app_module, "triage_enabled", lambda: False)

    resp = _client(capability_app).post(
        "/dashboard/retention",
        data={"action": "archive", "memory_id": "m1"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 303
    assert store.update_metadata.called


def test_retention_write_on_anothers_memory_is_404_not_an_error_leak(
    store, capability_app
):
    store.update_metadata.side_effect = TenantIsolationError("not yours")

    resp = _client(capability_app).post(
        "/dashboard/retention",
        data={"action": "archive", "memory_id": "someone-elses"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 404


def test_retention_write_on_an_absent_memory_is_404(store, capability_app):
    store.update_metadata.return_value = {"updated": False, "reason": "not_found"}

    resp = _client(capability_app).post(
        "/dashboard/retention",
        data={"action": "archive", "memory_id": "gone"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 404


def test_retention_action_must_be_one_of_the_three(store, capability_app):
    resp = _client(capability_app).post(
        "/dashboard/retention",
        data={"action": "delete", "memory_id": "m1"},
        headers=_ORIGIN,
    )

    assert resp.status_code == 400
    assert not store.update_metadata.called


def test_retention_write_without_a_memory_id_is_400(store, capability_app):
    resp = _client(capability_app).post(
        "/dashboard/retention", data={"action": "archive"}, headers=_ORIGIN
    )

    assert resp.status_code == 400
    assert not store.update_metadata.called


def test_cross_origin_retention_write_is_rejected(store, capability_app):
    resp = _client(capability_app).post(
        "/dashboard/retention",
        data={"action": "archive", "memory_id": "m1"},
        headers={"origin": "https://evil.example"},
    )

    assert resp.status_code == 403
    assert not store.update_metadata.called


def test_get_on_the_retention_endpoint_is_405(capability_app):
    assert _client(capability_app).get("/dashboard/retention").status_code == 405


def test_page_renders_the_triage_status(capability_app, monkeypatch, triage_on):
    _install_triage_runner(
        monkeypatch,
        _FakeTriageRunner(status=TriageStatus(state="running", total=7, processed=3)),
    )

    resp = _client(capability_app).get("/dashboard")

    data = _page_data(resp.text)
    assert data["triage_enabled"] is True
    assert data["triage"]["state"] == "running"
    assert (data["triage"]["total"], data["triage"]["processed"]) == (7, 3)


def test_page_reports_when_automatic_triage_is_off(capability_app, monkeypatch):
    monkeypatch.setattr(app_module, "triage_enabled", lambda: False)

    resp = _client(capability_app).get("/dashboard")

    assert _page_data(resp.text)["triage_enabled"] is False
    assert "Automatic review is off on this server" in resp.text


def test_page_carries_each_memorys_retention_state_and_reason(store, capability_app):
    """An archived memory stays on this page — it is the only place the user
    can see what was set aside, and why."""
    store.all.return_value = [
        {"id": "m1", "memory": "likes dark roast"},
        {
            "id": "m2",
            "memory": "the file was at /tmp/x",
            "metadata": {
                "retention_state": "archived",
                "retention_source": "llm",
                "retention_reason": "one-off task detail",
            },
        },
    ]

    resp = _client(capability_app).get("/dashboard")

    kept, archived = _page_data(resp.text)["memories"]
    assert kept["retention"] == {"state": "keep", "by_user": False, "reason": ""}
    assert archived["retention"] == {
        "state": "archived",
        "by_user": False,
        "reason": "one-off task detail",
    }


# --- approving scopes fills them (VC-97) ----------------------------------


def test_approving_scopes_starts_the_tagging_sweep(
    capability_app, registry, monkeypatch, holder, tagging_on
):
    """Registering a scope tags nothing by itself, so a vocabulary the user
    just approved would otherwise arrive empty."""
    _install_suggester(monkeypatch, None)
    runner = _install_runner(monkeypatch, _FakeRunner())
    holder.put(config.DEFAULT_USER_ID, [_proposal("travel")])

    resp = _client(capability_app).post(
        "/dashboard/suggest",
        data={"action": "confirm", "key": ["travel__user"]},
        headers=_ORIGIN,
    )

    assert resp.status_code == 303
    assert runner.calls == [config.DEFAULT_USER_ID]


def test_approving_nothing_starts_no_sweep(
    capability_app, monkeypatch, holder, tagging_on
):
    """A confirm that registered no new scope has nothing to re-derive, and a
    sweep is one model call per memory."""
    _install_suggester(monkeypatch, None)
    runner = _install_runner(monkeypatch, _FakeRunner())
    holder.put(config.DEFAULT_USER_ID, [_proposal("travel")])

    resp = _client(capability_app).post(
        "/dashboard/suggest", data={"action": "confirm"}, headers=_ORIGIN
    )

    assert resp.status_code == 303
    assert runner.calls == []


def test_approving_a_scope_registered_since_the_proposal_starts_no_sweep(
    capability_app, registry, monkeypatch, holder, tagging_on
):
    """The collision check already drops it, so nothing changed for tags either."""
    _install_suggester(monkeypatch, None)
    runner = _install_runner(monkeypatch, _FakeRunner())
    registry.register(
        config.DEFAULT_USER_ID, owner_type="user", owner_slug=RESERVED_OWNER_SLUG,
        scopes=[("travel", "mine, written by hand")],
    )
    holder.put(config.DEFAULT_USER_ID, [_proposal("travel")])

    resp = _client(capability_app).post(
        "/dashboard/suggest",
        data={"action": "confirm", "key": ["travel__user"]},
        headers=_ORIGIN,
    )

    assert resp.status_code == 303
    assert runner.calls == []


def test_approving_scopes_without_a_model_registers_but_starts_nothing(
    capability_app, registry, monkeypatch, holder
):
    """Confirm must still work where no model may be called — it just has no
    classifier to fill the scopes with."""
    monkeypatch.setattr(app_module, "classifier_enabled", lambda: False)
    runner = _install_runner(monkeypatch, _FakeRunner())
    holder.put(config.DEFAULT_USER_ID, [_proposal("travel")])

    resp = _client(capability_app).post(
        "/dashboard/suggest",
        data={"action": "confirm", "key": ["travel__user"]},
        headers=_ORIGIN,
    )

    assert resp.status_code == 303
    assert [s.key for s in registry.all(config.DEFAULT_USER_ID)] == ["travel__user"]
    assert runner.calls == []


def test_approving_scopes_while_a_sweep_runs_is_not_an_error(
    capability_app, registry, monkeypatch, holder, caplog, tagging_on
):
    """That pass started against the older vocabulary, so the scopes may go
    untagged — the re-tag button is still there, and a refused start must not
    turn approving them into a failure. It is logged as busy either way, so
    "the scopes I approved are still empty" has an answer in the log."""
    _install_suggester(monkeypatch, None)
    _install_runner(monkeypatch, _FakeRunner(started=False))
    holder.put(config.DEFAULT_USER_ID, [_proposal("travel")])
    caplog.set_level(logging.INFO, logger="context_layer.access")

    resp = _client(capability_app).post(
        "/dashboard/suggest",
        data={"action": "confirm", "key": ["travel__user"]},
        headers=_ORIGIN,
    )

    assert resp.status_code == 303
    assert [s.key for s in registry.all(config.DEFAULT_USER_ID)] == ["travel__user"]
    actions = [
        json.loads(r.getMessage())["action"]
        for r in caplog.records
        if "dashboard_action" in r.getMessage()
    ]
    assert actions == ["sweep_busy", "suggest_confirm"]


# --- the map view and its scope summaries ---------------------------------


@pytest.fixture
def summaries(monkeypatch):
    """A fresh summary holder per test — the real one is process-wide, so
    tests sharing it would see each other's summaries."""
    fresh = SummaryHolder()
    monkeypatch.setattr(app_module, "get_summary_holder", lambda: fresh)
    return fresh


def _install_summarizer(monkeypatch, result):
    """Make the batched summary call return (or raise) `result`, and record
    the rows and scopes it was handed."""
    calls = []

    def fake(rows, scopes):
        calls.append((rows, scopes))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(app_module, "summarize_scopes", fake)
    return calls


def test_summarizing_holds_the_result_for_the_page(
    capability_app, monkeypatch, summaries, tagging_on
):
    calls = _install_summarizer(
        monkeypatch, [ScopeSummary(key="dietary__user", text="Your food notes.")]
    )

    resp = _client(capability_app).post("/dashboard/summarize", headers=_ORIGIN)

    assert resp.status_code == 303
    assert resp.headers["location"] == "../dashboard"
    assert len(calls) == 1
    held = summaries.get(config.DEFAULT_USER_ID)
    assert [(s.key, s.text) for s in held.summaries] == [
        ("dietary__user", "Your food notes.")
    ]


def test_summarizing_is_refused_when_no_model_may_be_called(
    capability_app, monkeypatch, summaries
):
    """Outside EXTRACTION_MODE=anthropic nothing is sent to a model, so the
    endpoint says so rather than holding an empty result that reads as "your
    scopes have nothing in them"."""
    monkeypatch.setattr(app_module, "classifier_enabled", lambda: False)
    calls = _install_summarizer(monkeypatch, [])

    resp = _client(capability_app).post("/dashboard/summarize", headers=_ORIGIN)

    assert resp.status_code == 409
    assert calls == []
    assert summaries.get(config.DEFAULT_USER_ID).generated_at == ""


def test_a_failed_summary_call_says_so_rather_than_holding_nothing(
    capability_app, monkeypatch, summaries, tagging_on
):
    _install_summarizer(monkeypatch, SummaryFailed("no api key"))

    resp = _client(capability_app).post("/dashboard/summarize", headers=_ORIGIN)

    assert resp.status_code == 502
    assert summaries.get(config.DEFAULT_USER_ID).generated_at == ""


def test_re_summarizing_replaces_the_previous_answer(
    capability_app, monkeypatch, summaries, tagging_on
):
    _install_summarizer(monkeypatch, [ScopeSummary(key="b__user", text="second")])
    summaries.put(config.DEFAULT_USER_ID, [ScopeSummary(key="a__user", text="first")])

    _client(capability_app).post("/dashboard/summarize", headers=_ORIGIN)

    held = summaries.get(config.DEFAULT_USER_ID)
    assert [s.key for s in held.summaries] == ["b__user"]


def test_cross_origin_summarize_is_rejected(
    capability_app, monkeypatch, summaries, tagging_on
):
    calls = _install_summarizer(monkeypatch, [])

    resp = _client(capability_app).post(
        "/dashboard/summarize", headers={"origin": "https://evil.example"}
    )

    assert resp.status_code == 403
    assert calls == []


def test_get_on_the_summarize_endpoint_is_405(capability_app):
    assert _client(capability_app).get("/dashboard/summarize").status_code == 405


def test_page_carries_the_held_summaries(capability_app, summaries, tagging_on):
    summaries.put(
        config.DEFAULT_USER_ID,
        [ScopeSummary(key="dietary__user", text="Your food notes.")],
    )

    data = _page_data(_client(capability_app).get("/dashboard").text)

    assert data["summaries"]["summaries"] == [
        {"key": "dietary__user", "text": "Your food notes."}
    ]
    assert data["summaries"]["generated_at"]


def test_page_separates_never_summarized_from_summarized_nothing(
    capability_app, summaries, tagging_on
):
    """Both render as an empty list; only one of them means "press the button"."""
    before = _page_data(_client(capability_app).get("/dashboard").text)
    summaries.put(config.DEFAULT_USER_ID, [])
    after = _page_data(_client(capability_app).get("/dashboard").text)

    assert before["summaries"]["generated_at"] == ""
    assert after["summaries"]["generated_at"] != ""


def test_page_ships_the_map_as_a_third_tab(capability_app):
    """The graph is built client-side from the data block, so this layer can
    only check that the view and its controls are on the page — what a click
    draws is not observable from here.

    The map is a tab on the existing bar rather than a second view switcher,
    so what ships is a `map` entry in the tab list, not a control of its own.
    """
    page = _client(capability_app).get("/dashboard").text

    assert 'const TABS = ["active", "archived", "map"];' in page
    assert '<svg id="graph"' in page
    assert "function renderGraph(" in page
    # Untagged memories are a node too: a map that dropped them would
    # understate how much the store holds.
    assert 'label: "Untagged"' in page


def test_a_summary_cannot_break_out_of_the_data_block(capability_app, summaries):
    """Summaries are model output riding the same JSON block as memory text —
    same escape guarantees required."""
    summaries.put(
        config.DEFAULT_USER_ID,
        [ScopeSummary(key="x__user", text='</script><img src=x onerror=alert(1)>')],
    )

    page = _client(capability_app).get("/dashboard").text

    assert "</script><img" not in page
    assert _page_data(page)["summaries"]["summaries"][0]["text"] == (
        '</script><img src=x onerror=alert(1)>'
    )
