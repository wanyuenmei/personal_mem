"""HTTP (streamable-http) transport assembly.

Wraps FastMCP's streamable-http ASGI app in the memory-browser dashboard and
the right guard for the active auth mode, plus a health-check endpoint in
front:

- OAuth mode (config.workos_enabled()): FastMCP already added the auth routes +
  bearer middleware, so only RateLimitGuard applies — capability-path rewriting
  would 404 the /.well-known/* discovery routes and swallow the 401 connectors
  need to launch WorkOS sign-in. The dashboard runs its own AuthKit browser
  sign-in (FastMCP's bearer middleware only covers /mcp).
- Capability-path mode (default): CapabilityPathGuard serves /<token>/mcp and
  /<token>/dashboard and 404s everything else, never emitting a 401.

The dashboard sits INSIDE the guard, so in capability mode a request only ever
reaches it through a valid token path.

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
    # Imported here, not at module top: importing the tools module constructs
    # the process-wide ContextStore (embedder and all), which nothing should
    # pay for just by importing the transport layer.
    from context_layer.consent import ScopeTaggingHandler
    from context_layer.curation import RetentionHandler
    from context_layer.dashboard import DashboardApp
    from context_layer.jobs import RunStore, SweepWorker
    from context_layer.tools.consent_tools import get_registry
    from context_layer.tools.memory_tools import get_store

    store, registry = get_store(), get_registry()
    runs = RunStore.from_config()
    # The worker that actually executes sweeps, started here because this is
    # where the process owns a store to sweep. It picks up runs the dashboard
    # enqueues and, on boot, any run a previous process was killed
    # mid-pass — which is the whole point of the runs being rows (VC-98).
    SweepWorker(
        runs, store, [ScopeTaggingHandler(registry), RetentionHandler()]
    ).start()

    inner = DashboardApp(
        mcp.streamable_http_app(),
        store,
        oauth_mode=workos_enabled(),
        # The same registry instance register_scopes writes to, so the page
        # shows a party's vocabulary the moment it registers.
        registry=registry,
        runs=runs,
    )
    if workos_enabled():
        guarded = RateLimitGuard(inner, RATE_LIMIT_RPM)
    else:
        guarded = CapabilityPathGuard(inner, CONTEXT_LAYER_TOKENS, RATE_LIMIT_RPM)
    return HealthCheck(guarded)


def run_http(mcp) -> None:
    """Serve the streamable-http transport with uvicorn."""
    import uvicorn

    uvicorn.run(build_asgi_app(mcp), host=MCP_HOST, port=MCP_PORT)
