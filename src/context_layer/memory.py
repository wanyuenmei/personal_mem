"""Thin wrapper around mem0 so the rest of the app never touches mem0 directly.

Every memory carries a `scope` in its metadata from day one. Scopes aren't
*enforced* yet (that's the M5 consent layer), but modeling them now — as the
brief argues, the scope schema is the spine — avoids a painful migration later.
"""

from typing import Optional

from mem0 import Memory

from .config import DEFAULT_USER_ID, build_mem0_config, infer_enabled

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
        text: str,
        user_id: Optional[str] = None,
        scope: str = "general",
    ) -> dict:
        """Store a memory. With extraction on, mem0 distills/dedups facts;
        with EXTRACTION_MODE=none it stores the raw text and embeds it."""
        return self._mem.add(
            text,
            user_id=user_id or DEFAULT_USER_ID,
            metadata={"scope": scope},
            infer=infer_enabled(),
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
        filters = {"user_id": user_id or DEFAULT_USER_ID}
        if scope is not None:
            filters["scope"] = scope
        res = self._mem.search(query, filters=filters, top_k=limit)
        return _as_results(res)

    def all(self, user_id: Optional[str] = None) -> list[dict]:
        res = self._mem.get_all(filters={"user_id": user_id or DEFAULT_USER_ID})
        return _as_results(res)
