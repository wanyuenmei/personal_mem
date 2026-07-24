"""The MCP tools this server exposes: search_memory and add_memory.

The tool functions are module-level (so they're directly importable/testable),
and `register_memory_tools(mcp)` applies the FastMCP `@tool` decoration to them
against a given server instance — keeping tool definitions decoupled from the
composition root (app.py) that builds the server.

The tool descriptions deliberately tell the client this store is the user's
*authoritative* context and should take precedence over the client's own
memory — the seed of "supersession". We can't disable a client's internal
memory, only steer precedence through these descriptions.
"""

import logging

from mcp.server.fastmcp import Context, FastMCP

from context_layer.identity import resolve_user_id
from context_layer.memory import ContextStore
from context_layer.memory.scope import classify
from context_layer.observability import log_tool_call

logger = logging.getLogger("context_layer.tools")

# One store per process. Kept at module scope (not built inside the tools) so a
# test can patch mem0.Memory.from_config before import and get a fake store.
_store = ContextStore()


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


def add_memory(text: str, ctx: Context | None = None) -> str:
    """Save a durable fact about the user to their authoritative context store.

    Call this whenever the user shares a lasting preference, decision,
    correction, or personal detail worth remembering across conversations and
    across other AI apps — don't wait to be asked to "remember".
    """
    log_tool_call("add_memory", ctx)
    user_id = resolve_user_id(ctx)
    # Classified here, not taken from the caller: scope is an exact-match filter
    # on read and will gate third-party access, so the client must not choose it.
    scope = classify(text)
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


def register_memory_tools(mcp: FastMCP) -> None:
    """Register the memory tools on a FastMCP server instance."""
    mcp.tool()(search_memory)
    mcp.tool()(add_memory)
