"""Pure-ASGI guards for the HTTP transport.

Neither is a BaseHTTPMiddleware, so they never buffer the SSE stream. Both add
a crude global rate limit (a spend cap so an exposed endpoint can't run up
unbounded Anthropic spend). CapabilityPathGuard additionally implements the
capability-path auth used when WorkOS OAuth is not configured; RateLimitGuard
is the rate-limit-only variant used in OAuth mode, where FastMCP owns auth.
"""

import time
from collections import deque


class RateLimitGuard:
    """Just the crude global rate limit, no path matching.

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
    """Per-client capability paths + a global rate limit.

    When tokens are configured, each client connects at its own secret path
    /<token>/mcp; the guard matches the token, strips the prefix, stamps the
    client label into scope["state"] (per-client attribution for the access
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
