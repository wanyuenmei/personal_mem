"""Test-wide environment setup and shared fixtures.

context_layer.config reads its configuration from the environment at IMPORT
time (module-level globals), so these vars must be set before anything in
this test session first imports context_layer.config — which is why this
lives in conftest.py rather than inside a fixture, and why it uses
os.environ.setdefault (never override something the environment/CI already
set on purpose) except for CONTEXT_LAYER_DATA, which always gets a private
temp directory so tests never read or write the developer's real
~/.context-layer store.

EXTRACTION_MODE=none means mem0 stores raw text and embeds it, without
invoking any LLM for fact-extraction — so tests need no ANTHROPIC_API_KEY and
make no outbound LLM calls. The embedder still runs for real (fastembed,
local/ONNX, 384-dim) so the isolation test exercises a real local vector
store, not a mock — the whole point of that test is to prove the filter
actually holds in the store, not merely that the Python call sites look right.

The `fake_mem0` fixture below is the opposite tack, for the PER-7 tests
(tenant filtering, config shape, tool error handling): those exercise our own
code against a fully in-memory mem0 double so they never touch a real
backend at all. Both styles coexist — tests opt into `fake_mem0` when they
want the double; the isolation test just constructs a real ContextStore.
"""

import hashlib
import os
import re
import tempfile
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("EXTRACTION_MODE", "none")
os.environ.setdefault("VECTOR_STORE", "chroma")
os.environ.setdefault("EMBEDDER_PROVIDER", "fastembed")
os.environ["CONTEXT_LAYER_DATA"] = tempfile.mkdtemp(prefix="context-layer-test-")


@pytest.fixture
def fake_mem0(monkeypatch):
    """Patch mem0.Memory.from_config to return a MagicMock in place of a real
    mem0.Memory instance, so ContextStore() never touches a real backend."""
    import mem0

    fake = MagicMock(name="fake_mem0_memory")
    monkeypatch.setattr(mem0.Memory, "from_config", lambda cfg: fake)
    return fake


def _patch_offline_embedder() -> None:
    """Opt-in escape hatch: swap fastembed's real (network-downloaded, ONNX)
    model for a tiny deterministic hash-based embedder, so the isolation test
    can run somewhere huggingface.co is unreachable.

    This replaces ONLY the embedding math. Everything the isolation test
    actually needs to prove real still runs for real: mem0's Memory class,
    the real chroma vector store, and — the thing that matters — the real
    user_id metadata filter that tenant isolation depends on. A deterministic
    per-text hash is a perfectly good stand-in for embedding *quality* here,
    since this test only needs semantically-close-enough vectors to get
    plausible top-k matches back; it is not evaluating retrieval quality.

    Off by default: GitHub Actions' hosted runner has normal internet access
    and downloads the real fastembed model, so CI exercises the true
    end-to-end embedder. Only set CONTEXT_LAYER_TEST_OFFLINE_EMBEDDER=1 in an
    environment where huggingface.co is actually blocked (confirmed for this
    dev sandbox: its egress proxy returns policy-denial 403s to
    huggingface.co:443, not merely a flaky/slow download).

    The stand-in is a hashing-trick bag-of-words vector (each lowercased word
    token hashes to a dimension + sign, summed, then L2-normalized) rather
    than pure random noise per string: mem0's search applies a real
    minimum-similarity threshold (0.1 by default), and independent random
    vectors in 384 dims almost never clear it, which would make the search
    half of the isolation test vacuous (empty results regardless of
    filtering). A shared-vocabulary hash gives genuinely similar vectors to
    texts that share words — the same shape of signal a real embedder would
    give — so a broken/missing user_id filter would still leak a
    same-vocabulary memory across tenants and be caught.
    """
    from mem0.embeddings.base import EmbeddingBase
    from mem0.embeddings.fastembed import FastEmbedEmbedding

    _TOKEN_RE = re.compile(r"[a-z0-9']+")

    def _offline_init(self, config=None) -> None:
        EmbeddingBase.__init__(self, config)
        self.config.model = self.config.model or "offline-hash-embedder"
        if not self.config.embedding_dims:
            self.config.embedding_dims = 384

    def _offline_embed(self, text, memory_action=None) -> list[float]:
        dims = self.config.embedding_dims or 384
        vec = [0.0] * dims
        for token in _TOKEN_RE.findall(text.lower()):
            digest = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16)
            idx = digest % dims
            sign = 1.0 if (digest // dims) % 2 == 0 else -1.0
            vec[idx] += sign
        norm = sum(v * v for v in vec) ** 0.5
        if norm == 0:
            vec[0] = 1.0
        else:
            vec = [v / norm for v in vec]
        return vec

    FastEmbedEmbedding.__init__ = _offline_init  # type: ignore[method-assign]
    FastEmbedEmbedding.embed = _offline_embed  # type: ignore[method-assign]


if os.environ.get("CONTEXT_LAYER_TEST_OFFLINE_EMBEDDER", "").strip().lower() in (
    "1",
    "true",
    "yes",
):
    _patch_offline_embedder()
