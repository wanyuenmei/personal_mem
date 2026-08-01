"""The memory browser: everything stored about you, plus scope management.

Sits in front of the MCP app on the HTTP transport and owns every /dashboard*
path; anything else passes straight through. Reads render store.all() and the
consent-scope registry; the dashboard is also the consent layer's first
mutating surface — three form-POST endpoints let the user manage scopes and
per-memory tags (memory edit/delete from the browser are PER-56 / PER-41):

- POST /dashboard/scopes — create or delete a scope the user owns (the
  reserved "user" owner). Third parties' registrations are never deletable
  here; their vocabulary is managed by re-registration.
- POST /dashboard/tags — add or remove one consent-scope tag on one memory,
  written into mem0 metadata via the tenant-guarded store.update_metadata.
- POST /dashboard/sweep — re-run the classifier over every memory. Returns as
  soon as the background thread is started; the page renders that thread's
  progress on reload. This is the rebuild path after any vocabulary change,
  and it is user-triggered by design — classification never runs on a page
  view.
- POST /dashboard/suggest — the way out of an empty vocabulary (VC-92). Its
  `run` action asks a model what categories the user's memories fall into and
  holds the candidates; `confirm` registers only the ones the user ticked, as
  the user's own scopes. Two actions rather than one because a scope key is
  what a future consent grant gates on: model output never reaches the
  registry without a person in between.
- POST /dashboard/retention — memory triage (VC-94). `sweep` starts the pass
  that judges every memory on whether it would inform a later decision;
  `keep` and `archive` set one memory's state by hand, which no later pass
  overrules. Nothing here deletes: archived memories move to their own tab on
  the page, and the only thing that changes is that search stops returning
  them.

Mutations are guarded by the same principal resolution as the page plus a
same-origin check (Origin, falling back to Referer, must name the host the
request arrived at) on top of the samesite=lax session cookie — lax already
withholds the cookie from cross-site POSTs; the origin check is the backstop.
Post-mutation redirects are RELATIVE ("../dashboard") so that in capability
mode the browser resolves them against the token-prefixed URL the guard
stripped — an absolute /dashboard would escape the prefix and 404.

Auth mirrors the MCP surface's two modes:

- OAuth mode: browser sign-in via WorkOS AuthKit. /dashboard/login redirects to
  the hosted sign-in page; /dashboard/callback exchanges the code and seals
  access+refresh tokens into an httponly cookie (the SDK's Fernet sealing);
  /dashboard verifies the cookie's access token locally against the tenant JWKS
  on every request and transparently refreshes an expired one. The signed-in
  WorkOS user id gets the same prefix identity.resolve_user_id applies to MCP
  bearer tokens, so the browser shows exactly the namespace connectors write to.
- Capability mode: the guard in front only forwards /dashboard* after matching
  /<token>/dashboard*, so reaching this app at all IS the auth, and memories
  come from the single-tenant default namespace — same trust model as
  /<token>/mcp. The page must emit no absolute /dashboard/* links in this mode:
  the browser's real paths carry the token prefix the guard stripped.

Starlette Request/Response here is safe with the SSE stream because this app
fully owns its routes and only ever *forwards* everything else — it never wraps
or buffers the MCP app's responses the way a BaseHTTPMiddleware would.
"""

import dataclasses
import hmac
import json
import logging
import secrets
from typing import Any, Optional
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from context_layer import config
from context_layer.consent import (
    DESCRIPTION_MAX_CHARS,
    MAX_SAMPLE_MEMORIES,
    PROVENANCE_USER,
    PROVENANCE_USER_REMOVED,
    RESERVED_OWNER_SLUG,
    SLUG_MAX_CHARS,
    DiscoveryFailed,
    ScopeRegistry,
    classifier_enabled,
    get_proposal_holder,
    get_sweep_runner,
    slugify,
    suggest_scopes,
    tag_key,
)
from context_layer.curation import (
    SOURCE_USER,
    STATE_ARCHIVED,
    STATE_KEEP,
    get_triage_runner,
    state_updates,
    triage_enabled,
)
from context_layer.dashboard.page import render_page
from context_layer.memory import ContextStore, TenantIsolationError
from context_layer.observability import log_dashboard_action, log_dashboard_view

logger = logging.getLogger("context_layer.dashboard")

SESSION_COOKIE = "wos_session"
STATE_COOKIE = "wos_oauth_state"
# Long-lived cookie; the sealed refresh token inside is what actually expires.
_SESSION_COOKIE_MAX_AGE = 60 * 60 * 24 * 30

# Where a finished mutation sends the browser: resolved RELATIVE to the POST
# URL (/…/dashboard/scopes or /…/dashboard/tags), so any capability-token
# prefix in the browser's real URL survives the round trip.
_BACK_TO_PAGE = "../dashboard"

_AUTH_PATHS = ("/dashboard/login", "/dashboard/callback", "/dashboard/logout")
_POST_PATHS = (
    "/dashboard/scopes",
    "/dashboard/tags",
    "/dashboard/sweep",
    "/dashboard/suggest",
    "/dashboard/retention",
)


def _json_safe(data: dict) -> dict:
    """Make a model dict survive the SDK's json.dumps-based session sealing.

    The workos User model parses its timestamps into datetime objects, which
    seal_session_from_auth_response feeds straight into json.dumps — a crash.
    default=str stringifies them, matching what the SDK's own refresh path
    stores (it re-seals the raw API JSON, where timestamps are strings).
    """
    return json.loads(json.dumps(data, default=str))


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
        registry: Optional[ScopeRegistry] = None,
    ) -> None:
        self.app = app
        self.store = store
        self.oauth_mode = oauth_mode
        # Injectable for tests; lazily built from config in production so
        # capability-mode deploys never construct a WorkOS client at all.
        self._workos = workos_client
        # Injectable for tests; defaults to the process-wide registry the
        # register_scopes tool writes to, so the page shows what tools registered.
        self._scope_registry = registry

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path == "/dashboard/":  # tolerate the hand-typed trailing slash
            path = "/dashboard"
        if path != "/dashboard" and path not in _AUTH_PATHS + _POST_PATHS:
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if path in _POST_PATHS:
            if request.method != "POST":
                response: Response = PlainTextResponse(
                    "method not allowed", status_code=405
                )
            else:
                response = await self._mutate(request, path)
        elif request.method != "GET":
            response = PlainTextResponse("method not allowed", status_code=405)
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

    async def _principal(self, request: Request) -> Optional[_Principal]:
        """The viewer, per auth mode. None only in OAuth mode with no session."""
        if self.oauth_mode:
            return await self._authenticate_browser(request)
        # Capability mode: the path guard already authenticated this request,
        # and everything lives in the single-tenant namespace.
        return _Principal(
            user_id=config.DEFAULT_USER_ID, label=config.DEFAULT_USER_ID
        )

    async def _view(self, request: Request) -> Response:
        principal = await self._principal(request)
        if principal is None:
            if not config.dashboard_login_enabled():
                return PlainTextResponse(_LOGIN_UNCONFIGURED, status_code=503)
            return RedirectResponse("/dashboard/login", status_code=302)

        try:
            # store.all caps at its default ceiling (mem0 would otherwise return
            # only the 20 most recent); browsing/grouping beyond that is PER-84.
            rows = await run_in_threadpool(self.store.all, principal.user_id)
            scopes = await run_in_threadpool(
                self._registry().all, principal.user_id
            )
        except Exception:
            logger.exception(
                "dashboard failed to load memories/scopes for user=%s",
                principal.user_id,
            )
            return PlainTextResponse(
                "Couldn't load your memories (backend error). Try again shortly.",
                status_code=500,
            )

        log_dashboard_view(principal.user_id, self._client_label(request))
        response: Response = HTMLResponse(
            render_page(
                rows, scopes,
                user_label=principal.label, show_logout=self.oauth_mode,
                sweep=get_sweep_runner().status(principal.user_id).as_dict(),
                suggestions=get_proposal_holder().get(principal.user_id).as_dict(),
                tagging_enabled=classifier_enabled(),
                triage=get_triage_runner().status(principal.user_id).as_dict(),
                triage_enabled=triage_enabled(),
            )
        )
        if principal.fresh_cookie:
            self._set_session_cookie(response, principal.fresh_cookie)
        return response

    # --- mutations (scopes + tags) ----------------------------------------

    async def _mutate(self, request: Request, path: str) -> Response:
        principal = await self._principal(request)
        if principal is None:
            # A POST can't run the sign-in flow; make the browser reload the
            # page, which will.
            return PlainTextResponse(
                "Your session has expired — reload the dashboard and sign in.",
                status_code=403,
            )
        if not self._same_origin(request):
            return PlainTextResponse(
                "cross-origin request rejected", status_code=403
            )
        form = await request.form()
        fields = {key: str(value) for key, value in form.items()}
        try:
            if path == "/dashboard/scopes":
                response = await self._scopes_action(fields, principal, request)
            elif path == "/dashboard/sweep":
                response = await self._sweep_action(principal, request)
            elif path == "/dashboard/suggest":
                # The raw form, not `fields`: the confirm checklist submits one
                # `key` per ticked proposal, which flattening to a dict loses.
                response = await self._suggest_action(form, principal, request)
            elif path == "/dashboard/retention":
                response = await self._retention_action(fields, principal, request)
            else:
                response = await self._tags_action(fields, principal, request)
        except Exception:
            logger.exception(
                "dashboard mutation failed for user=%s path=%s",
                principal.user_id, path,
            )
            return PlainTextResponse(
                "Couldn't save that change (backend error). Try again shortly.",
                status_code=500,
            )
        if principal.fresh_cookie:
            self._set_session_cookie(response, principal.fresh_cookie)
        return response

    async def _scopes_action(
        self, fields: dict, principal: _Principal, request: Request
    ) -> Response:
        """Create or delete one of the user's OWN scopes (owner slug "user").

        Third parties' scopes are never touchable here: their vocabulary is
        theirs to re-register, and deleting one from under a party would
        silently orphan whatever the user tagged with it.
        """
        action = fields.get("action") or ""
        registry = self._registry()
        if action == "create":
            name = (fields.get("name") or "").strip()
            description = (fields.get("description") or "").strip()
            if not slugify(name):
                return PlainTextResponse(
                    "Scope name needs at least one letter or digit.",
                    status_code=400,
                )
            if len(name) > SLUG_MAX_CHARS:
                return PlainTextResponse(
                    f"Scope name is too long (max {SLUG_MAX_CHARS} characters).",
                    status_code=400,
                )
            if len(description) > DESCRIPTION_MAX_CHARS:
                return PlainTextResponse(
                    f"Description is too long (max {DESCRIPTION_MAX_CHARS} "
                    "characters).",
                    status_code=400,
                )
            await run_in_threadpool(
                registry.register,
                principal.user_id,
                owner_type="user",
                owner_slug=RESERVED_OWNER_SLUG,
                scopes=[(name, description)],
            )
        elif action == "delete":
            key = (fields.get("key") or "").strip()
            scope = await run_in_threadpool(registry.get, principal.user_id, key)
            if scope is None:
                return PlainTextResponse("no such scope", status_code=404)
            if scope.owner_name != RESERVED_OWNER_SLUG:
                return PlainTextResponse(
                    "Only scopes you created yourself can be deleted.",
                    status_code=403,
                )
            await run_in_threadpool(registry.delete, principal.user_id, key)
        else:
            return PlainTextResponse("unknown scope action", status_code=400)
        log_dashboard_action(
            principal.user_id, f"scopes_{action}", self._client_label(request)
        )
        return RedirectResponse(_BACK_TO_PAGE, status_code=303)

    async def _tags_action(
        self, fields: dict, principal: _Principal, request: Request
    ) -> Response:
        """Add or remove one consent-scope tag on one memory.

        Both actions are unconditional writes of a provenance value under the
        scope's cs_* metadata key: add stamps `user` (manual, survives
        re-sweeps), remove stamps the `user_removed` tombstone rather than
        deleting the key — mem0's update merges metadata and cannot remove
        keys, and the tombstone is what stops the classifier (VC-88) from
        re-applying a tag the user vetoed. Re-adding over a tombstone stamps
        `user` again.
        """
        action = fields.get("action") or ""
        memory_id = (fields.get("memory_id") or "").strip()
        scope_key = (fields.get("scope_key") or "").strip()
        if action not in ("add", "remove"):
            return PlainTextResponse("unknown tag action", status_code=400)
        if not memory_id or not scope_key:
            return PlainTextResponse(
                "memory_id and scope_key are required", status_code=400
            )
        scope = await run_in_threadpool(
            self._registry().get, principal.user_id, scope_key
        )
        if scope is None:
            return PlainTextResponse("no such scope", status_code=404)
        provenance = PROVENANCE_USER if action == "add" else PROVENANCE_USER_REMOVED
        try:
            result = await run_in_threadpool(
                self.store.update_metadata,
                memory_id,
                {tag_key(scope_key): provenance},
                principal.user_id,
            )
        except TenantIsolationError:
            # Another tenant's memory id: for THIS viewer it doesn't exist, and
            # the response must not reveal otherwise. Logged as a warning — a
            # browser shouldn't be able to produce foreign ids by accident.
            logger.warning(
                "dashboard tag write refused: memory %r is not user %r's",
                memory_id, principal.user_id,
            )
            return PlainTextResponse("no such memory", status_code=404)
        if not result.get("updated"):
            return PlainTextResponse("no such memory", status_code=404)
        log_dashboard_action(
            principal.user_id, f"tags_{action}", self._client_label(request)
        )
        return RedirectResponse(_BACK_TO_PAGE, status_code=303)

    async def _sweep_action(self, principal: _Principal, request: Request) -> Response:
        """Start a full re-classification of this user's memories.

        Returns as soon as the thread is running — a sweep is one LLM call per
        memory and would blow any request timeout if awaited. The page reads
        the runner's per-user status on the next load, so "did it finish" is a
        reload, not a held-open connection.

        A second POST while one is already running is accepted and ignored
        (logged as ``sweep_busy``) rather than erroring: the user clicked a
        button twice, which is not a failure worth showing them.
        """
        if not classifier_enabled():
            return PlainTextResponse(
                "Automatic tagging is off on this server: memories are only "
                "classified when EXTRACTION_MODE=anthropic, so nothing is sent "
                "to a model. You can still tag memories yourself.",
                status_code=409,
            )
        started = get_sweep_runner().start(
            self.store, self._registry(), principal.user_id
        )
        log_dashboard_action(
            principal.user_id,
            "sweep_start" if started else "sweep_busy",
            self._client_label(request),
        )
        return RedirectResponse(_BACK_TO_PAGE, status_code=303)

    async def _suggest_action(
        self, form: FormData, principal: _Principal, request: Request
    ) -> Response:
        """Propose scopes from this user's memories, then register the ticked ones.

        ``run`` makes one model call over a sample of the store and holds the
        candidates; ``confirm`` registers the ticked ones and drops the rest
        (see ``consent.discovery`` for why those are two acts and not one).

        Unlike the sweep this is a single call, so it runs inline on a worker
        thread and a failure can be reported straight back rather than through
        a status the page has to reload for.
        """
        action = str(form.get("action") or "")
        registry = self._registry()
        holder = get_proposal_holder()
        if action == "run":
            # Gating `run` alone, not the whole endpoint: confirm registers
            # what the user ticked and calls nothing, so refusing it with "no
            # memory is sent to a model" would be false and would strand a
            # checklist the user is already looking at.
            if not classifier_enabled():
                return PlainTextResponse(
                    "Scope suggestions are off on this server: memories are "
                    "only sent to a model when EXTRACTION_MODE=anthropic. You "
                    "can still create scopes yourself.",
                    status_code=409,
                )
            # Only the sample is used, so don't drag the whole store back for it.
            rows = await run_in_threadpool(
                self.store.all, principal.user_id, MAX_SAMPLE_MEMORIES
            )
            scopes = await run_in_threadpool(registry.all, principal.user_id)
            try:
                proposals = await run_in_threadpool(suggest_scopes, rows, scopes)
            except DiscoveryFailed:
                logger.exception(
                    "scope suggestion failed for user=%s", principal.user_id
                )
                return PlainTextResponse(
                    "Couldn't suggest scopes: this server didn't get an answer "
                    "from the model. Check its API key and model settings, then "
                    "the server logs.",
                    status_code=502,
                )
            holder.put(principal.user_id, proposals)
        elif action == "confirm":
            ticked = {str(key).strip() for key in form.getlist("key")}
            # Taken, not read: the checklist is spent either way, and clearing
            # it in the same step stops a double-submitted form registering
            # twice. Anything left unticked is simply discarded.
            pending = holder.take(principal.user_id)
            chosen = [p for p in pending.proposals if p.key in ticked]
            if chosen:
                # Re-checked against the registry rather than trusted from
                # generation time: register() upserts, so a proposal whose key
                # got registered in between would silently overwrite the
                # description that landed there first.
                taken = {
                    scope.key
                    for scope in await run_in_threadpool(
                        registry.all, principal.user_id
                    )
                }
                fresh = [p for p in chosen if p.key not in taken]
                await run_in_threadpool(
                    registry.register,
                    principal.user_id,
                    owner_type="user",
                    owner_slug=RESERVED_OWNER_SLUG,
                    scopes=[(p.name, p.description) for p in fresh],
                )
        else:
            return PlainTextResponse("unknown suggestion action", status_code=400)
        log_dashboard_action(
            principal.user_id, f"suggest_{action}", self._client_label(request)
        )
        return RedirectResponse(_BACK_TO_PAGE, status_code=303)

    async def _retention_action(
        self, fields: dict, principal: _Principal, request: Request
    ) -> Response:
        """Start a triage pass, or set one memory's retention state by hand.

        ``sweep`` is the model's pass over everything, started on a background
        thread exactly like the scope sweep and for the same reason — one call
        per memory would blow any request timeout if awaited — with the page
        reading its progress on the next load.

        ``keep`` and ``archive`` are the user overruling it, one memory at a
        time. Both write the same three keys the pass writes, under the
        ``user`` provenance that stops any later pass re-deciding them: this
        is the escape hatch that makes an automatic pass safe to run at all.
        Neither deletes anything — archiving is a state, and an archived
        memory keeps its place on this page.
        """
        action = fields.get("action") or ""
        if action == "sweep":
            if not triage_enabled():
                return PlainTextResponse(
                    "Triage is off on this server: memories are only judged by "
                    "a model when EXTRACTION_MODE=anthropic, so nothing is sent "
                    "to one. You can still set memories aside yourself.",
                    status_code=409,
                )
            started = get_triage_runner().start(self.store, principal.user_id)
            log_dashboard_action(
                principal.user_id,
                "retention_sweep_start" if started else "retention_sweep_busy",
                self._client_label(request),
            )
            return RedirectResponse(_BACK_TO_PAGE, status_code=303)
        if action not in ("keep", "archive"):
            return PlainTextResponse("unknown retention action", status_code=400)
        memory_id = (fields.get("memory_id") or "").strip()
        if not memory_id:
            return PlainTextResponse("memory_id is required", status_code=400)
        state = STATE_ARCHIVED if action == "archive" else STATE_KEEP
        try:
            result = await run_in_threadpool(
                self.store.update_metadata,
                memory_id,
                state_updates(state, SOURCE_USER),
                principal.user_id,
            )
        except TenantIsolationError:
            # Another tenant's memory id: for THIS viewer it doesn't exist, and
            # the response must not reveal otherwise — same as the tag path.
            logger.warning(
                "dashboard retention write refused: memory %r is not user %r's",
                memory_id, principal.user_id,
            )
            return PlainTextResponse("no such memory", status_code=404)
        if not result.get("updated"):
            return PlainTextResponse("no such memory", status_code=404)
        log_dashboard_action(
            principal.user_id, f"retention_{action}", self._client_label(request)
        )
        return RedirectResponse(_BACK_TO_PAGE, status_code=303)

    @staticmethod
    def _same_origin(request: Request) -> bool:
        """CSRF backstop for the POST endpoints: the browser-set Origin (or
        Referer, which older browsers send where Origin is omitted) must name
        exactly the host this request arrived at. Requests carrying neither
        header are rejected — the mutating surface is browser-only, and a
        browser form POST always sends at least one of them.
        """
        source = request.headers.get("origin") or request.headers.get("referer") or ""
        host = (request.headers.get("host") or "").strip().lower()
        return bool(host) and urlsplit(source).netloc.lower() == host

    def _registry(self) -> ScopeRegistry:
        if self._scope_registry is None:
            # Imported lazily to mirror how transport keeps tool modules out of
            # import time; get_registry() is the same instance register_scopes
            # writes to, so tool registrations show up here without re-reads.
            from context_layer.tools.consent_tools import get_registry

            self._scope_registry = get_registry()
        return self._scope_registry

    @staticmethod
    def _client_label(request: Request) -> str:
        """Mirror the access log's trust order: the capability guard's stamped
        token label (server-assigned) over the User-Agent (self-asserted)."""
        client = getattr(request.state, "client", None)
        return str(client or request.headers.get("user-agent") or "")

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
                user=_json_safe(dataclasses.asdict(auth.user)),
                impersonator=(
                    _json_safe(dataclasses.asdict(auth.impersonator))
                    if auth.impersonator
                    else None
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
