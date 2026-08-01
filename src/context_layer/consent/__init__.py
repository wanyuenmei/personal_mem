"""Consent layer: the scope vocabulary that consent decisions are made in.

A consent scope is a named category of personal context ("dietary",
"travel") that memories can later be tagged with and, eventually, shared
under. Scopes are defined per owner — each third party registers the list it
cares about, and the user can add their own — rather than as one global
taxonomy (VC-86). Memories carry their tags as one ``cs_<scope-key>``
metadata key per scope with a provenance value (VC-87) — see ``tags``.
Tags are assigned by hand from the dashboard or derived by the ``classifier``
and written by ``tagging``, which keeps that work off every hot path (VC-88).
An empty vocabulary makes all of that a no-op, so ``discovery`` proposes a
starting set of the user's own scopes from their memories, for the user to
approve (VC-92).
"""

from context_layer.consent.classifier import (
    ClassificationFailed,
    classifier_enabled,
    classify,
)
from context_layer.consent.discovery import (
    MAX_SAMPLE_MEMORIES,
    DiscoveryFailed,
    ProposalHolder,
    ScopeProposal,
    get_proposal_holder,
    suggest_scopes,
)
from context_layer.consent.registry import (
    DESCRIPTION_MAX_CHARS,
    RESERVED_OWNER_SLUG,
    SLUG_MAX_CHARS,
    ConsentScope,
    ScopeRegistry,
    slugify,
)
from context_layer.consent.tagging import (
    SweepStatus,
    get_sweep_runner,
    tag_new_memories,
    tag_updates,
)
from context_layer.consent.tags import (
    PROVENANCE_LLM,
    PROVENANCE_LLM_CLEARED,
    PROVENANCE_USER,
    PROVENANCE_USER_REMOVED,
    active_tags,
    tag_key,
)

__all__ = [
    "DESCRIPTION_MAX_CHARS",
    "MAX_SAMPLE_MEMORIES",
    "PROVENANCE_LLM",
    "PROVENANCE_LLM_CLEARED",
    "PROVENANCE_USER",
    "PROVENANCE_USER_REMOVED",
    "RESERVED_OWNER_SLUG",
    "SLUG_MAX_CHARS",
    "ClassificationFailed",
    "ConsentScope",
    "DiscoveryFailed",
    "ProposalHolder",
    "ScopeProposal",
    "ScopeRegistry",
    "SweepStatus",
    "active_tags",
    "classifier_enabled",
    "classify",
    "get_proposal_holder",
    "get_sweep_runner",
    "slugify",
    "suggest_scopes",
    "tag_key",
    "tag_new_memories",
    "tag_updates",
]
