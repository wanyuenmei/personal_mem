"""The scope vocabulary and the classifier that assigns it.

Scope is a coarse category tag stored alongside every memory. It is decided
**server-side**, never by the calling client: it is an exact-match filter on
read, so an unconstrained caller could fragment retrieval just by capitalising
differently, and once the consent layer uses scope to gate what a grantee may
read, a client could tag everything ``general`` to keep it readable. Neither is
a risk the caller should be able to take.

Both write paths share this module so one vocabulary governs the whole store:
the live ``add_memory`` tool classifies the text it is about to save, and the
backfill importer classifies a conversation transcript
(``ingest.scope.infer_scope`` builds the transcript and delegates here).

mem0 is not involved. ``ContextStore.add`` passes the tag through as opaque
metadata; mem0 does fact extraction, dedup and embedding, and never interprets
scope.

Privacy invariant: this only calls out when ``EXTRACTION_MODE == "anthropic"``
(the default/deploy mode). In ``none``/``ollama`` mode nothing leaves the
machine, so classification degrades to ``DEFAULT_SCOPE`` — same trust guarantee
as the rest of the store. Any error or unexpected model output also falls back,
so a bad classification never blocks a write.
"""

import logging

from context_layer.config import ANTHROPIC_MODEL, EXTRACTION_MODE

logger = logging.getLogger("context_layer.memory.scope")

# Canonical scope vocabulary the classifier must choose from — the coarse life
# domains a personal context store divides into. Shared by the live and backfill
# paths; changing it changes how everything written afterwards is tagged.
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

DEFAULT_SCOPE = "general"

# How much text to send to the classifier. Scope is a coarse signal — the
# opening lines are plenty — and truncating keeps the call cheap.
MAX_CLASSIFY_CHARS = 2000


def classify(text: str, *, default: str = DEFAULT_SCOPE) -> str:
    """Classify text into exactly one scope from ``SCOPES``.

    Returns ``default`` immediately — with no network call — unless
    ``EXTRACTION_MODE == "anthropic"``. On the anthropic path, an error or an
    out-of-vocabulary reply also falls back to ``default``.
    """
    if EXTRACTION_MODE != "anthropic":
        return default

    snippet = text.strip()[:MAX_CLASSIFY_CHARS]
    if not snippet:
        return default

    try:
        import anthropic

        client = anthropic.Anthropic()
        prompt = (
            "Classify the following into exactly ONE category from "
            f"this list: {', '.join(SCOPES)}.\n"
            "Reply with only the single category word, nothing else.\n\n"
            f"{snippet}"
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
        logger.exception("scope classification failed; falling back to %r", default)
        return default

    scope = raw.strip().lower()
    return scope if scope in SCOPES else default
