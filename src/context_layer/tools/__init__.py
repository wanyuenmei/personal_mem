"""Tools layer: the MCP tools exposed to clients."""

from context_layer.tools.consent_tools import (
    register_consent_tools,
    register_scopes,
)
from context_layer.tools.memory_tools import (
    add_memory,
    register_memory_tools,
    search_memory,
)

__all__ = [
    "add_memory",
    "register_consent_tools",
    "register_memory_tools",
    "register_scopes",
    "search_memory",
]
