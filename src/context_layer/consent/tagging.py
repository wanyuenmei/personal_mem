"""Where classifier output actually becomes tags — off both hot paths.

Two entry points, one reconcile rule:

- :class:`SweepRunner` — the user-triggered full re-sweep, from the
  dashboard's POST /dashboard/sweep. Walks every memory on a background thread and
  publishes progress the page can render. This is also the rebuild path after
  any vocabulary change: scopes have no history, so re-deriving from the text
  is the only way tags catch up with a newly registered scope.
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
from dataclasses import asdict, dataclass
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

logger = logging.getLogger("context_layer.consent.tagging")

# SweepStatus.error when every memory in a sweep failed on its own — the one
# failure mode with a user-facing explanation rather than an exception type
# name, because it is nearly always credentials or model configuration. The
# dashboard branches on this exact value to render that explanation.
SWEEP_ERROR_ALL_FAILED = "all_failed"


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


def tag_rows(
    store,
    user_id: str,
    scopes: Sequence[ConsentScope],
    rows: Sequence[dict],
    *,
    on_progress: Optional[Callable[[], None]] = None,
) -> TagCounts:
    """Classify and tag each row; returns what changed and what failed.

    One memory's failure — a classification call that failed, a write that
    lost a race with a delete — is logged and counted rather than abandoning
    the rest of the sweep.
    """
    changed = 0
    failed = 0
    for row in rows:
        memory_id = str(row.get("id") or "")
        text = str(row.get("memory") or row.get("text") or "")
        try:
            if memory_id and text:
                updates = tag_updates(row.get("metadata"), scopes, classify(text, scopes))
                if updates:
                    store.update_metadata(memory_id, updates, user_id)
                    changed += 1
        except Exception:
            failed += 1
            logger.exception("failed to tag memory %r for user=%s", memory_id, user_id)
        if on_progress is not None:
            on_progress()
    return TagCounts(changed=changed, failed=failed)


# --- the user-triggered full sweep ----------------------------------------


@dataclass
class SweepStatus:
    """What the dashboard shows about a user's sweep.

    In-process and deliberately not persisted: it describes a running thread
    in THIS worker, and a restart genuinely has no sweep running. Losing it
    costs nothing — the sweep is re-runnable at will and converges to the same
    tags.

    ``error`` carries either the exception type that ended the whole sweep or
    ``SWEEP_ERROR_ALL_FAILED`` when every memory failed individually. The
    second is why ``failed`` exists: a sweep where every classification call
    was rejected used to finish as "done, 0 of N updated", indistinguishable
    from a store where nothing needed tagging.
    """

    state: str = "idle"  # idle | running | done | error
    # How many scopes the pass had to sort into: zero is "no categories to
    # classify into", which otherwise reads identically to a real pass that
    # matched nothing (both are 0 of 0). A count rather than a second terminal
    # state because ``state`` is the lifecycle and this is a fact about the
    # input — and because it stays true of a STORED result once the user does
    # register scopes, where a "no_scopes" state would have to be reread.
    scope_count: int = 0
    total: int = 0
    processed: int = 0
    changed: int = 0
    failed: int = 0
    started_at: str = ""
    finished_at: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class SweepRunner:
    """Per-user sweep threads plus the status the page renders.

    At most one sweep per user at a time: a second POST while one is running
    is a no-op rather than a second full pass over the store (the button is
    one click, and an impatient user shouldn't be able to multiply the API
    calls). Statuses are keyed by user id so one tenant's sweep is never
    visible to — or blocked by — another's.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._statuses: dict[str, SweepStatus] = {}

    def status(self, user_id: str) -> SweepStatus:
        with self._lock:
            return self._statuses.get(user_id) or SweepStatus()

    def is_running(self, user_id: str) -> bool:
        return self.status(user_id).state == "running"

    def start(self, store, registry: ScopeRegistry, user_id: str) -> bool:
        """Kick off a sweep; False if one is already running for this user.

        Claims the slot under the lock before the thread starts, so two
        near-simultaneous POSTs can't both see "idle" and both spawn.
        """
        with self._lock:
            existing = self._statuses.get(user_id)
            if existing is not None and existing.state == "running":
                return False
            self._statuses[user_id] = SweepStatus(state="running", started_at=_now())
        thread = threading.Thread(
            target=self._run,
            args=(store, registry, user_id),
            name=f"scope-sweep-{user_id}",
            daemon=True,
        )
        thread.start()
        return True

    def _update(self, user_id: str, **fields) -> None:
        with self._lock:
            status = self._statuses.get(user_id)
            if status is None:
                return
            for name, value in fields.items():
                setattr(status, name, value)

    def _bump_processed(self, user_id: str) -> None:
        with self._lock:
            status = self._statuses.get(user_id)
            if status is not None:
                status.processed += 1

    def _run(self, store, registry: ScopeRegistry, user_id: str) -> None:
        try:
            scopes = registry.all(user_id)
            rows = store.all(user_id) if scopes else []
            self._update(user_id, scope_count=len(scopes), total=len(rows))
            counts = tag_rows(
                store,
                user_id,
                scopes,
                rows,
                on_progress=lambda: self._bump_processed(user_id),
            )
            # Every memory failing is a broken classifier, not a sweep with
            # nothing to do; anything less still reports what did land.
            everything_failed = bool(rows) and counts.failed == len(rows)
            self._update(
                user_id,
                state="error" if everything_failed else "done",
                changed=counts.changed,
                failed=counts.failed,
                error=SWEEP_ERROR_ALL_FAILED if everything_failed else "",
                finished_at=_now(),
            )
        except Exception as exc:
            logger.exception("scope sweep failed for user=%s", user_id)
            self._update(
                user_id,
                state="error",
                error=type(exc).__name__,
                finished_at=_now(),
            )


_sweeps = SweepRunner()


def get_sweep_runner() -> SweepRunner:
    """The process-wide sweep runner the dashboard starts and reads."""
    return _sweeps


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
