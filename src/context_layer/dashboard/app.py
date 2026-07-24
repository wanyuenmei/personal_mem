"""The memory browser: a read-only page showing everything stored about you.

Sits in front of the MCP app on the HTTP transport and owns every /dashboard*
path; anything else passes straight through. Read-only by design — it renders
store.all() and never mutates (edit/delete actions are PER-56 / PER-41).

Auth mirrors the MCP surface's two modes:

- OAuth mode: browser sign-in via WorkOS AuthKit. /dashboard/login redirects to
  the hosted sign-in page; /dashboard/callback exchanges the code and seals
  access+refresh tokens into an httponly cookie (the SDK's Fernet sealing);
  /dashboard verifies the cookie's access token locally against the tenant JWKS
  on every request and transparently refreshes an expired one. The signed-in
  WorkOS user id gets the same prefix identity.resolve_user_id applies to MCP
  bearer tokens, so the browser shows exactly the namespace connectors write to.
- Capability mode: the guard in front only forwards /dashboard after matching
  /<token>/dashboard, so reaching this app at all IS the auth, and memories come
  from the single-tenant default namespace — same trust model as /<token>/mcp.
  The page must emit no absolute /dashboard/* links in this mode: the browser's
  real paths carry the token prefix the guard stripped.

Starlette Request/Response here is safe with the SSE stream because this app
fully owns its routes and only ever *forwards* everything else — it never wraps
or buffers the MCP app's responses the way a BaseHTTPMiddleware would.
"""

import dataclasses
import hmac
import logging
import secrets
from typing import Any, Optional
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from context_layer import config
from context_layer.dashboard.page import render_page
from context_layer.memory import ContextStore
from context_layer.observability import log_dashboard_view

logger = logging.getLogger("context_layer.dashboard")

SESSION_COOKIE = "wos_session"
STATE_COOKIE = "wos_oauth_state"
# Long-lived cookie; the sealed refresh token inside is what actually expires.
_SESSION_COOKIE_MAX_AGE = 60 * 60 * 24 * 30

_LOGIN_UNCONFIGURED = (
    "Dashboard sign-in is not configured on this deploy. Set WORKOS_API_KEY and "
    "WORKOS_COOKIE_PASSWORD (a Fernet key), and register "
    "<public-url>/dashboard/callback as a redirect URI in the WorkOS dashboard."
)


@dataclasses.dataclass
class _Principal:
    """A signed-in dashboard viewer resolved from the session cookie."""

    user_id: str  # the mem0 namespace (prefixed WorkOS user id)
    label: str  # what the page greets them as (email, falling back to id)
    fresh_cookie: Optional[str] = None  # re-set when the session was refreshed


class DashboardApp:
    """ASGI component owning /dashboard*; forwards everything else to `app`."""

    def __init__(
        self,
        app: Any,
        store: ContextStore,
        *,
        oauth_mode: bool,
        workos_client: Any = None,
    ) -> None:
        self.app = app
        self.store = store
        self.oauth_mode = oauth_mode
        # Injectable for tests; lazily built from config in production so
        # capability-mode deploys never construct a WorkOS client at all.
        self._workos = workos_client

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path == "/dashboard/":  # tolerate the hand-typed trailing slash
            path = "/dashboard"
        if path not in ("/dashboard", "/dashboard/login", "/dashboard/callback",
                        "/dashboard/logout"):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if request.method != "GET":
            response: Response = PlainTextResponse("method not allowed", status_code=405)
        elif path != "/dashboard" and not self.oauth_mode:
            # The auth sub-paths exist only for the AuthKit flow. In capability
            # mode any redirect they issued would point at the bare /dashboard,
            # which 404s once the guard-stripped token prefix is gone from the
            # browser's URL — so 404 them outright, like any other wrong path.
            response = PlainTextResponse("not found", status_code=404)
        elif path == "/dashboard":
            response = await self._view(request)
        elif path == "/dashboard/login":
            response = self._login()
        elif path == "/dashboard/callback":
            response = await self._callback(request)
        else:
            response = await self._logout(request)
        await response(scope, receive, send)

    # --- the page itself ------------------------------------------------

    async def _view(self, request: Request) -> Response:
        if self.oauth_mode:
            principal = await self._authenticate_browser(request)
            if principal is None:
                if not config.dashboard_login_enabled():
                    return PlainTextResponse(_LOGIN_UNCONFIGURED, status_code=503)
                return RedirectResponse("/dashboard/login", status_code=302)
        else:
            # Capability mode: the path guard already authenticated this
            # request, and everything lives in the single-tenant namespace.
            principal = _Principal(
                user_id=config.DEFAULT_USER_ID, label=config.DEFAULT_USER_ID
            )

        try:
            rows = await run_in_threadpool(self.store.all, principal.user_id)
        except Exception:
            logger.exception(
                "dashboard failed to list memories for user=%s", principal.user_id
            )
            return PlainTextResponse(
                "Couldn't load your memories (backend error). Try again shortly.",
                status_code=500,
            )

        # Mirror _client_label's trust order: the capability guard's stamped
        # token label (server-assigned) over the User-Agent (self-asserted).
        client = getattr(request.state, "client", None)
        log_dashboard_view(
            principal.user_id, str(client or request.headers.get("user-agent") or "")
        )
        response: Response = HTMLResponse(
            render_page(rows, user_label=principal.label, show_logout=self.oauth_mode)
        )
        if principal.fresh_cookie:
            self._set_session_cookie(response, principal.fresh_cookie)
        return response

    # --- AuthKit browser session ------------------------------------------

    async def _authenticate_browser(self, request: Request) -> Optional[_Principal]:
        """Resolve the sealed session cookie to a principal, or None.

        authenticate() checks the sealed access token's signature/expiry against
        the tenant JWKS locally; on failure we attempt one refresh (a WorkOS
        call using the sealed refresh token) before giving up, and hand the new
        sealed session back to the caller to re-set the cookie.
        """
        if not config.dashboard_login_enabled():
            return None
        cookie = request.cookies.get(SESSION_COOKIE)
        if not cookie:
            return None
        try:
            session = self._client().user_management.load_sealed_session(
                session_data=cookie, cookie_password=config.WORKOS_COOKIE_PASSWORD
            )
            auth = await run_in_threadpool(session.authenticate)
            fresh_cookie = None
            if not getattr(auth, "authenticated", False):
                refreshed = await run_in_threadpool(session.refresh)
                if not getattr(refreshed, "authenticated", False):
                    return None
                auth, fresh_cookie = refreshed, refreshed.sealed_session
        except Exception:
            logger.exception("dashboard session verification failed")
            return None

        user = getattr(auth, "user", None) or {}
        workos_user_id = str(user.get("id") or "").strip()
        if not workos_user_id:
            return None
        return _Principal(
            user_id=f"{config.WORKOS_USER_ID_PREFIX}{workos_user_id}",
            label=str(user.get("email") or workos_user_id),
            fresh_cookie=fresh_cookie,
        )

    def _login(self) -> Response:
        # Only reachable in OAuth mode; capability mode 404s the auth sub-paths
        # in __call__ (the token path is the credential — there is no login).
        if not config.dashboard_login_enabled():
            return PlainTextResponse(_LOGIN_UNCONFIGURED, status_code=503)
        # `state` round-trips through AuthKit and is checked against this
        # cookie in the callback, so a forged callback can't attach a session
        # the user never initiated (login CSRF).
        state = secrets.token_urlsafe(32)
        url = self._client().user_management.get_authorization_url(
            provider="authkit", redirect_uri=self._redirect_uri(), state=state
        )
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(
            STATE_COOKIE, state, max_age=600, path="/dashboard",
            httponly=True, secure=self._https(), samesite="lax",
        )
        return response

    async def _callback(self, request: Request) -> Response:
        if not config.dashboard_login_enabled():
            return PlainTextResponse("not found", status_code=404)
        code = request.query_params.get("code") or ""
        state = request.query_params.get("state") or ""
        expected = request.cookies.get(STATE_COOKIE) or ""
        if not code or not expected or not hmac.compare_digest(state, expected):
            return PlainTextResponse(
                "Sign-in failed (missing or mismatched state). "
                "Start again at /dashboard.",
                status_code=400,
            )
        try:
            client = self._client()
            auth = await run_in_threadpool(
                lambda: client.user_management.authenticate_with_code(code=code)
            )
            from workos.session import seal_session_from_auth_response

            sealed = seal_session_from_auth_response(
                access_token=auth.access_token,
                refresh_token=auth.refresh_token,
                user=dataclasses.asdict(auth.user),
                impersonator=(
                    dataclasses.asdict(auth.impersonator) if auth.impersonator else None
                ),
                cookie_password=config.WORKOS_COOKIE_PASSWORD,
            )
        except Exception:
            logger.exception("AuthKit code exchange failed")
            return PlainTextResponse(
                "Sign-in failed during the code exchange; check the server logs.",
                status_code=502,
            )
        response = RedirectResponse("/dashboard", status_code=302)
        self._set_session_cookie(response, sealed)
        response.delete_cookie(STATE_COOKIE, path="/dashboard")
        return response

    async def _logout(self, request: Request) -> Response:
        """Clear the local session, ending it at WorkOS too when possible."""
        target = "/dashboard"
        cookie = request.cookies.get(SESSION_COOKIE)
        if config.dashboard_login_enabled() and cookie:
            try:
                session = self._client().user_management.load_sealed_session(
                    session_data=cookie, cookie_password=config.WORKOS_COOKIE_PASSWORD
                )
                auth = await run_in_threadpool(session.authenticate)
                if getattr(auth, "authenticated", False):
                    target = await run_in_threadpool(session.get_logout_url)
            except Exception:
                # Still sign out locally: the cookie deletion below is the part
                # that must not fail.
                logger.exception("could not resolve WorkOS logout URL")
        response = RedirectResponse(target, status_code=302)
        response.delete_cookie(SESSION_COOKIE, path="/dashboard")
        return response

    # --- plumbing ---------------------------------------------------------

    def _client(self) -> Any:
        if self._workos is None:
            from workos import WorkOSClient

            self._workos = WorkOSClient(
                api_key=config.WORKOS_API_KEY,
                client_id=config.WORKOS_CLIENT_ID,
                base_url=config.WORKOS_API_BASE_URL,
            )
        return self._workos

    @staticmethod
    def _public_base() -> str:
        """scheme://host of the deploy, from the MCP resource URL."""
        parts = urlsplit(config.PUBLIC_SERVER_URL)
        return f"{parts.scheme}://{parts.netloc}"

    def _redirect_uri(self) -> str:
        return f"{self._public_base()}/dashboard/callback"

    def _https(self) -> bool:
        return self._public_base().startswith("https://")

    def _set_session_cookie(self, response: Response, sealed: str) -> None:
        # Scoped to /dashboard so the browser never sends it to /mcp, and
        # httponly so page scripts (which render attacker-influenceable memory
        # text) can never read the sealed tokens.
        response.set_cookie(
            SESSION_COOKIE, sealed, max_age=_SESSION_COOKIE_MAX_AGE, path="/dashboard",
            httponly=True, secure=self._https(), samesite="lax",
        )
