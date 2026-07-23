"""Offline backfill: parse existing AI-export history into the memory store.

Each source parser maps its export onto the shared normalized conversation
format (`normalized.py`); the runner (`runner.py`) infers a scope per
conversation and feeds it through `ContextStore.add()` for mem0 extraction. This
is an offline batch layer — it is NOT part of the live MCP request path.
"""

from context_layer.ingest.claude import (
    parse_claude_conversations,
    parse_claude_export,
)
from context_layer.ingest.normalized import Conversation, Message
from context_layer.ingest.runner import (
    BackfillResult,
    Estimate,
    estimate,
    run_backfill,
)
from context_layer.ingest.scope import infer_scope

__all__ = [
    "BackfillResult",
    "Conversation",
    "Estimate",
    "Message",
    "estimate",
    "infer_scope",
    "parse_claude_conversations",
    "parse_claude_export",
    "run_backfill",
]
