"""Consent layer: the scope vocabulary that consent decisions are made in.

A consent scope is a named category of personal context ("dietary",
"travel") that memories can later be tagged with and, eventually, shared
under. Scopes are defined per owner — each third party registers the list it
cares about, and the user can add their own — rather than as one global
taxonomy (VC-86). Memories carry their tags as one ``cs_<scope-key>``
metadata key per scope with a provenance value (VC-87) — see ``tags``.
Tags are assigned by hand from the dashboard or derived by the ``classifier``
and written by ``tagging``, which keeps that work off every hot path (VC-88).
``discovery`` is where the vocabulary itself can come from: it proposes scopes
from the memories already stored, for the user to approve one by one (VC-92).
"""

from context_layer.consent.classifier import classifier_enabled, classify
from context_layer.consent.discovery import (
    ScopeProposal,
    SuggestionStatus,
    get_suggestion_runner,
    register_proposals,
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
    "PROVENANCE_LLM",
    "PROVENANCE_LLM_CLEARED",
    "PROVENANCE_USER",
    "PROVENANCE_USER_REMOVED",
    "RESERVED_OWNER_SLUG",
    "SLUG_MAX_CHARS",
    "ConsentScope",
    "ScopeProposal",
    "ScopeRegistry",
    "SuggestionStatus",
    "SweepStatus",
    "active_tags",
    "classifier_enabled",
    "classify",
    "get_suggestion_runner",
    "get_sweep_runner",
    "register_proposals",
    "slugify",
    "suggest_scopes",
    "tag_key",
    "tag_new_memories",
    "tag_updates",
]
