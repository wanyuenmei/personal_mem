"""Curation layer: keeping the store worth reading.

A connected client writes down whatever it decides is worth remembering, and
over a few months that is a lot of things that were true for an afternoon.
They cost nothing to store and quite a lot to keep: every one of them is a
candidate result in `search_memory`, which is the surface an assistant
actually builds its answers from.

So every memory carries a retention state — kept, or archived — in its mem0
metadata (``retention``), decided either by the user or by an out-of-band LLM
pass over the whole store (``triage``, applied by ``sweep``) that asks one
question of each memory: would knowing this change a decision later?

Archived means SET ASIDE, never deleted (VC-94). An archived memory stops
coming back from `search_memory`, and stays in the dashboard in its own
section with the reason it was set aside and a one-click restore. Deletion is
a separate act the user takes deliberately (VC-57/VC-41), and nothing in this
layer performs one.
"""

from context_layer.curation.retention import (
    REASON_KEY,
    REASON_MAX_CHARS,
    SOURCE_KEY,
    SOURCE_LLM,
    SOURCE_USER,
    STATE_ARCHIVED,
    STATE_KEEP,
    STATE_KEY,
    decided_by_user,
    is_archived,
    retention_reason,
    retention_state,
    state_updates,
)
from context_layer.curation.sweep import (
    TRIAGE_ERROR_ALL_FAILED,
    TriageCounts,
    TriageRunner,
    TriageStatus,
    get_triage_runner,
    retention_updates,
    triage_rows,
)
from context_layer.curation.triage import (
    KEEP,
    MAX_TRIAGE_CHARS,
    TriageFailed,
    Verdict,
    triage,
    triage_enabled,
)

__all__ = [
    "KEEP",
    "MAX_TRIAGE_CHARS",
    "REASON_KEY",
    "REASON_MAX_CHARS",
    "SOURCE_KEY",
    "SOURCE_LLM",
    "SOURCE_USER",
    "STATE_ARCHIVED",
    "STATE_KEEP",
    "STATE_KEY",
    "TRIAGE_ERROR_ALL_FAILED",
    "TriageCounts",
    "TriageFailed",
    "TriageRunner",
    "TriageStatus",
    "Verdict",
    "decided_by_user",
    "get_triage_runner",
    "is_archived",
    "retention_reason",
    "retention_state",
    "retention_updates",
    "state_updates",
    "triage",
    "triage_enabled",
    "triage_rows",
]
