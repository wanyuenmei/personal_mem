"""How consent-scope tags live inside a memory's mem0 metadata.

A memory's tags are stored as ONE metadata key per scope — ``cs_<scope-key>``
with a provenance string as the value — never as a JSON array of scope keys.
That shape is load-bearing: mem0's pgvector filter builder compares
``payload->>'<key>'`` text, so a filter can match a scalar value under a known
key but silently never matches a value inside an array. One key per scope
means future grant enforcement can filter memories pre-ranking with the
filter builder as it is, no migration.

Provenance values:

- ``user`` — the user tagged this memory themselves (dashboard). Manual tags
  survive classifier re-sweeps.
- ``llm`` — the out-of-band classifier (VC-88) applied the tag. Rebuildable:
  a re-sweep may add or replace ``llm`` tags freely.
- ``user_removed`` — the user removed the tag. A tombstone rather than a key
  deletion, for two reasons: the classifier must never re-apply a tag the
  user vetoed (a deleted key would just get re-added on the next sweep), and
  mem0's ``update`` merges metadata into the existing payload — keys can be
  set but never removed through it. Reads as untagged everywhere.
"""

from typing import Optional

TAG_PREFIX = "cs_"

PROVENANCE_USER = "user"
PROVENANCE_LLM = "llm"
PROVENANCE_USER_REMOVED = "user_removed"

# The provenance values under which a tag counts as "this memory carries this
# scope". Anything else — the user_removed tombstone, or a malformed value a
# buggy writer left behind — reads as untagged.
_ACTIVE_PROVENANCES = (PROVENANCE_USER, PROVENANCE_LLM)


def tag_key(scope_key: str) -> str:
    """The metadata key a scope's tag lives under: ``cs_<scope-key>``."""
    return f"{TAG_PREFIX}{scope_key}"


def active_tags(metadata: Optional[dict]) -> dict[str, str]:
    """The scopes a memory actively carries: ``{scope_key: provenance}``.

    Reads the ``cs_*`` keys out of a mem0 metadata dict, dropping the prefix.
    Only string values with an active provenance count — ``user_removed``
    reads as untagged, and any non-string value (e.g. an array some other
    writer produced) is ignored rather than trusted.
    """
    if not metadata:
        return {}
    return {
        key[len(TAG_PREFIX):]: value
        for key, value in metadata.items()
        if key.startswith(TAG_PREFIX) and value in _ACTIVE_PROVENANCES
    }
