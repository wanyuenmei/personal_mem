"""The consent tool this server exposes: register_scopes.

Same shape as memory_tools: module-level functions (importable/testable) and a
register_consent_tools(mcp) that applies the FastMCP decoration, keeping tool
definitions decoupled from the composition root.

The party name is SELF-ASSERTED by the calling client — WorkOS MCP tokens
carry no verified client identity until token introspection (VC-65) lands —
which is why registrations are stored per user (the registry the tool writes
to is partitioned by the same user_id as the memory store) and why registering
scopes grants nothing: it only declares a vocabulary the user can see, tag
against, and later grant from. A client lying about its name can only mislabel
its own entry in its own user's registry.
"""

import logging
from typing import NotRequired, Optional, TypedDict

from mcp.server.fastmcp import Context, FastMCP

from context_layer.consent import (
    DESCRIPTION_MAX_CHARS,
    RESERVED_OWNER_SLUG,
    SLUG_MAX_CHARS,
    ScopeRegistry,
    slugify,
)
from context_layer.identity import resolve_user_id
from context_layer.observability import log_tool_call

logger = logging.getLogger("context_layer.tools")

_MAX_SCOPES_PER_CALL = 20
# Names and parties are capped at the slug cap: slugify() never lengthens its
# input, so nothing that passes validation can hit the slug truncation — which
# would silently collapse two distinct names onto one registry key.
_MAX_NAME_CHARS = SLUG_MAX_CHARS
_MAX_DESCRIPTION_CHARS = DESCRIPTION_MAX_CHARS

# Lazily built (unlike memory_tools' import-time _store, which exists so tests
# can patch mem0 before import): the registry has no heavy backend to fake, so
# tests just point this at a temp SQLite file.
_registry: Optional[ScopeRegistry] = None


def get_registry() -> ScopeRegistry:
    """The process-wide scope registry, shared with the dashboard."""
    global _registry
    if _registry is None:
        _registry = ScopeRegistry.from_config()
    return _registry


class ScopeSpec(TypedDict):
    """One scope a third party cares about: a short name and what it covers."""

    name: str
    description: NotRequired[str]


def register_scopes(
    party: str, scopes: list[ScopeSpec], ctx: Context | None = None
) -> str:
    """Declare the categories of the user's personal context your app cares
    about, e.g. [{"name": "dietary", "description": "food preferences,
    allergies, restrictions"}].

    Call this once when connecting (re-calling is safe: it updates
    descriptions, never deletes). `party` is your app's name. Registering
    grants NO access to memories — it defines the vocabulary the user sees,
    tags their memories with, and decides sharing by. Descriptions are shown
    to the user and guide how memories get categorized, so write them plainly.
    """
    log_tool_call("register_scopes", ctx)
    user_id = resolve_user_id(ctx)

    party_slug = slugify(party)
    if not party_slug:
        return (
            f"Could not register: party name {party!r} needs at least one "
            "letter or digit."
        )
    if len(party.strip()) > _MAX_NAME_CHARS:
        return (
            f"Could not register: party name {party.strip()[:40]!r}… is too "
            f"long (max {_MAX_NAME_CHARS} characters)."
        )
    if party_slug == RESERVED_OWNER_SLUG:
        return (
            f"Could not register: the party name {RESERVED_OWNER_SLUG!r} is "
            "reserved for scopes the user creates themselves. Use your app's "
            "own name."
        )
    if not scopes:
        return "Could not register: provide at least one scope."
    if len(scopes) > _MAX_SCOPES_PER_CALL:
        return (
            f"Could not register: at most {_MAX_SCOPES_PER_CALL} scopes per "
            f"call, got {len(scopes)}. Keep the vocabulary coarse — scopes "
            "are categories the user reasons about, not tags."
        )

    pairs: list[tuple[str, str]] = []
    seen_slugs: dict[str, str] = {}
    for spec in scopes:
        name = str(spec.get("name") or "").strip()
        description = str(spec.get("description") or "").strip()
        name_slug = slugify(name)
        if not name_slug:
            return (
                f"Could not register: scope name {name!r} needs at least one "
                "letter or digit."
            )
        if len(name) > _MAX_NAME_CHARS:
            return (
                f"Could not register: scope name {name[:40]!r}… is too long "
                f"(max {_MAX_NAME_CHARS} characters)."
            )
        if len(description) > _MAX_DESCRIPTION_CHARS:
            return (
                f"Could not register: the description for {name!r} is too "
                f"long (max {_MAX_DESCRIPTION_CHARS} characters)."
            )
        if name_slug in seen_slugs:
            return (
                f"Could not register: scope names {seen_slugs[name_slug]!r} "
                f"and {name!r} are the same scope once normalized "
                f"({name_slug!r}). Send one of them."
            )
        seen_slugs[name_slug] = name
        pairs.append((name, description))

    try:
        registered = get_registry().register(
            user_id,
            owner_type="third_party",
            owner_slug=party_slug,
            scopes=pairs,
        )
    except Exception:
        logger.exception("register_scopes failed for party=%r", party_slug)
        return (
            "Sorry, I couldn't save the scope registration right now "
            "(a backend error occurred). Please try again shortly."
        )

    lines = [
        f"Registered. {party_slug!r} now has {len(registered)} scope(s) in "
        "this user's registry:"
    ]
    for scope in registered:
        suffix = f" — {scope.description}" if scope.description else ""
        lines.append(f"- {scope.name}{suffix}")
    lines.append(
        "The user reviews these in their dashboard and controls which "
        "memories are tagged with them."
    )
    return "\n".join(lines)


def register_consent_tools(mcp: FastMCP) -> None:
    """Register the consent tools on a FastMCP server instance."""
    mcp.tool()(register_scopes)
