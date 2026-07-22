"""Build the mem0 configuration + server settings from environment.

Everything here is designed so that LOCAL defaults are unchanged from M1
(stdio transport, Chroma store, HuggingFace embedder) and the DEPLOY path is
opt-in via env vars. That means your existing local setup and data keep working
untouched; Railway just sets a handful of variables.

Extraction backend (unchanged from M1) is pluggable via EXTRACTION_MODE:
  anthropic | ollama | none  (none => infer=False, nothing leaves the machine)
"""

import os

from dotenv import load_dotenv

# Project-local .env first (wins — load_dotenv never overrides already-set vars),
# then the user's global ~/.env as fallback for machine-wide secrets like
# ANTHROPIC_API_KEY, so keys don't have to live inside the repo directory.
load_dotenv()
load_dotenv(os.path.expanduser("~/.env"))

# --- Extraction LLM ---------------------------------------------------------
EXTRACTION_MODE = os.getenv("EXTRACTION_MODE", "anthropic").strip().lower()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
_VALID_MODES = {"anthropic", "ollama", "none"}

# --- Embedder (local + free in all cases) -----------------------------------
# fastembed (default) or huggingface. Default is fastembed EVERYWHERE — including
# local dev — so local and prod embed identically (parity): no "works on my
# machine" divergence and no re-embedding when moving between them.
EMBEDDER_PROVIDER = os.getenv("EMBEDDER_PROVIDER", "fastembed").strip().lower()
EMBEDDER_MODEL = os.getenv("EMBEDDER_MODEL", "").strip()
_HF_DEFAULT = "sentence-transformers/all-MiniLM-L6-v2"
_FASTEMBED_DEFAULT = "BAAI/bge-small-en-v1.5"

# Known embedding dimensions per model. pgvector fixes the column dimension at
# table-creation time, so a silent model/dims mismatch corrupts search. We look
# the dims up from the model and refuse to start on a conflict.
_KNOWN_DIMS = {
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "BAAI/bge-small-en-v1.5": 384,
}


def _resolved_embedder_model() -> str:
    if EMBEDDER_MODEL:
        return EMBEDDER_MODEL
    return _FASTEMBED_DEFAULT if EMBEDDER_PROVIDER == "fastembed" else _HF_DEFAULT


def _resolved_embed_dims() -> int:
    model = _resolved_embedder_model()
    known = _KNOWN_DIMS.get(model)
    env = os.getenv("EMBED_DIMS")
    if env is not None:
        dims = int(env)
        if known is not None and dims != known:
            raise ValueError(
                f"EMBED_DIMS={dims} conflicts with the known {known} dims for "
                f"embedder model {model!r}. Fix EMBED_DIMS or the model."
            )
        return dims
    if known is not None:
        return known
    raise ValueError(
        f"Unknown embedding dimension for model {model!r}. Set EMBED_DIMS "
        f"explicitly to its true dimension (and keep it in sync with pgvector)."
    )


EMBED_DIMS = _resolved_embed_dims()

# --- Vector store -----------------------------------------------------------
# chroma (default, local, embedded) or pgvector (deploy).
VECTOR_STORE = os.getenv("VECTOR_STORE", "chroma").strip().lower()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()  # Railway injects this
PG_COLLECTION = os.getenv("PG_COLLECTION", "context_layer").strip()

# --- Paths / identity -------------------------------------------------------
DATA_DIR = os.path.expanduser(os.getenv("CONTEXT_LAYER_DATA", "~/.context-layer"))
DEFAULT_USER_ID = os.getenv("USER_ID", "mei")

# --- MCP transport ----------------------------------------------------------
# stdio (default, local/Claude Desktop) or streamable-http (deploy).
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
# Railway (and most PaaS) inject PORT; fall back to MCP_PORT then 8000.
MCP_PORT = int(os.getenv("PORT", os.getenv("MCP_PORT", "8000")))

# --- HTTP guardrails (stopgap until M2.2 OAuth) -----------------------------
# Capability tokens: each token gets its own secret endpoint /<token>/mcp, so
# individual clients can be issued + revoked independently. Format:
#   CONTEXT_LAYER_TOKENS="claude:abc123,cursor:def456"  (label:token pairs)
# Bare tokens (no label) also work. Falls back to the older singular
# CONTEXT_LAYER_TOKEN. Empty = open /mcp (fine locally; not on a public deploy).


def _parse_tokens() -> dict[str, str]:
    """Returns {token: label} from CONTEXT_LAYER_TOKENS / CONTEXT_LAYER_TOKEN."""
    raw = (
        os.getenv("CONTEXT_LAYER_TOKENS", "").strip()
        or os.getenv("CONTEXT_LAYER_TOKEN", "").strip()
    )
    tokens: dict[str, str] = {}
    if not raw:
        return tokens
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    for i, part in enumerate(parts):
        label, sep, tok = part.partition(":")
        if sep and tok.strip():
            tokens[tok.strip()] = label.strip() or f"client{i}"
        else:
            tokens[part] = f"client{i}" if len(parts) > 1 else "default"
    return tokens


CONTEXT_LAYER_TOKENS = _parse_tokens()
# Crude global cap so an exposed endpoint can't run up unbounded Anthropic spend.
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "120"))

# --- OAuth multi-tenancy (M2.2, WorkOS AuthKit) -----------------------------
# ADDITIVE and fully opt-in: when these are UNSET the server behaves exactly as
# today (CapabilityPathGuard + single-tenant resolve_user_id). When set, the
# HTTP transport runs as an OAuth 2.0 Resource Server (RFC 9728): FastMCP
# publishes /.well-known/oauth-protected-resource pointing at the WorkOS AuthKit
# issuer, connector UIs (Claude/ChatGPT) discover it, register dynamically and
# sign in AGAINST WORKOS, and every /mcp request must carry a WorkOS-issued
# Bearer access token that we verify against WorkOS's JWKS.
#
# NB: this is the deliberate reversal of the M2.1 "never answer 401" rule — in
# OAuth mode a 401 with a WWW-Authenticate resource_metadata hint is exactly
# what makes a connector launch its (now-succeedable) sign-in flow. It only
# happens when WorkOS is configured; the capability-path deploy is untouched.
#
# NEVER hardcode any of these — they are secrets/tenant identifiers supplied at
# deploy time. See .env.example and the README "Connect an AI client" section.
WORKOS_CLIENT_ID = os.getenv("WORKOS_CLIENT_ID", "").strip()
WORKOS_API_KEY = os.getenv("WORKOS_API_KEY", "").strip()
# The AuthKit issuer URL the connector discovers + signs in against, e.g.
# https://your-app.authkit.app  (WorkOS Dashboard -> AuthKit -> "AuthKit domain").
WORKOS_AUTHKIT_DOMAIN = os.getenv("WORKOS_AUTHKIT_DOMAIN", "").strip()
# WorkOS API base (JWKS lives at {base}/sso/jwks/{client_id}); override only for
# testing against a non-prod WorkOS environment.
WORKOS_API_BASE_URL = os.getenv("WORKOS_API_BASE_URL", "https://api.workos.com").strip()
# The public URL of THIS resource server (its MCP endpoint), e.g.
# https://your-domain/mcp — used verbatim as the RFC 9728 resource identifier.
PUBLIC_SERVER_URL = os.getenv("PUBLIC_SERVER_URL", "").strip()
# Optional: comma/space-separated scopes a token must carry (empty = any valid
# token is accepted). Also the audience to validate, if your tenant sets `aud`.
WORKOS_REQUIRED_SCOPES = os.getenv("WORKOS_REQUIRED_SCOPES", "").strip()
WORKOS_AUDIENCE = os.getenv("WORKOS_AUDIENCE", "").strip()
# Prefix applied to the token subject to form the mem0 user_id, so authenticated
# tenants can never collide with the single-tenant DEFAULT_USER_ID namespace.
WORKOS_USER_ID_PREFIX = os.getenv("WORKOS_USER_ID_PREFIX", "workos_")


def workos_required_scopes() -> list[str]:
    """Required scopes as a list (empty => accept any valid token)."""
    raw = WORKOS_REQUIRED_SCOPES.replace(",", " ")
    return [s for s in raw.split() if s]


def workos_enabled() -> bool:
    """True only when the minimum WorkOS OAuth config is present.

    Requires the client id (identifies the JWKS + tenant), the AuthKit issuer
    (what connectors discover and sign in against), and this server's public URL
    (the RFC 9728 resource identifier). WORKOS_API_KEY is optional — token
    verification only needs the public JWKS. Any missing piece => OAuth is off
    and the server falls back to the capability-path stopgap.
    """
    return bool(WORKOS_CLIENT_ID and WORKOS_AUTHKIT_DOMAIN and PUBLIC_SERVER_URL)


def infer_enabled() -> bool:
    """Whether mem0 runs LLM fact-extraction on writes."""
    return EXTRACTION_MODE != "none"


def _llm_block() -> dict:
    if EXTRACTION_MODE == "anthropic":
        return {"provider": "anthropic",
                "config": {"model": ANTHROPIC_MODEL, "temperature": 0.1, "max_tokens": 2000}}
    if EXTRACTION_MODE == "ollama":
        return {"provider": "ollama",
                "config": {"model": OLLAMA_MODEL, "temperature": 0.1, "max_tokens": 2000}}
    # none: never invoked (infer=False), but must construct without a real key.
    return {"provider": "openai",
            "config": {"model": "gpt-4o-mini", "api_key": "none-mode-unused"}}


def _embedder_block() -> dict:
    model = _resolved_embedder_model()
    if EMBEDDER_PROVIDER == "fastembed":
        return {"provider": "fastembed",
                "config": {"model": model, "embedding_dims": EMBED_DIMS}}
    return {"provider": "huggingface", "config": {"model": model}}


def _vector_store_block() -> dict:
    if VECTOR_STORE == "pgvector":
        cfg = {"collection_name": PG_COLLECTION, "embedding_model_dims": EMBED_DIMS}
        if DATABASE_URL:
            cfg["connection_string"] = DATABASE_URL
        return {"provider": "pgvector", "config": cfg}
    return {"provider": "chroma",
            "config": {"collection_name": "context_layer",
                       "path": os.path.join(DATA_DIR, "chroma")}}


def build_mem0_config() -> dict:
    if EXTRACTION_MODE not in _VALID_MODES:
        raise ValueError(
            f"EXTRACTION_MODE={EXTRACTION_MODE!r} is invalid; choose one of {sorted(_VALID_MODES)}"
        )
    return {
        "llm": _llm_block(),
        "embedder": _embedder_block(),
        "vector_store": _vector_store_block(),
    }
