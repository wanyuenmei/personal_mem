"""The single seam where a caller's identity resolves to a mem0 user_id.

Single-tenant by default (returns DEFAULT_USER_ID). When the WorkOS OAuth
layer is configured, this is the ONE place that reads the authenticated
principal off the request context and returns their stable, per-tenant id — so
tenant isolation lives behind a single, testable function.

Mechanism: FastMCP's auth middleware verifies the Bearer token per request and
stashes the resulting AccessToken in a contextvar (mcp.server.auth's
``get_access_token``). We read the token's ``sub`` from there. When there is no
authenticated principal — stdio, the capability-path deploy, or OAuth simply not
configured — ``get_access_token`` returns None and we fall back to the
single-tenant default, so existing behavior is unchanged.
"""

from typing import Optional

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import Context

from context_layer.config import DEFAULT_USER_ID, WORKOS_USER_ID_PREFIX


def resolve_user_id(ctx: Optional[Context] = None) -> str:
    # The authenticated principal (if any) is verified per-request by FastMCP's
    # bearer-auth middleware and exposed via this contextvar. `ctx` is kept in
    # the signature (server.py passes it) but the principal comes from the
    # request-scoped auth context, not from the tool arguments.
    token = get_access_token()
    if token is not None and token.subject:
        # Namespace by prefix so an authenticated tenant can never collide with
        # the single-tenant DEFAULT_USER_ID space.
        return f"{WORKOS_USER_ID_PREFIX}{token.subject}"
    return DEFAULT_USER_ID
