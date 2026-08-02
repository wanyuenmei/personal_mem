"""Where classifier output actually becomes tags — off both hot paths.

Two entry points, one reconcile rule:

- :class:`ScopeTaggingHandler` — the user-triggered full re-sweep, enqueued
  by the dashboard's POST /dashboard/sweep and executed by the job worker as a
  durable run, so a deploy mid-pass resumes instead of starting over (VC-98).
  This is also the rebuild path after any vocabulary change: scopes have no
  history, so re-deriving from the text is the only way tags catch up with a
  newly registered scope.
- :func:`tag_new_memories` — the on-write pass. ``add_memory`` fires it on a
  daemon thread and returns immediately; a classifier failure or a slow API
  call can never fail or delay the write that triggered it.

Both converge on :func:`tag_updates`, which decides what to write for one
memory. Tags stay a DERIVED index — recomputed from the text, rebuildable,
never the source of truth — with one exception that is the whole point of the
provenance field: what the user did by hand is intent, not derivation, and
survives every sweep. Concretely the classifier never touches a ``user`` tag
or a ``user_removed`` tombstone, and retracts its own stale tags by writing
``llm_cleared`` rather than deleting a key (mem0's update merges metadata and
cannot remove keys).
"""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from context_layer.consent.classifier import classifier_enabled, classify
from context_layer.consent.registry import ConsentScope, ScopeRegistry
from context_layer.consent.tags import (
    PROVENANCE_LLM,
    PROVENANCE_LLM_CLEARED,
    USER_OWNED_PROVENANCES,
    tag_key,
)
from context_layer.jobs.runs import fingerprint_of
from context_layer.jobs.worker import Pass

logger = logging.getLogger("context_layer.consent.tagging")

# Records which vocabulary a memory was last classified against. Deliberately
# NOT under the ``cs_`` prefix: everything there is a scope tag with a
# provenance value, and a key that looked like one but held a hash would be a
# trap for the next person reading a payload — even though ``active_tags``
# would filter it out today.
SWEPT_KEY = "scope_swept_fp"


def scope_fingerprint(scopes: Sequence[ConsentScope]) -> str:
    """What "already classified" means, as a hash of the whole vocabulary.

    Descriptions are in it, not just keys: the classifier puts them in the
    prompt, so editing one can change which memories a scope claims and every
    memory is owed a re-think. Sorted, so the same vocabulary hashes the same
    however the registry happened to return it.
    """
    return fingerprint_of(
        *sorted(f"{scope.key}={scope.description}" for scope in scopes)
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def tag_updates(
    metadata: Optional[dict],
    scopes: Sequence[ConsentScope],
    applicable: Sequence[str],
) -> dict[str, str]:
    """The metadata keys to write for one memory; empty when nothing changes.

    ``applicable`` is what the classifier just decided. For each registered
    scope:

    - a ``user`` or ``user_removed`` value is left exactly as it is — user
      intent outranks the classifier in both directions;
    - a scope the classifier picked becomes ``llm``;
    - a scope it did NOT pick is retracted to ``llm_cleared`` only if this
      classifier had previously tagged it. Scopes it never set are skipped
      entirely, so metadata doesn't grow a key per scope per memory.

    Returning only genuine changes keeps a re-sweep of an unchanged store from
    issuing a write (and a re-embed) per memory.
    """
    metadata = metadata or {}
    wanted = set(applicable)
    updates: dict[str, str] = {}
    for scope in scopes:
        key = tag_key(scope.key)
        current = metadata.get(key)
        if current in USER_OWNED_PROVENANCES:
            continue
        if scope.key in wanted:
            new = PROVENANCE_LLM
        elif current == PROVENANCE_LLM:
            new = PROVENANCE_LLM_CLEARED
        else:
            continue
        if new != current:
            updates[key] = new
    return updates


@dataclass(frozen=True)
class TagCounts:
    """How one pass over a batch of rows went.

    ``failed`` counts memories that could not be tagged at all, whatever the
    reason — the classifier call failed, or the write did. Both land in the
    same place from the user's side ("this memory didn't get tagged"), and
    both belong in the count that stops a sweep reading as a clean success.
    A row with no id or no text is neither: there was nothing to do.
    """

    changed: int = 0
    failed: int = 0


def tag_one(
    store,
    user_id: str,
    row: dict,
    scopes: Sequence[ConsentScope],
    fingerprint: str,
) -> bool:
    """Classify one memory and write its tags. True if a tag actually changed.

    The write always happens when there is something to classify, even when no
    tag changed, because it also carries the stamp saying which vocabulary this
    memory has now been judged against. That stamp is what lets the next pass
    skip it instead of paying for the same answer again — one metadata write
    now against one model call every future sweep.

    Raises whatever the classifier or the store raised. Callers count the
    failure and move on: one unreachable memory must not end a pass.
    """
    memory_id = str(row.get("id") or "")
    text = str(row.get("memory") or row.get("text") or "")
    if not memory_id or not text:
        return False
    changes = tag_updates(row.get("metadata"), scopes, classify(text, scopes))
    store.update_metadata(memory_id, {**changes, SWEPT_KEY: fingerprint}, user_id)
    return bool(changes)


def tag_rows(
    store,
    user_id: str,
    scopes: Sequence[ConsentScope],
    rows: Sequence[dict],
    *,
    on_progress: Optional[Callable[[], None]] = None,
) -> TagCounts:
    """Classify and tag each row; returns what changed and what failed.

    The on-write path's loop. The full sweep no longer comes through here — it
    is a durable run the job worker walks (see ``jobs.worker``), so that it can
    survive the process it started in — but a handful of just-written memories
    are worth doing inline on the thread that already has them.

    One memory's failure — a classification call that failed, a write that
    lost a race with a delete — is logged and counted rather than abandoning
    the rest.
    """
    fingerprint = scope_fingerprint(scopes)
    changed = 0
    failed = 0
    for row in rows:
        try:
            if tag_one(store, user_id, row, scopes, fingerprint):
                changed += 1
        except Exception:
            failed += 1
            logger.exception(
                "failed to tag memory %r for user=%s",
                str(row.get("id") or ""), user_id,
            )
        if on_progress is not None:
            on_progress()
    return TagCounts(changed=changed, failed=failed)


# --- the user-triggered full sweep ----------------------------------------


class ScopeTaggingHandler:
    """What a scope-tagging run is, for the job worker that executes it.

    The sweep used to be a thread this module owned. It is now a durable run
    (see ``jobs.runs``) and this is only the part that is specific to tagging:
    what the pass is judging against, which memories it covers, which are
    already current, and what doing one means. Everything else — claiming the
    run, heartbeating, counting, resuming it after the process died — belongs
    to the worker and is the same for every kind of pass.
    """

    kind = "scope_tagging"

    def __init__(self, registry: ScopeRegistry) -> None:
        self._registry = registry

    def prepare(self, store, user_id: str) -> Pass:
        """Read the vocabulary ONCE, and settle the whole attempt against it.

        Not once per row: the registry opens a connection per call, so
        re-reading would add a round trip per memory. More importantly it would
        make the pass incoherent — a scope registered halfway through would be
        tagged onto later memories while every one of them is stamped with the
        fingerprint of the vocabulary as it was at the start.

        With no vocabulary there are no rows. Tags ARE scopes: a pass could
        only ever decide "nothing applies" for every memory, at the price of a
        model call each. The run still completes, reporting the zero scopes it
        had to sort into — which is what tells the page to say so rather than
        claim a clean sweep (VC-90).
        """
        scopes = self._registry.all(user_id)
        return Pass(
            fingerprint=scope_fingerprint(scopes),
            rows=store.all(user_id) if scopes else [],
            # A fact about the input rather than the outcome, so it is known
            # before row one and stays true of a stored result afterwards.
            detail={"scope_count": len(scopes)},
            context=scopes,
        )

    def is_current(self, row: dict, fingerprint: str) -> bool:
        return (row.get("metadata") or {}).get(SWEPT_KEY) == fingerprint

    def handle(self, store, user_id: str, row: dict, plan: Pass):
        changed = tag_one(store, user_id, row, plan.context, plan.fingerprint)
        return {"changed": 1} if changed else {}


# --- the on-write pass -----------------------------------------------------


def _new_memory_ids(result) -> list[str]:
    """The ids mem0 just created or rewrote, out of an ``add`` result.

    mem0 returns ``{"results": [{"id", "memory", "event"}]}`` (or a bare
    list). Only ADD and UPDATE change what a memory says; DELETE and NONE
    leave nothing new to classify.
    """
    rows = result.get("results", []) if isinstance(result, dict) else result
    if not isinstance(rows, list):
        return []
    ids = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        event = str(row.get("event") or "ADD").upper()
        memory_id = str(row.get("id") or "")
        if memory_id and event in ("ADD", "UPDATE"):
            ids.append(memory_id)
    return ids


def _tag_ids(store, registry: ScopeRegistry, user_id: str, ids: Sequence[str]) -> None:
    """Classify exactly ``ids`` and write their tags.

    Re-reads the rows through ``store.all`` rather than trusting the add
    result: the result carries the extracted text but not the metadata, and an
    UPDATE event lands on a memory that may already hold user tags this pass
    must not clobber. One extra read per write, on a background thread, buys
    the same reconcile rule as the full sweep.
    """
    wanted = set(ids)
    scopes = registry.all(user_id)
    if not scopes:
        return
    rows = [row for row in store.all(user_id) if str(row.get("id") or "") in wanted]
    if rows:
        tag_rows(store, user_id, scopes, rows)


def tag_new_memories(
    store, user_id: str, result, registry: Optional[ScopeRegistry] = None
) -> Optional[threading.Thread]:
    """Tag whatever ``add`` just wrote, on a daemon thread. Never raises.

    Called from ``add_memory`` after a successful write. Returns the thread
    (tests join it) or None when there is nothing to do — no classification
    configured, or nothing added. Daemon so a shutdown mid-classification
    doesn't hang the process: the sweep can always rebuild what was missed.
    """
    try:
        if not classifier_enabled():
            return None
        ids = _new_memory_ids(result)
        if not ids:
            return None
        if registry is None:
            # Lazily resolved so this module doesn't import the tool layer at
            # import time; it is the same registry the dashboard reads.
            from context_layer.tools.consent_tools import get_registry

            registry = get_registry()

        def run() -> None:
            try:
                _tag_ids(store, registry, user_id, ids)
            except Exception:
                logger.exception("on-write scope tagging failed for user=%s", user_id)

        thread = threading.Thread(
            target=run, name="scope-tag-on-write", daemon=True
        )
        thread.start()
        return thread
    except Exception:
        # Belt and braces: this runs inside the add_memory success path, and
        # nothing here is allowed to turn a saved memory into a failed call.
        logger.exception("could not start on-write scope tagging for user=%s", user_id)
        return None
