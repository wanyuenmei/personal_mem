"""Thin wrapper around mem0 so the rest of the app never touches mem0 directly.

Memories are written untagged: the add path stamps no category label (PER-63
removed the old one), and consent-scope tags arrive later, out of band, as
metadata merged in through `update_metadata` — one ``cs_<scope-key>`` payload
key per scope, written by the dashboard (VC-87) and eventually the classifier
(VC-88). See context_layer.consent.tags for the key/provenance shape and why
it is one scalar key per scope rather than an array.

--- Tenant isolation (PER-5) --------------------------------------------------
Isolation currently rests entirely on passing `filters={"user_id": ...}` (or
the `user_id=` kwarg) through to mem0 on every call. That is a single point of
failure: if `identity.resolve_user_id()` (or any future auth wiring, see
PER-12) ever returns `None`/`""`/whitespace, mem0 would silently search or
write with NO user filter at all — the worst possible failure mode for a
product whose entire promise is data ownership.

`_require_user_id` is the defense-in-depth guard: every method on
ContextStore that touches mem0 validates its resolved user_id through this
one choke point before the call reaches mem0, and raises loudly
(TenantIsolationError) instead of ever letting a falsy/blank id through.
Note this module does NOT default a missing user_id to DEFAULT_USER_ID
anymore — silently substituting a fallback here would defeat the point of the
guard (a caller bug would just quietly write to the default tenant instead of
failing loudly). Callers that want the single-tenant default (e.g. the stdio
path via identity.resolve_user_id()) must pass it explicitly.

Per-tenant namespacing (a separate mem0 collection/store per user_id, rather
than one shared collection filtered by a field) was considered per the ticket
but deliberately deferred — see the PR description for the full trade-off.
"""

from typing import Optional

from mem0 import Memory

from context_layer.config import build_mem0_config, infer_enabled


class TenantIsolationError(ValueError):
    """Raised when a store call would otherwise reach mem0 without a valid,
    non-empty user_id — i.e. a filter that would match/affect every tenant's
    data instead of one. This must never be caught-and-ignored; a caller
    seeing this needs to fix its user_id resolution, not paper over it."""


def _require_user_id(user_id: Optional[str], *, op: str) -> str:
    if not isinstance(user_id, str) or not user_id.strip():
        raise TenantIsolationError(
            f"Refusing to {op}: user_id must be a non-empty string, got "
            f"{user_id!r}. This is a tenant-isolation guard — a caller "
            "upstream (e.g. identity.resolve_user_id()) returned a falsy "
            "user_id, which would otherwise reach mem0 with no per-tenant "
            "filter at all."
        )
    return user_id


# --- shim: mem0's fastembed embedder returns numpy arrays, which psycopg can't
# adapt as SQL parameters ("cannot adapt type 'ndarray'") on the pgvector path.
# Coerce to plain lists at the embedder boundary; harmless for chroma. Applies
# to whichever mem0 is installed (PyPI in the container, the editable clone
# locally), so it fixes prod too. Candidate for an upstream mem0 PR.
try:
    from mem0.embeddings.fastembed import FastEmbedEmbedding

    _orig_fastembed_embed = FastEmbedEmbedding.embed

    def _embed_as_list(self, text, memory_action=None):
        out = _orig_fastembed_embed(self, text, memory_action)
        return out.tolist() if hasattr(out, "tolist") else out

    FastEmbedEmbedding.embed = _embed_as_list  # type: ignore[method-assign]
except ImportError:  # fastembed not installed (huggingface-only env)
    pass


# Payload keys update_metadata refuses to touch: the tenant filter itself
# (user_id — overwriting it would re-home the memory to another tenant, the
# exact move the isolation guard exists to prevent), mem0's other identity
# fields, and the core fields mem0 derives from the text on every update.
_PROTECTED_METADATA_KEYS = frozenset(
    {"user_id", "agent_id", "run_id", "actor_id", "role",
     "id", "data", "hash", "created_at", "updated_at"}
)


def _as_results(res: object) -> list[dict]:
    """mem0 returns either {'results': [...]} or a bare list; normalize to a list."""
    if isinstance(res, dict):
        res = res.get("results", [])
    return res if isinstance(res, list) else []


class ContextStore:
    def __init__(self) -> None:
        self._mem = Memory.from_config(build_mem0_config())

    def add(
        self,
        content: "str | list[dict]",
        user_id: Optional[str] = None,
        extra_metadata: Optional[dict] = None,
        infer: Optional[bool] = None,
    ) -> dict:
        """Store a memory. `content` is either raw text or a list of
        {"role", "content"} message dicts (a whole conversation) — mem0 accepts
        both, and passing the turns keeps conversation structure for better
        extraction than a flattened blob. With extraction on, mem0 distills/dedups
        facts; with EXTRACTION_MODE=none it stores the content and embeds it.
        `extra_metadata`, if given, is stored as the memory's metadata (e.g. the
        backfill importer stamps source/source_id provenance).

        `infer` overrides the extraction setting for THIS call: True forces LLM
        fact-extraction, False stores the content with no LLM (a 0-cost path —
        e.g. for testing the ingest pipeline without paying for extraction).
        None (the default) follows EXTRACTION_MODE via infer_enabled()."""
        user_id = _require_user_id(user_id, op="add a memory")
        return self._mem.add(
            content,
            user_id=user_id,
            metadata=dict(extra_metadata) if extra_metadata else {},
            infer=infer_enabled() if infer is None else infer,
        )

    def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        limit: int = 5,
    ) -> list[dict]:
        """Semantic search over a user's memories, newest-relevant first."""
        user_id = _require_user_id(user_id, op="search memories")
        res = self._mem.search(query, filters={"user_id": user_id}, top_k=limit)
        return _as_results(res)

    def all(self, user_id: Optional[str] = None, limit: int = 1000) -> list[dict]:
        """Every memory for a user, up to `limit`.

        mem0's get_all defaults to top_k=20 — passing no limit silently truncates
        to the 20 most recent, which is what made the dashboard show only a slice.
        We pass an explicit, generous ceiling instead. This is a ceiling, not true
        pagination: browsing an arbitrarily large store (and grouping it into the
        mind-map view) is PER-84.
        """
        user_id = _require_user_id(user_id, op="list all memories")
        res = self._mem.get_all(filters={"user_id": user_id}, top_k=limit)
        return _as_results(res)

    def delete(self, memory_id: str, user_id: Optional[str] = None) -> dict:
        """Delete a single memory by id — but only if it belongs to `user_id`.

        Internal use for now, deliberately NOT wired into an MCP tool: the
        reconciliation / version-control work will call this to prune a
        superseded memory once that lands. It's exposed on the store so those
        callers (and admin scripts) have a tenant-safe primitive to build on.

        mem0's own `delete(memory_id)` takes NO user filter, so a bare id would
        let any caller delete ANY tenant's memory — the same single-point-of-
        failure `_require_user_id` guards against on the read/write paths. This
        wrapper fetches the memory first and refuses to delete unless its owner
        matches the resolved `user_id`, keeping deletes behind the same
        tenant-isolation choke point as add/search/all.

        Returns ``{"deleted": True, "id": ...}`` on success, or
        ``{"deleted": False, "id": ..., "reason": "not_found"}`` when no memory
        with that id exists — delete is idempotent, so removing an absent id is
        not an error. Raises TenantIsolationError on a falsy `user_id`, or when
        the id resolves to a memory owned by a different tenant.
        """
        user_id = _require_user_id(user_id, op="delete a memory")
        existing = self._mem.get(memory_id)
        if not existing:
            return {"deleted": False, "id": memory_id, "reason": "not_found"}
        owner = existing.get("user_id")
        if owner != user_id:
            raise TenantIsolationError(
                f"Refusing to delete memory {memory_id!r}: it belongs to a "
                f"different tenant (owner={owner!r}, caller user_id={user_id!r}). "
                "A delete must never cross tenant boundaries."
            )
        self._mem.delete(memory_id)
        return {"deleted": True, "id": memory_id}

    def update_metadata(
        self, memory_id: str, updates: dict, user_id: Optional[str] = None
    ) -> dict:
        """Merge `updates` into a memory's metadata — only if it belongs to
        `user_id`. Same fetch-first owner check as `delete`, because mem0's own
        `update(memory_id, metadata=...)` takes no user filter either.

        MERGE, not replace: mem0's `_update_memory` copies the existing payload
        and `dict.update`s the given metadata into it, so keys can be added or
        overwritten but never removed. That constraint is why consent-tag
        removal writes a `user_removed` tombstone value instead of deleting the
        key (see context_layer.consent.tags). It also means the payload's own
        `user_id` survives an update — the memory must remain visible through
        the user_id-filtered read paths afterwards, which the real-store test
        in tests/memory/test_isolation.py proves rather than assumes (mem0's
        `get` strips identity keys out of the metadata dict it returns, so a
        naive read-modify-write cycle would hand mem0 a metadata dict with no
        `user_id` in it at all).

        `updates` must not touch the payload keys mem0 or the tenant filter
        own (`user_id` above all — overwriting it would move the memory across
        tenants); those raise ValueError before anything reaches mem0.

        A metadata-only update still re-embeds the unchanged text through the
        local embedder — cheap, and no LLM call.

        Returns ``{"updated": True, "id": ...}`` on success, or
        ``{"updated": False, "id": ..., "reason": "not_found"}`` for an absent
        id. Raises TenantIsolationError on a falsy `user_id` or when the id
        belongs to a different tenant.
        """
        user_id = _require_user_id(user_id, op="update a memory's metadata")
        if not updates:
            raise ValueError("update_metadata needs at least one key to set")
        protected = _PROTECTED_METADATA_KEYS.intersection(updates)
        if protected:
            raise ValueError(
                f"Refusing to update metadata keys {sorted(protected)}: they are "
                "managed by the store/mem0 (overwriting user_id would move the "
                "memory across tenants)."
            )
        existing = self._mem.get(memory_id)
        if not existing:
            return {"updated": False, "id": memory_id, "reason": "not_found"}
        owner = existing.get("user_id")
        if owner != user_id:
            raise TenantIsolationError(
                f"Refusing to update metadata of memory {memory_id!r}: it belongs "
                f"to a different tenant (owner={owner!r}, caller "
                f"user_id={user_id!r})."
            )
        self._mem.update(memory_id, metadata=dict(updates))
        return {"updated": True, "id": memory_id}

    def delete_all(self, user_id: Optional[str] = None) -> dict:
        """Delete EVERY memory belonging to a single tenant.

        The tenant-isolation guard matters most here: mem0's `delete_all()`
        with no filter deletes across ALL tenants (it only raises if *nothing*
        is passed), so a falsy user_id slipping through would wipe every user's
        data. `_require_user_id` refuses a falsy id loudly before the call ever
        reaches mem0, and the `user_id=` filter scopes the wipe to exactly one
        tenant.

        Internal use for now, deliberately NOT wired into an MCP tool — the
        user-facing "delete everything" / account-erasure flow will build on
        this primitive.

        Returns ``{"deleted_all": True, "user_id": ..., "count": N}`` where N is
        how many memories were removed (counted before the wipe).
        """
        user_id = _require_user_id(user_id, op="delete all memories")
        count = len(self.all(user_id=user_id))
        self._mem.delete_all(user_id=user_id)
        return {"deleted_all": True, "user_id": user_id, "count": count}
