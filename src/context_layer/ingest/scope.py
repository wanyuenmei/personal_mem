"""Infer a coarse scope tag for a conversation using the extraction LLM.

Backfilled conversations are tagged with a scope (dietary, travel, work, …) so
imported memories slot into the same scope schema as everything the live tools
write. We ask the extraction LLM to classify each conversation into exactly one
scope from a fixed vocabulary.

Privacy invariant: this only calls out when ``EXTRACTION_MODE == "anthropic"``
(the default/deploy mode). In ``none`` mode nothing leaves the machine, so scope
inference degrades to the caller's default — same trust guarantee as the rest of
the store (ollama scope-inference is a follow-up). Any error or unexpected model
output also falls back to the default, so a bad classification never blocks an
import.
"""

import logging

from context_layer.config import ANTHROPIC_MODEL, EXTRACTION_MODE
from context_layer.ingest.normalized import Conversation

logger = logging.getLogger("context_layer.ingest.scope")

# Canonical scope vocabulary the classifier must choose from. Kept aligned with
# the coarse categories the memory tools already use (general/shopping/dietary/
# travel/writing_style/work), plus a few common life domains.
SCOPES = (
    "general",
    "shopping",
    "dietary",
    "travel",
    "writing_style",
    "work",
    "health",
    "finance",
    "relationships",
    "hobbies",
    "education",
)

# How much of the transcript to send to the classifier. Scope is a coarse
# signal — the opening turns are plenty — and truncating keeps this extra call
# cheap on top of the (already paid-for) extraction call.
_TRANSCRIPT_CHARS = 2000


def _transcript(conversation: Conversation) -> str:
    parts = []
    if conversation.title:
        parts.append(f"Title: {conversation.title}")
    for m in conversation.messages:
        parts.append(f"{m.role}: {m.text}")
    return "\n".join(parts)[:_TRANSCRIPT_CHARS]


def infer_scope(conversation: Conversation, *, default: str = "general") -> str:
    """Classify a conversation into exactly one scope from ``SCOPES``.

    Returns ``default`` immediately — with no network call — unless
    ``EXTRACTION_MODE == "anthropic"``. On the anthropic path, an error or an
    out-of-vocabulary reply also falls back to ``default``.
    """
    if EXTRACTION_MODE != "anthropic":
        return default
    transcript = _transcript(conversation)
    if not transcript.strip():
        return default
    try:
        import anthropic

        client = anthropic.Anthropic()
        prompt = (
            "Classify the following conversation into exactly ONE category from "
            f"this list: {', '.join(SCOPES)}.\n"
            "Reply with only the single category word, nothing else.\n\n"
            f"{transcript}"
        )
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=8,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(
            getattr(block, "text", "")
            for block in resp.content
            if getattr(block, "type", None) == "text"
        )
    except Exception:
        logger.exception("scope inference failed; falling back to %r", default)
        return default
    scope = raw.strip().lower()
    return scope if scope in SCOPES else default
