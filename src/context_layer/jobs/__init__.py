"""Long passes over a user's store, as durable work rather than live threads.

A sweep is minutes of wall time and one model call per memory. Held in a
thread, it belongs to whichever process served the click that started it: a
deploy kills it silently, the progress the page was showing dies with it, and
the only way forward is to pay for the whole store again. Held as a row, it
belongs to the account — a worker picks it up, heartbeats while it works, and
another worker finishes it if the first one goes away (VC-98).

This layer owns the lifecycle and nothing about memories. What a row means,
which rows a pass covers, and what judging one does are supplied by the
feature that owns the question — the consent layer for scope tagging, the
curation layer for retention — as a :class:`~context_layer.jobs.worker.SweepHandler`.
That is what keeps this shared without tying those two layers to each other.

It does not decide WHEN to sweep. A run is enqueued because someone asked;
the worker only ever finishes work already requested, so no memory reaches a
model because a container booted.
"""

from context_layer.jobs.runs import (
    ERROR_ALL_FAILED,
    STALE_AFTER,
    STATE_DONE,
    STATE_ERROR,
    STATE_QUEUED,
    STATE_RUNNING,
    RunStore,
    SweepRun,
    fingerprint_of,
)
from context_layer.jobs.worker import Pass, SweepHandler, SweepWorker

__all__ = [
    "ERROR_ALL_FAILED",
    "STALE_AFTER",
    "STATE_DONE",
    "STATE_ERROR",
    "STATE_QUEUED",
    "STATE_RUNNING",
    "Pass",
    "RunStore",
    "SweepHandler",
    "SweepRun",
    "SweepWorker",
    "fingerprint_of",
]
