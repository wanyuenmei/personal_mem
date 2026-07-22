"""Composition root: build the MCP server and run it on the selected transport.

Transport is env-selected (config.MCP_TRANSPORT):
  - stdio (default)   -> local Claude Desktop / Claude Code / Cursor
  - streamable-http   -> remote deploy (Railway), for Claude + ChatGPT connectors

Every tool resolves the caller to a user_id via identity.resolve_user_id(ctx) —
single-tenant today, the auth seam for when OAuth lands. For the HTTP transport,
auth is chosen by whether WorkOS is configured (see transport.http and the auth
layer); build_fastmcp_auth_kwargs() adds the OAuth resource-server settings when
WorkOS is set, or nothing otherwise — leaving the server identical when OAuth is
off.

This module wires the layers together and owns no logic of its own: tools live
in the tools layer, guards/OAuth in the auth layer, HTTP assembly in the
transport layer.
"""

from mcp.server.fastmcp import FastMCP

from context_layer.auth import build_fastmcp_auth_kwargs
from context_layer.config import MCP_HOST, MCP_PORT, MCP_TRANSPORT
from context_layer.tools import register_memory_tools
from context_layer.transport import run_http

_INSTRUCTIONS = (
    "This server is the user's personal context layer — their authoritative, "
    "cross-app memory. Proactively call search_memory before answering anything "
    "that could depend on who the user is (preferences, history, plans, style); "
    "call add_memory whenever the user shares a lasting fact. The user expects "
    "these tools to be used without being asked."
)


def build_mcp() -> FastMCP:
    """Construct the FastMCP server: auth wiring + registered tools."""
    mcp = FastMCP(
        "personal-context-layer",
        instructions=_INSTRUCTIONS,
        host=MCP_HOST,
        port=MCP_PORT,
        stateless_http=True,
        streamable_http_path="/mcp",
        **build_fastmcp_auth_kwargs(),
    )
    register_memory_tools(mcp)
    return mcp


def run() -> None:
    """Build the server and serve it on the configured transport."""
    mcp = build_mcp()
    if MCP_TRANSPORT in ("streamable-http", "http"):
        run_http(mcp)
    elif MCP_TRANSPORT == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    run()
