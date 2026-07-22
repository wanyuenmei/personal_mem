"""Access/audit logging: who called which tool, with what scope, when.

CapabilityPathGuard (server.py) stamps the resolved per-token client label
into the ASGI scope's "state" before forwarding to the MCP app; a Starlette
Request built from that scope exposes it as request.state.client. This module
is the other half: read that label back out and log it alongside the tool
name, scope, and timestamp on every tool call. stdio has no per-client token,
so those calls log as "stdio".

This is deliberately just structured logging (key=value on one line) rather
than a dedicated audit datastore — the foundation for the audit dashboard
(see PER-13), not that work itself.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from mcp.server.fastmcp import Context

logger = logging.getLogger("context_layer.access")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


def _client_label(ctx: Context | None) -> str:
    if ctx is None:
        return "stdio"
    try:
        request = ctx.request_context.request
    except ValueError:
        return "stdio"
    if request is None:
        return "stdio"
    return getattr(request.state, "client", "unlabeled")


def log_tool_call(tool: str, ctx: Context | None, scope: Optional[str] = None) -> None:
    """Log one access/audit record: tool, client ("who"), scope, timestamp.

    `scope` is the memory scope involved in the call (e.g. the `scope` param
    on add_memory). It's omitted for calls with no scope of their own
    (e.g. search_memory isn't scope-filtered today), and logged as
    scope=none in that case so the field is always present in the line.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    logger.info(
        "tool_call tool=%s client=%s scope=%s ts=%s",
        tool,
        _client_label(ctx),
        scope or "none",
        timestamp,
    )
