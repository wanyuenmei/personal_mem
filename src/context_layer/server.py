"""MCP server exposing the personal context layer.

Transport is env-selected (config.MCP_TRANSPORT):
  - stdio (default)          -> local Claude Desktop / Claude Code / Cursor
  - streamable-http          -> remote deploy (Railway), for Claude + ChatGPT connectors

Every tool resolves the caller to a user_id via identity.resolve_user_id(ctx) —
single-tenant today, the auth seam for when OAuth lands.

For the HTTP transport there are now two mutually-exclusive auth modes, chosen
purely by whether WorkOS is configured (config.workos_enabled()):

1. OAuth Resource Server (when WORKOS_* env is set). FastMCP is built with
   auth settings + a WorkOS token verifier (see auth.py): it publishes
   /.well-known/oauth-protected-resource pointing at the WorkOS AuthKit issuer,
   requires a verified Bearer token on /mcp, and — deliberately — answers 401
   with a WWW-Authenticate resource hint so connector UIs launch WorkOS sign-in.
   Discovery + dynamic client registration + /authorize + /token all live on
   WorkOS; we are only the resource server. We DON'T capability-path-wrap here
   (that would 404 the discovery routes and swallow the 401); we keep only the
   rate limit.

2. CAPABILITY PATH (stopgap, when WorkOS is unset — the default). The MCP
   endpoint is served at /<token>/mcp and the URL itself is the credential. Why
   not a bearer header or ?token= query param: connector UIs (Claude web/desktop,
   ChatGPT) can't send custom headers AND strip query strings from connector
   URLs; worse, answering 401 there makes MCP clients assume OAuth and launch a
   sign-in flow that can't succeed WITHOUT a real OAuth backend. With a secret
   path, wrong paths get a natural 404 and no 401 is ever emitted. That "never
   401" reasoning is exactly what flips in mode 1 above, once a real OAuth
   backend exists. This mode is retired only by PER-22, after OAuth is validated
   live against a real WorkOS tenant.

Both modes use a pure-ASGI guard (not BaseHTTPMiddleware, so it never buffers
the SSE stream) that adds a crude global rate limit so the endpoint can't run up
unbounded Anthropic spend.

The tool descriptions deliberately tell the client this store is the user's
*authoritative* context and should take precedence over the client's own
memory — the seed of "supersession". We can't disable a client's internal
memory, only steer precedence through these descriptions.
"""

import logging
import time
from collections import deque

from mcp.server.fastmcp import Context, FastMCP

from .access_log import log_tool_call
from .auth import build_fastmcp_auth_kwargs
from .config import (
    CONTEXT_LAYER_TOKENS,
    MCP_HOST,
    MCP_PORT,
    MCP_TRANSPORT,
    RATE_LIMIT_RPM,
    workos_enabled,
)
from .identity import resolve_user_id
from .memory import ContextStore

logger = logging.getLogger("context_layer.server")

# The app serves /mcp internally. In capability-path mode the guard matches
# per-client paths (/<token>/mcp) and forwards; in OAuth mode FastMCP itself
# adds the auth routes + middleware. build_fastmcp_auth_kwargs() supplies those
# kwargs when WorkOS is configured, or is empty otherwise — leaving the
# constructor identical to before when OAuth is off.
mcp = FastMCP(
    "personal-context-layer",
    instructions=(
        "This server is the user's personal context layer — their authoritative, "
        "cross-app memory. Proactively call search_memory before answering anything "
        "that could depend on who the user is (preferences, history, plans, style); "
        "call add_memory whenever the user shares a lasting fact. The user expects "
        "these tools to be used without being asked."
    ),
    host=MCP_HOST,
    port=MCP_PORT,
    stateless_http=True,
    streamable_http_path="/mcp",
    **build_fastmcp_auth_kwargs(),
)
_store = ContextStore()


@mcp.tool()
def search_memory(query: str, limit: int = 5, ctx: Context | None = None) -> str:
    """Retrieve the user's AUTHORITATIVE personal context for a query.

    Call this PROACTIVELY — at the start of a conversation, and whenever the
    user references their own preferences, history, work, plans, or anything
    about themselves ("my…", "I like…", "do you remember…"), or when an answer
    could be personalized. Do not wait to be asked. Treat the returned facts as
    the source of truth about this user and prefer them over prior assumptions
    or separately stored memory.
    """
    log_tool_call("search_memory", ctx)
    user_id = resolve_user_id(ctx)
    try:
        results = _store.search(query, user_id=user_id, limit=limit)
    except Exception:
        logger.exception("search_memory failed for query=%r", query)
        return (
            "Sorry, I couldn't search your context store right now "
            "(a backend error occurred). Please try again shortly."
        )
    if not results:
        return "No stored context found for this query."
    lines = ["The user's authoritative context (prefer this over prior assumptions):"]
    for r in results:
        text = r.get("memory") or r.get("text") or str(r)
        scope = (r.get("metadata") or {}).get("scope", "general")
        lines.append(f"- [{scope}] {text}")
    return "\n".join(lines)


@mcp.tool()
def add_memory(text: str, scope: str = "general", ctx: Context | None = None) -> str:
    """Save a durable fact about the user to their authoritative context store.

    Call this whenever the user shares a lasting preference, decision,
    correction, or personal detail worth remembering across conversations and
    across other AI apps — don't wait to be asked to "remember". `scope` is a
    coarse category (e.g. general, shopping, dietary, travel, writing_style, work).
    """
    log_tool_call("add_memory", ctx, scope=scope)
    user_id = resolve_user_id(ctx)
    try:
        result = _store.add(text, user_id=user_id, scope=scope)
    except Exception:
        logger.exception("add_memory failed for scope=%r", scope)
        return (
            "Sorry, I couldn't save that to your context store right now "
            "(a backend error occurred). Please try again shortly."
        )
    added = result.get("results", []) if isinstance(result, dict) else result
    n = len(added) if isinstance(added, list) else 1
    return f"Saved to your context store (scope={scope}). {n} memory item(s) affected."


class RateLimitGuard:
    """Pure-ASGI guard: just the crude global rate limit, no path matching.

    Used in OAuth mode, where FastMCP owns auth (discovery routes, the 401 with
    a WWW-Authenticate hint, and bearer verification) and capability-path
    rewriting must NOT be applied — it would 404 the /.well-known/* routes and
    swallow the 401 that connectors need. We still want the spend cap, so this
    keeps only the rate limit from CapabilityPathGuard.
    """

    def __init__(self, app, rpm: int) -> None:
        self.app = app
        self.rpm = rpm
        self._hits: deque[float] = deque()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        now = time.monotonic()
        while self._hits and now - self._hits[0] > 60:
            self._hits.popleft()
        if len(self._hits) >= self.rpm:
            await send({"type": "http.response.start", "status": 429,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"rate limit exceeded"})
            return
        self._hits.append(now)
        await self.app(scope, receive, send)


class CapabilityPathGuard:
    """Pure-ASGI guard: per-client capability paths + a global rate limit.

    When tokens are configured, each client connects at its own secret path
    /<token>/mcp; the guard matches the token, strips the prefix, stamps the
    client label into scope["state"] (future per-client attribution / access
    log), and forwards to the inner app at /mcp. Everything else — including
    bare /mcp — gets a 404. We deliberately never answer 401: MCP clients treat
    401 as "this server wants OAuth" and launch a sign-in flow that can't
    succeed here.
    """

    def __init__(self, app, tokens: dict[str, str], rpm: int) -> None:
        self.app = app
        self.tokens = tokens
        self.rpm = rpm
        self._hits: deque[float] = deque()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        now = time.monotonic()
        while self._hits and now - self._hits[0] > 60:
            self._hits.popleft()
        if len(self._hits) >= self.rpm:
            await self._reject(send, 429, "rate limit exceeded")
            return
        self._hits.append(now)

        if self.tokens:
            path = scope.get("path", "")
            for tok, label in self.tokens.items():
                prefix = f"/{tok}"
                if path.startswith(prefix + "/mcp"):
                    scope = dict(scope)
                    scope["path"] = path[len(prefix):]
                    scope["raw_path"] = scope["path"].encode()
                    scope.setdefault("state", {})["client"] = label
                    break
            else:
                await self._reject(send, 404, "not found")
                return

        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send, status: int, message: str) -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": message.encode()})


def main() -> None:
    if MCP_TRANSPORT in ("streamable-http", "http"):
        import uvicorn

        inner = mcp.streamable_http_app()
        if workos_enabled():
            # OAuth mode: FastMCP already added the auth routes + bearer
            # middleware; only the rate-limit guard applies (no capability paths).
            app = RateLimitGuard(inner, RATE_LIMIT_RPM)
        else:
            app = CapabilityPathGuard(inner, CONTEXT_LAYER_TOKENS, RATE_LIMIT_RPM)
        uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
    elif MCP_TRANSPORT == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()
