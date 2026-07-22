"""The single seam where a caller's identity resolves to a mem0 user_id.

Right now (M2.0/M2.1, single-tenant) this always returns DEFAULT_USER_ID. At
M2.2, once the managed-OAuth layer is wired, this is the ONE place that reads
the authenticated principal off the request context and returns their stable
per-user id — so tenant isolation lives behind a single, testable function.
"""

from typing import Optional

from mcp.server.fastmcp import Context

from .config import DEFAULT_USER_ID


def resolve_user_id(ctx: Optional[Context] = None) -> str:
    # TODO(M2.2): when auth is enabled, extract the authenticated user from the
    # request context (e.g. ctx.request_context / the OAuth token's subject) and
    # return a stable id namespaced per tenant. Until then, single tenant.
    return DEFAULT_USER_ID
