"""Where a triage verdict actually becomes a memory's retention state.

One entry point: the user-triggered pass over the whole store, from the
dashboard's POST /dashboard/retention. It walks every memory on a background
thread and publishes progress the page can render, like the scope sweep.

There is deliberately NO on-write pass to match the scope classifier's. A
memory that was just written has no track record to judge — the user said
something a minute ago and a client decided it was worth storing, which is
the worst possible moment to ask whether it will still matter. Triage earns
its answer from a store that has accumulated, so it runs when the user asks
for it, over everything at once.

It converges, as the dashboard's per-memory buttons do, on the metadata
:mod:`~context_layer.curation.retention` defines, through
:func:`retention_updates` — which decides what to write for one memory, under
one rule: the retention state is DERIVED —
recomputable from the text, reversible, never the source of truth — except
where the user has set it themselves, which is intent and survives every
sweep in both directions.

The thread-plus-status machinery here mirrors
``consent.tagging.SweepRunner`` closely, and stays a separate implementation
on purpose: the two report different things (memories set aside and restored
versus tags changed), and a shared base class would tie the consent and
curation layers together through a third module for the sake of forty lines
of locking — against the whole point of keeping each layer extractable on its
own.
"""

import logging
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from context_layer.curation.retention import (
    SOURCE_LLM,
    STATE_ARCHIVED,
    STATE_KEEP,
    STATE_KEY,
    decided_by_user,
    retention_state,
    state_updates,
)
from context_layer.curation.triage import Verdict, triage

logger = logging.getLogger("context_layer.curation.sweep")

# TriageStatus.error when every memory in a pass failed on its own — nearly
# always credentials or model configuration rather than the memories. The
# dashboard branches on this exact value to say so.
TRIAGE_ERROR_ALL_FAILED = "all_failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def retention_updates(metadata: Optional[dict], verdict: Verdict) -> dict[str, str]:
    """The metadata keys to write for one memory; empty when nothing changes.

    - A memory whose state the user set by hand is left exactly as it is. A
      sweep never overrules a person about their own store, in either
      direction: what they kept stays kept, what they set aside stays aside.
    - An archive verdict on an already-archived memory writes nothing. It is
      already where it belongs, and rewriting the reason would cost a store
      write and a re-embed to change a sentence nobody is reading.
    - A keep verdict writes only when it RETRACTS an earlier archive. Keep is
      the state of every memory that has no retention keys at all, so
      stamping it on a whole store would mean a write per memory to record the
      default.

    Returning only genuine changes is what keeps a second pass over an
    unchanged store from rewriting every row.
    """
    if decided_by_user(metadata):
        return {}
    current = retention_state(metadata)
    if verdict.archived:
        if current == STATE_ARCHIVED:
            return {}
        return state_updates(STATE_ARCHIVED, SOURCE_LLM, verdict.reason)
    if current == STATE_ARCHIVED:
        # The model changed its mind, or the memory's text did. Written with an
        # empty reason so the old one stops showing next to a restored memory.
        return state_updates(STATE_KEEP, SOURCE_LLM)
    return {}


@dataclass(frozen=True)
class TriageCounts:
    """How one pass over a batch of rows went.

    ``archived`` and ``restored`` are counted apart rather than summed into
    "changed": a pass that sets 40 memories aside and one that puts 40 back
    are the same size and opposite events, and the user is owed the
    difference. ``failed`` counts memories that got no decision at all,
    whatever the reason — the triage call failed, or the write did.
    """

    archived: int = 0
    restored: int = 0
    failed: int = 0


def triage_rows(
    store,
    user_id: str,
    rows: Sequence[dict],
    *,
    on_progress: Optional[Callable[[], None]] = None,
) -> TriageCounts:
    """Judge each row and write what changed; returns the tally.

    One memory's failure — a call that failed, a write that lost a race with a
    delete — is logged and counted rather than abandoning the rest of the pass.
    """
    archived = 0
    restored = 0
    failed = 0
    for row in rows:
        memory_id = str(row.get("id") or "")
        text = str(row.get("memory") or row.get("text") or "")
        try:
            if memory_id and text:
                metadata = row.get("metadata")
                updates = retention_updates(metadata, triage(text))
                if updates:
                    store.update_metadata(memory_id, updates, user_id)
                    if updates[STATE_KEY] == STATE_ARCHIVED:
                        archived += 1
                    else:
                        restored += 1
        except Exception:
            failed += 1
            logger.exception("failed to triage memory %r for user=%s", memory_id, user_id)
        if on_progress is not None:
            on_progress()
    return TriageCounts(archived=archived, restored=restored, failed=failed)


@dataclass
class TriageStatus:
    """What the dashboard shows about a user's triage pass.

    In-process and deliberately not persisted, like the scope sweep's status:
    it describes a running thread in THIS worker, a restart genuinely has no
    pass running, and losing it costs nothing because the retention states it
    produced are in the store and the pass is re-runnable at will.
    """

    state: str = "idle"  # idle | running | done | error
    total: int = 0
    processed: int = 0
    archived: int = 0
    restored: int = 0
    failed: int = 0
    started_at: str = ""
    finished_at: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class TriageRunner:
    """Per-user triage threads plus the status the page renders.

    At most one pass per user at a time: a second POST while one is running is
    a no-op rather than a second walk over the store, so an impatient click
    can't double the API calls. Statuses are keyed by user id, so one tenant's
    pass is never visible to — or blocked by — another's.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._statuses: dict[str, TriageStatus] = {}

    def status(self, user_id: str) -> TriageStatus:
        with self._lock:
            return self._statuses.get(user_id) or TriageStatus()

    def is_running(self, user_id: str) -> bool:
        return self.status(user_id).state == "running"

    def start(self, store, user_id: str) -> bool:
        """Kick off a pass; False if one is already running for this user.

        Claims the slot under the lock before the thread starts, so two
        near-simultaneous POSTs can't both see "idle" and both spawn.
        """
        with self._lock:
            existing = self._statuses.get(user_id)
            if existing is not None and existing.state == "running":
                return False
            self._statuses[user_id] = TriageStatus(state="running", started_at=_now())
        thread = threading.Thread(
            target=self._run,
            args=(store, user_id),
            name=f"memory-triage-{user_id}",
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

    def _run(self, store, user_id: str) -> None:
        try:
            # Archived memories are read back in too: this is the pass that can
            # retract its own earlier verdict, and skipping them would make
            # every archive permanent until a human intervened.
            rows = store.all(user_id)
            self._update(user_id, total=len(rows))
            counts = triage_rows(
                store, user_id, rows, on_progress=lambda: self._bump_processed(user_id)
            )
            # Every memory failing is a broken triage call, not a store that
            # was already exactly right; anything less still reports what landed.
            everything_failed = bool(rows) and counts.failed == len(rows)
            self._update(
                user_id,
                state="error" if everything_failed else "done",
                archived=counts.archived,
                restored=counts.restored,
                failed=counts.failed,
                error=TRIAGE_ERROR_ALL_FAILED if everything_failed else "",
                finished_at=_now(),
            )
        except Exception as exc:
            logger.exception("triage pass failed for user=%s", user_id)
            self._update(
                user_id, state="error", error=type(exc).__name__, finished_at=_now()
            )


_runner = TriageRunner()


def get_triage_runner() -> TriageRunner:
    """The process-wide runner the dashboard starts and reads."""
    return _runner


__all__ = [
    "TRIAGE_ERROR_ALL_FAILED",
    "TriageCounts",
    "TriageRunner",
    "TriageStatus",
    "get_triage_runner",
    "retention_updates",
    "triage_rows",
]
