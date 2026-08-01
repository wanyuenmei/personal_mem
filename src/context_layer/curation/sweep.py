"""Where a triage verdict actually becomes a memory's retention state.

One entry point: the user-triggered pass over the whole store, enqueued by
the dashboard's POST /dashboard/retention and executed by the job worker as a
durable run, like the scope sweep.

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

This module used to carry its own thread-and-status machinery, mirroring the
scope sweep's, and said so on purpose: the two report different things, and a
shared base class was not worth tying the consent and curation layers together
through a third module for forty lines of locking.

That reasoning was right about the layering and wrong about the size. What is
shared is not locking — it is a run that survives the process executing it: a
persisted record, a claim, a heartbeat, resumption after a crash, and the rule
that already-judged memories are skipped rather than paid for again (VC-98).
Duplicating that is duplicating the hard part, and it means fixing the same
bug twice.

The layering concern still holds, and is answered rather than ignored: the
shared machinery lives in ``jobs``, a layer of its own that knows nothing
about memories, and this module depends on it the way it already depends on
the store. What stays here is what is genuinely curation's — the question
triage asks, and the fact that setting a memory aside and putting one back are
opposite events worth counting apart.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

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
from context_layer.jobs.runs import fingerprint_of
from context_layer.jobs.worker import Pass

logger = logging.getLogger("context_layer.curation.sweep")

# What triage judges against. There is no per-user vocabulary here as there is
# for scopes — the question the pass asks is fixed — so this stands in for one:
# bump it when the prompt changes and the next pass re-judges the whole store
# instead of skipping everything it has already seen.
POLICY_VERSION = "triage-v1"

# Records the policy a memory was last judged under, so a repeat pass can skip
# it. Sits beside the retention keys rather than among them: it says when the
# question was last asked, not what the answer was.
SWEPT_KEY = "retention_swept_fp"


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


def triage_one(
    store, user_id: str, row: dict, fingerprint: str
) -> dict[str, int]:
    """Judge one memory and write what changed; returns what it counted as.

    The write always happens when there is something to judge, even when the
    verdict changes nothing, because it also carries the stamp saying this
    memory has been judged under the current policy. That stamp is what lets
    the next pass skip it rather than pay for the same verdict again.

    Raises whatever the triage call or the store raised — the worker counts
    the failure and carries on to the next memory.
    """
    memory_id = str(row.get("id") or "")
    text = str(row.get("memory") or row.get("text") or "")
    if not memory_id or not text:
        return {}
    changes = retention_updates(row.get("metadata"), triage(text))
    store.update_metadata(memory_id, {**changes, SWEPT_KEY: fingerprint}, user_id)
    if not changes:
        return {}
    moved = "archived" if changes[STATE_KEY] == STATE_ARCHIVED else "restored"
    # Counted both ways: ``changed`` is what every kind of pass reports, and
    # the archived/restored split is what this one owes the user — 40 memories
    # set aside and 40 put back are the same size and opposite events.
    return {"changed": 1, moved: 1}


class RetentionHandler:
    """What a triage run is, for the job worker that executes it.

    Only the part specific to retention: what the pass is judging against,
    which memories it covers, which have already been judged, and what judging
    one does. The lifecycle around that — claim, heartbeat, counters, resume
    after a crash — is the worker's, and identical to the scope sweep's.
    """

    kind = "retention"

    def prepare(self, store, user_id: str) -> Pass:
        """Settle the attempt: the policy it judges under, and every memory.

        Archived memories are read back in — this is the pass that can retract
        its own earlier verdict, and skipping them would make every archive
        permanent until a human intervened.
        """
        return Pass(
            fingerprint=fingerprint_of(POLICY_VERSION),
            rows=store.all(user_id),
            detail={"archived": 0, "restored": 0},
        )

    def is_current(self, row: dict, fingerprint: str) -> bool:
        return (row.get("metadata") or {}).get(SWEPT_KEY) == fingerprint

    def handle(self, store, user_id: str, row: dict, plan: Pass):
        return triage_one(store, user_id, row, plan.fingerprint)


__all__ = [
    "POLICY_VERSION",
    "SWEPT_KEY",
    "RetentionHandler",
    "retention_updates",
    "triage_one",
]
