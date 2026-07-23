"""Offline backfill: parse existing AI-export history into the memory store.

Each source parser maps its export onto the shared normalized conversation
format (`normalized.py`); downstream ingest code is written once against that
format. This is an offline batch layer — it is NOT part of the live MCP request
path.
"""

from context_layer.ingest.claude import (
    parse_claude_conversations,
    parse_claude_export,
)
from context_layer.ingest.normalized import Conversation, Message

__all__ = [
    "Conversation",
    "Message",
    "parse_claude_conversations",
    "parse_claude_export",
]
