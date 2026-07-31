"""Consent layer: the scope vocabulary that consent decisions are made in.

A consent scope is a named category of personal context ("dietary",
"travel") that memories can later be tagged with and, eventually, shared
under. Scopes are defined per owner — each third party registers the list it
cares about, and the user can add their own — rather than as one global
taxonomy (VC-86).
"""

from context_layer.consent.registry import (
    RESERVED_OWNER_SLUG,
    SLUG_MAX_CHARS,
    ConsentScope,
    ScopeRegistry,
    slugify,
)

__all__ = [
    "RESERVED_OWNER_SLUG",
    "SLUG_MAX_CHARS",
    "ConsentScope",
    "ScopeRegistry",
    "slugify",
]
