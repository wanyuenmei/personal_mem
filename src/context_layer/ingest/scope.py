"""Infer a coarse scope tag for a whole conversation.

The vocabulary and the classifier live in ``memory.scope``, shared with the live
``add_memory`` path so imported memories land in the same scope schema as
everything the tools write — previously this module owned its own copy, and
nothing kept the two aligned. All this adds is the conversation-shaped part:
flattening a transcript before classifying it.
"""

from context_layer.ingest.normalized import Conversation
from context_layer.memory.scope import DEFAULT_SCOPE, MAX_CLASSIFY_CHARS, SCOPES, classify

__all__ = ["SCOPES", "infer_scope"]


def _transcript(conversation: Conversation) -> str:
    parts = []
    if conversation.title:
        parts.append(f"Title: {conversation.title}")
    for m in conversation.messages:
        parts.append(f"{m.role}: {m.text}")
    return "\n".join(parts)[:MAX_CLASSIFY_CHARS]


def infer_scope(conversation: Conversation, *, default: str = DEFAULT_SCOPE) -> str:
    """Classify a conversation into exactly one scope from ``SCOPES``.

    Falls back to ``default`` when classification is unavailable or the model
    replies out of vocabulary — see ``memory.scope.classify``.
    """
    return classify(_transcript(conversation), default=default)
