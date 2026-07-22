"""HTTP (streamable-http) transport assembly.

Wraps FastMCP's streamable-http ASGI app in the right guard for the active
auth mode, and adds a health-check endpoint in front:

- OAuth mode (config.workos_enabled()): FastMCP already added the auth routes +
  bearer middleware, so only RateLimitGuard applies — capability-path rewriting
  would 404 the /.well-known/* discovery routes and swallow the 401 connectors
  need to launch WorkOS sign-in.
- Capability-path mode (default): CapabilityPathGuard serves /<token>/mcp and
  404s everything else, never emitting a 401.

GET /health (and /healthz) is answered 200 *before* the guards, so a liveness/
readiness probe works without a capability token or a bearer token.
"""

from context_layer.auth import CapabilityPathGuard, RateLimitGuard
from context_layer.config import (
    CONTEXT_LAYER_TOKENS,
    MCP_HOST,
    MCP_PORT,
    RATE_LIMIT_RPM,
    workos_enabled,
)

_HEALTH_PATHS = ("/health", "/healthz")


class HealthCheck:
    """Answer GET /health(z) -> 200 before auth/rate-limit, for probes."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("path") in _HEALTH_PATHS:
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"ok"})
            return
        await self.app(scope, receive, send)


def build_asgi_app(mcp):
    """Build the guarded ASGI app for the streamable-http transport."""
    inner = mcp.streamable_http_app()
    if workos_enabled():
        guarded = RateLimitGuard(inner, RATE_LIMIT_RPM)
    else:
        guarded = CapabilityPathGuard(inner, CONTEXT_LAYER_TOKENS, RATE_LIMIT_RPM)
    return HealthCheck(guarded)


def run_http(mcp) -> None:
    """Serve the streamable-http transport with uvicorn."""
    import uvicorn

    uvicorn.run(build_asgi_app(mcp), host=MCP_HOST, port=MCP_PORT)
