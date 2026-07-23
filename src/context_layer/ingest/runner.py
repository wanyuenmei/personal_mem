"""Batch extraction runner: normalized conversations → mem0.

Feeds each conversation through ``ContextStore.add()`` so an export becomes
extracted, scope-tagged memories. A full history import is the single most
expensive operation in the product (every conversation costs Anthropic tokens),
so the CLI (``scripts/backfill.py``) previews an ``estimate`` and only runs on an
explicit opt-in.

Idempotency/resume is a separate concern (PER-29): here we stamp
``source``/``source_id`` provenance into each memory's metadata, which that
manifest will key on to skip already-imported conversations.
"""

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from context_layer.ingest.normalized import Conversation
from context_layer.ingest.scope import infer_scope
from context_layer.memory import TenantIsolationError

logger = logging.getLogger("context_layer.ingest.runner")

# Rough $/1M-input-token rate for the default (Haiku-class) extraction model,
# used ONLY for an order-of-magnitude preview so a user can gauge a large import
# before spending. Approximate and provider-priced — the printed figure is a
# ballpark, not a bill.
_APPROX_USD_PER_MTOK = 1.0
_CHARS_PER_TOKEN = 4

ScopeFn = Callable[[Conversation], str]
# on_progress(index, total, conversation, resolved_scope, facts_added_for_it)
ProgressFn = Callable[[int, int, Conversation, str, int], None]


@dataclass
class Estimate:
    conversations: int
    messages: int
    approx_input_tokens: int
    llm_calls: int
    approx_cost_usd: float


@dataclass
class BackfillResult:
    conversations: int
    facts_added: int
    skipped: int
    failures: int


def _limited(
    conversations: list[Conversation], limit: Optional[int]
) -> list[Conversation]:
    return conversations[:limit] if limit is not None else conversations


def estimate(
    conversations: list[Conversation],
    *,
    with_extraction: bool = True,
    with_scope_inference: bool = True,
    limit: Optional[int] = None,
) -> Estimate:
    """Preview the size and rough cost of importing ``conversations``.

    Mirrors what ``run_backfill`` will actually do (same ``limit``, same
    empty-conversation skipping) so the preview matches the run. Each non-empty
    conversation costs one extraction call (unless ``with_extraction`` is off —
    the 0-cost path) plus one scope-inference call when ``with_scope_inference``.
    """
    convs = _limited(conversations, limit)
    n_msgs = sum(len(c.messages) for c in convs)
    chars = sum(len(m.text) for c in convs for m in c.messages)
    approx_tokens = chars // _CHARS_PER_TOKEN
    non_empty = sum(1 for c in convs if c.messages)
    per_conv_calls = (1 if with_extraction else 0) + (1 if with_scope_inference else 0)
    calls = non_empty * per_conv_calls
    cost = (approx_tokens / 1_000_000) * _APPROX_USD_PER_MTOK if with_extraction else 0.0
    return Estimate(
        conversations=len(convs),
        messages=n_msgs,
        approx_input_tokens=approx_tokens,
        llm_calls=calls,
        approx_cost_usd=round(cost, 4),
    )


def _facts_count(result: object) -> int:
    """mem0.add returns {'results': [...]} (or a bare list); count the items."""
    if isinstance(result, dict):
        results = result.get("results", [])
        return len(results) if isinstance(results, list) else 0
    return len(result) if isinstance(result, list) else 0


def run_backfill(
    conversations: list[Conversation],
    store,
    user_id: str,
    *,
    scope: Optional[str] = None,
    scope_fn: ScopeFn = infer_scope,
    limit: Optional[int] = None,
    infer: Optional[bool] = None,
    on_progress: Optional[ProgressFn] = None,
) -> BackfillResult:
    """Import ``conversations`` into ``store`` for ``user_id``.

    ``scope`` forces a fixed scope for every conversation (and skips
    ``scope_fn``); otherwise ``scope_fn`` resolves a scope per conversation.
    ``infer`` chooses the fact extractor and is passed straight to
    ``store.add``: ``True`` forces LLM extraction, ``False`` stores raw with no
    LLM (the 0-cost testing path), ``None`` follows the configured
    EXTRACTION_MODE.
    A per-conversation failure is counted and logged via ``on_progress`` but does
    not abort the run — one bad conversation shouldn't sink a whole import.
    ``store`` and ``scope_fn`` are injected so tests never touch a real backend
    or the network.
    """
    convs = _limited(conversations, limit)
    total = len(convs)
    facts = skipped = failures = 0
    for i, conv in enumerate(convs, 1):
        if not conv.messages:
            skipped += 1
            continue
        resolved_scope = scope if scope is not None else scope_fn(conv)
        messages = [{"role": m.role, "content": m.text} for m in conv.messages]
        try:
            result = store.add(
                messages,
                user_id=user_id,
                scope=resolved_scope,
                extra_metadata={"source": conv.source, "source_id": conv.source_id},
                infer=infer,
            )
            n = _facts_count(result)
            facts += n
        except TenantIsolationError:
            # A blank/invalid user_id is a whole-run config error, not a
            # per-item data problem — its own guard says never paper over it,
            # and user_id is constant across the batch. Fail fast rather than
            # mis-counting every conversation as a generic "failure".
            raise
        except Exception:
            # Per-item failure (a bad/oversized conversation, a transient store
            # error): log it so it's diagnosable — a bare count gives no clue
            # which conversation or why — then carry on with the rest.
            logger.exception(
                "backfill: store.add failed for source_id=%r; counting as failure",
                conv.source_id,
            )
            failures += 1
            n = 0
        if on_progress is not None:
            on_progress(i, total, conv, resolved_scope, n)
    return BackfillResult(
        conversations=total,
        facts_added=facts,
        skipped=skipped,
        failures=failures,
    )
