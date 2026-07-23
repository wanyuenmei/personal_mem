"""Thin wrapper around mem0 so the rest of the app never touches mem0 directly.

Every memory carries a `scope` in its metadata from day one. Scopes aren't
*enforced* yet (that's the future consent layer), but modeling them now — as the
brief argues, the scope schema is the spine — avoids a painful migration later.

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
        scope: str = "general",
        extra_metadata: Optional[dict] = None,
        infer: Optional[bool] = None,
    ) -> dict:
        """Store a memory. `content` is either raw text or a list of
        {"role", "content"} message dicts (a whole conversation) — mem0 accepts
        both, and passing the turns keeps conversation structure for better
        extraction than a flattened blob. With extraction on, mem0 distills/dedups
        facts; with EXTRACTION_MODE=none it stores the content and embeds it.
        `extra_metadata`, if given, is merged alongside the scope tag (e.g. the
        backfill importer stamps source/source_id provenance).

        `infer` overrides the extraction setting for THIS call: True forces LLM
        fact-extraction, False stores the content with no LLM (a 0-cost path —
        e.g. for testing the ingest pipeline without paying for extraction).
        None (the default) follows EXTRACTION_MODE via infer_enabled()."""
        user_id = _require_user_id(user_id, op="add a memory")
        metadata = {"scope": scope}
        if extra_metadata:
            metadata.update(extra_metadata)
        return self._mem.add(
            content,
            user_id=user_id,
            metadata=metadata,
            infer=infer_enabled() if infer is None else infer,
        )

    def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        limit: int = 5,
        scope: Optional[str] = None,
    ) -> list[dict]:
        """Semantic search over a user's memories, newest-relevant first.
        If `scope` is given, the filter is pushed into the store query so the
        top-k ranking happens WITHIN that scope (rather than slicing top-k after
        the fact, which could return fewer than `limit`, or none)."""
        user_id = _require_user_id(user_id, op="search memories")
        filters = {"user_id": user_id}
        if scope is not None:
            filters["scope"] = scope
        res = self._mem.search(query, filters=filters, top_k=limit)
        return _as_results(res)

    def all(self, user_id: Optional[str] = None) -> list[dict]:
        user_id = _require_user_id(user_id, op="list all memories")
        res = self._mem.get_all(filters={"user_id": user_id})
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
