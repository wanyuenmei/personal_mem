"""Access/audit logging: who called which tool, as whom, from where, when.

Each tool call emits one single-line JSON object. That shape is what makes the
log queryable on the deploy: Railway parses single-line JSON, promotes every
key to an attribute, and can then filter on them — ``@user:workos_…``,
``@client:claude-ai``, ``@tool:add_memory``. A plain
``key=value`` line would arrive as one opaque ``message`` string, searchable
only as free text. ``message`` and ``level`` are the two reserved keys.

The line carries two identities:

  - **user** — the tenant namespace from ``identity.resolve_user_id``: the
    OAuth token's subject under WorkOS, or the single-tenant default otherwise.
    It's the same value the memories are stored under, so a log line and a
    stored row can always be matched up.
  - **client** / **client_id** — which app made the call, from two sources with
    very different trust levels (see ``_client_label`` / ``_oauth_client_id``).

The two auth modes populate the client fields differently, because they are
mutually exclusive (``transport.http.build_asgi_app`` installs one guard or the
other):

  - Capability-path mode stamps a per-token label into the ASGI scope's
    "state", which a Starlette Request exposes as ``request.state.client``.
  - OAuth mode never runs that guard, so no label exists; the client is named
    from the request's ``User-Agent`` instead, and ``client_id`` comes from the
    verified access token when it carries a client claim.

This is deliberately just structured logging rather than a dedicated audit
datastore — the foundation for the audit dashboard (see PER-13), not that work
itself.
"""

import json
import logging
from datetime import datetime, timezone

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import Context

from context_layer.identity import resolve_user_id

# The record must reach the stream as bare JSON — any prefix (timestamp, logger
# name) would stop it parsing as structured output. So this logger owns its own
# handler emitting only the message, and does not propagate to root, whose
# formatter does add such a prefix.
logger = logging.getLogger("context_layer.access")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

# Every caller-supplied value (User-Agent, client_id) is bounded so one caller
# can't emit unbounded log lines.
# JSON encoding handles the *injection* half of the problem on its own: a client
# sending `x", "user": "someone_else` gets escaped into a single string value
# rather than forging a sibling attribute.
_MAX_FIELD = 64


def _bounded(value: str, fallback: str) -> str:
    trimmed = value.strip()[:_MAX_FIELD]
    return trimmed or fallback


def _client_label(ctx: Context | None) -> str:
    """Best available *human-readable* client name.

    Prefers the capability-path token label, which the server assigns and can
    therefore trust. Under OAuth that guard never runs, so this falls back to
    the request's ``User-Agent`` — enough to tell claude.ai from ChatGPT from
    Cursor when reading logs, but asserted by the caller, so never a basis for
    authorization.

    The MCP handshake's ``clientInfo.name`` would be the tidier source, and is
    deliberately not used: the server runs ``stateless_http=True``, so FastMCP
    builds a fresh session per request with no preceding ``initialize`` and
    ``client_params`` is never populated by the time a tool runs.
    """
    if ctx is None:
        return "stdio"

    try:
        request = ctx.request_context.request
    except ValueError:
        return "stdio"

    if request is None:
        return "stdio"

    label = getattr(request.state, "client", None)
    if label:
        return _bounded(str(label), "unlabeled")

    user_agent = ""
    try:
        user_agent = request.headers.get("user-agent") or ""
    except AttributeError:
        pass
    return _bounded(user_agent, "unlabeled")


def _oauth_client_id() -> str:
    """The OAuth client the token was issued to, or "none" outside OAuth mode.

    Unlike the client name this is not self-asserted — the bearer middleware has
    already verified the token — but on this deployment it is usually absent.

    WorkOS's MCP-flow tokens carry neither ``client_id`` nor ``azp``, so
    ``WorkOSTokenVerifier`` reports ``"unknown"`` and that is what lands here.
    The field is kept because a tenant whose tokens *do* carry the claim gets
    real per-app attribution from it; recovering it otherwise needs token
    introspection (PER-65).
    """
    token = get_access_token()
    client_id = (token.client_id or "").strip() if token is not None else ""
    return _bounded(client_id, "none") if client_id else "none"


def log_tool_call(tool: str, ctx: Context | None) -> None:
    """Log one access/audit record: tool, user, client, timestamp.

    Every field is always present, so a query on one never silently skips
    records that simply lacked the key.
    """
    logger.info(
        json.dumps(
            {
                "level": "info",
                "message": "tool_call",
                "tool": tool,
                "user": resolve_user_id(ctx),
                "client": _client_label(ctx),
                "client_id": _oauth_client_id(),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )
    )
