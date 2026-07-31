"""Consent layer: the scope vocabulary that consent decisions are made in.

A consent scope is a named category of personal context ("dietary",
"travel") that memories can later be tagged with and, eventually, shared
under. Scopes are defined per owner — each third party registers the list it
cares about, and the user can add their own — rather than as one global
taxonomy (VC-86). Memories carry their tags as one ``cs_<scope-key>``
metadata key per scope with a provenance value (VC-87) — see ``tags``.
"""

from context_layer.consent.registry import (
    DESCRIPTION_MAX_CHARS,
    RESERVED_OWNER_SLUG,
    SLUG_MAX_CHARS,
    ConsentScope,
    ScopeRegistry,
    slugify,
)
from context_layer.consent.tags import (
    PROVENANCE_LLM,
    PROVENANCE_USER,
    PROVENANCE_USER_REMOVED,
    active_tags,
    tag_key,
)

__all__ = [
    "DESCRIPTION_MAX_CHARS",
    "PROVENANCE_LLM",
    "PROVENANCE_USER",
    "PROVENANCE_USER_REMOVED",
    "RESERVED_OWNER_SLUG",
    "SLUG_MAX_CHARS",
    "ConsentScope",
    "ScopeRegistry",
    "active_tags",
    "slugify",
    "tag_key",
]
