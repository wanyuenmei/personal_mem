"""Access logging: which client called which tool.

CapabilityPathGuard (server.py) stamps the resolved per-token client label
into the ASGI scope's "state" before forwarding to the MCP app; a Starlette
Request built from that scope exposes it as request.state.client. This module
is the other half: read that label back out and log it alongside the tool
name on every tool call. stdio has no per-client token, so those calls log
as "stdio".
"""

import logging

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


def log_tool_call(tool: str, ctx: Context | None) -> None:
    logger.info("tool_call tool=%s client=%s", tool, _client_label(ctx))
