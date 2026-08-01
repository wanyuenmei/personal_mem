"""How a memory's retention state lives inside its mem0 metadata.

Three scalar metadata keys, written together and read as one decision:

- ``retention_state`` — ``keep`` or ``archived``. ABSENT MEANS KEEP: every
  memory written before this existed, and every memory no pass has looked at
  yet, reads as kept. Nothing disappears by omission, and an unrecognized
  value reads as kept too, so a buggy or future writer can never hide a
  memory by accident.
- ``retention_source`` — ``user`` or ``llm``, the same provenance idea the
  consent tags carry: what the user decided by hand is intent, not
  derivation, and a sweep leaves it alone in both directions (see
  :func:`context_layer.curation.sweep.retention_updates`).
- ``retention_reason`` — the model's one line on why it set a memory aside.
  Stored so the dashboard can show the user what a machine decided about
  their own store, rather than a state with no explanation.

Scalar values under known keys, not a nested object, for the reason
``consent.tags`` spells out: mem0's pgvector filter builder compares
``payload->>'<key>'`` text, so a future read path can filter archived
memories in the query rather than after it, with no migration.

Unlike a consent tag, retention is single-valued, so a state change is an
overwrite rather than a tombstone — one key, two values, no key ever needs
removing. ``retention_reason`` is the exception that proves the rule: mem0's
update MERGES metadata and cannot delete a key, so restoring a memory
overwrites the reason with an empty string instead of removing it.
"""

from typing import Optional

STATE_KEY = "retention_state"
SOURCE_KEY = "retention_source"
REASON_KEY = "retention_reason"

STATE_KEEP = "keep"
STATE_ARCHIVED = "archived"

SOURCE_USER = "user"
SOURCE_LLM = "llm"

# A reason is a label on a card, not prose. Bounded because it is model-written
# text landing in stored metadata, and it is truncated rather than rejected —
# an over-long reason is still better than none.
REASON_MAX_CHARS = 200


def retention_state(metadata: Optional[dict]) -> str:
    """A memory's retention state; ``keep`` unless it is explicitly archived.

    Every unknown value collapses to ``keep`` on purpose: the failure mode of
    this function is either "a memory the user meant to hide is visible" or "a
    memory the user has is invisible to them", and only the first is
    recoverable by looking at the dashboard.
    """
    if not metadata:
        return STATE_KEEP
    return STATE_ARCHIVED if metadata.get(STATE_KEY) == STATE_ARCHIVED else STATE_KEEP


def is_archived(metadata: Optional[dict]) -> bool:
    """Whether this memory has been set aside — the read paths' filter."""
    return retention_state(metadata) == STATE_ARCHIVED


def decided_by_user(metadata: Optional[dict]) -> bool:
    """Whether the user set this memory's state themselves.

    True for a memory they kept as much as one they archived: both directions
    are intent, and a sweep must not re-decide either.
    """
    return bool(metadata) and metadata.get(SOURCE_KEY) == SOURCE_USER


def retention_reason(metadata: Optional[dict]) -> str:
    """Why this memory was set aside, if whoever set it aside said."""
    if not metadata:
        return ""
    value = metadata.get(REASON_KEY)
    return value if isinstance(value, str) else ""


def state_updates(state: str, source: str, reason: str = "") -> dict[str, str]:
    """The metadata to write for one retention decision.

    Always all three keys. ``retention_reason`` is written even when empty
    because mem0 cannot remove a metadata key: restoring a memory the model
    archived has to overwrite the reason it gave, or the dashboard would keep
    showing "set aside because …" next to a memory that is no longer set
    aside.
    """
    return {
        STATE_KEY: state,
        SOURCE_KEY: source,
        REASON_KEY: reason[:REASON_MAX_CHARS],
    }
