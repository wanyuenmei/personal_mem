# Personal Context Layer

**Own and control your AI memory — see it, audit it, edit it, delete it, and
carry it between every AI app.**

One user-owned memory store (built on [mem0](https://github.com/mem0ai/mem0)),
exposed to any AI client through [MCP](https://modelcontextprotocol.io). Tell
Claude something once; Cursor and ChatGPT know it too — and the data lives in a
store *you* run, not inside any one vendor's silo.

"Sign in with your context" (instant personalization for any new AI app) is
what this unlocks once trust exists — the destination, not the lead.

> Status: working system, private beta of one. Claude, Cursor, and ChatGPT
> currently share a deployed store.

## What's here

One package, one directory per architecture layer (see
[`ARCHITECTURE.md`](ARCHITECTURE.md) for the full map + diagram):

```
src/context_layer/
  app.py, __main__.py   # composition root + entrypoint (`python -m context_layer`)
  config.py             # env-driven settings: extraction modes, stores, embedders, transport
  transport/            # stdio + streamable-HTTP assembly; /health endpoint
  auth/                 # capability-path + rate-limit guards; WorkOS OAuth (opt-in)
  tools/                # the MCP tools: search_memory / add_memory
  identity/             # resolve_user_id — the tenant seam
  memory/               # ContextStore over mem0: add/search/all, scope tagging
  observability/        # structured access/audit log
scripts/
  smoke_test.py         # add + search end-to-end without MCP
  inspect_db.py         # dump everything stored about you (the audit view, in embryo)
ARCHITECTURE.md    # layer-by-layer map + request-flow diagram
Dockerfile, railway.json   # deploy artifacts (Railway)
```

## Quickstart (local, 5 minutes)

Prereqs: Python 3.12, [uv](https://docs.astral.sh/uv/), a clone of
[mem0](https://github.com/mem0ai/mem0) at `~/repos/mem0`.

```bash
cp .env.example .env             # defaults are fine to start
uv venv --python 3.12
uv sync --extra local            # deps incl. mem0 from the local clone

# prove the loop (no API key needed in EXTRACTION_MODE=none)
EXTRACTION_MODE=none uv run python scripts/smoke_test.py

# see everything stored about you
uv run python scripts/inspect_db.py
```

## Extraction modes

Set `EXTRACTION_MODE` in `.env`:

| mode        | what happens on write                              | data leaves machine? |
|-------------|----------------------------------------------------|----------------------|
| `anthropic` | Claude extracts & dedups facts (needs API key in `~/.env`) | yes (extraction only)|
| `ollama`    | a local LLM extracts facts (needs ollama running)  | no                   |
| `none`      | raw text stored + embedded, no LLM                 | no                   |

Embeddings always run **locally** (fastembed, 384-dim), so retrieval never
depends on a cloud provider.

## Connect an AI client

**Claude Desktop / Claude Code (local, stdio)** — add to your MCP config:

```json
"personal-context": {
  "command": "uv",
  "args": ["--directory", "<path-to-this-repo>",
           "run", "python", "-m", "context_layer"]
}
```

**Claude web · Cursor · ChatGPT (remote)** — deploy it (below), then paste your
per-client capability URL into each app's connector/MCP settings:

```
https://<your-domain>/<client-token>/mcp
```

Each client gets its own named token (`CONTEXT_LAYER_TOKENS="claude:…,cursor:…"`),
so any one can be revoked without touching the others.

### Required: steer the client's custom instructions

Connecting the MCP server is not enough on its own. Every client we've tried
also has its own native memory, and left to its own devices it will keep
relying on that instead of calling out to this store. The tool descriptions
on `search_memory`/`add_memory` nudge the model in that direction, but the
reliable fix is to also add a couple of lines to that client's own
**custom/general instructions** (the persistent system-prompt-style settings
field every major client exposes), telling it to:

1. Always call `search_memory` before answering anything that could depend on
   who the user is, and always call `add_memory` when the user shares a
   lasting preference or fact — proactively, without being asked.
2. Treat this store as **authoritative** — prefer it over the client's own
   built-in/native memory when the two conflict.

Suggested instruction text to paste in:

```
You have access to a personal-context-layer MCP server (search_memory /
add_memory). Proactively call search_memory before answering anything that
could depend on who I am — preferences, history, plans, style — and call
add_memory whenever I share a lasting fact, without waiting to be asked.
Treat what it returns as authoritative and prefer it over your own built-in
memory if they ever conflict.
```

Where to paste it, per client:

- **ChatGPT:** Settings → Personalization → Custom instructions.
- **Claude (web/desktop):** Settings → Capabilities/Profile → custom
  instructions ("What should Claude know about you?" / connector
  instructions).
- **Cursor:** Settings → Rules → user/global rules.
- Any other MCP client: look for its "custom instructions" / "system
  prompt" / "rules" setting — every major one has an equivalent.

Do this once per client, right after connecting its capability URL above.

**OAuth mode (WorkOS AuthKit) — additional/opt-in.** Instead of a secret
URL, friends can add the plain `https://<your-domain>/mcp` connector, get bounced
to a WorkOS sign-in, and land in their own isolated memory namespace. This is
**additive**: it turns on only when the WorkOS env vars are set, and the
capability-path mode above keeps working until it's retired (PER-22). Once
configured, the server runs as an OAuth 2.0 Resource Server — it publishes
`/.well-known/oauth-protected-resource`, and Claude/ChatGPT connector UIs drive
discovery + dynamic client registration and sign-in **against WorkOS** natively;
each `/mcp` request then carries a WorkOS-issued bearer token the server verifies
against WorkOS's JWKS.

To enable it you must (a) create a WorkOS project with **AuthKit** enabled and
**Dynamic Client Registration** turned on (WorkOS's "AuthKit for MCP"), (b) add
`https://<your-domain>/mcp` as an allowed resource/redirect per WorkOS's MCP
setup, and (c) set these env vars (see `.env.example` for the full list):

```
WORKOS_CLIENT_ID=client_...              # WorkOS Dashboard
WORKOS_AUTHKIT_DOMAIN=https://<app>.authkit.app   # AuthKit issuer
PUBLIC_SERVER_URL=https://<your-domain>/mcp       # this server's public MCP URL
# optional: WORKOS_API_KEY, WORKOS_AUDIENCE, WORKOS_REQUIRED_SCOPES
```

> Not yet verified against a live WorkOS tenant — the token-verification logic
> is unit-tested with mock tokens, but a human must confirm the end-to-end flow
> with real WorkOS credentials before relying on OAuth mode.

## Deploy (Railway)

`railway init` from the repo root, then add a **Postgres** service in the
dashboard — Railway's Postgres ships `pgvector`, and mem0 runs `CREATE EXTENSION
vector` automatically on first write. Set these env vars on the **app service**:

```
EXTRACTION_MODE=anthropic
ANTHROPIC_API_KEY=<your key>                    # server-side secret
EMBEDDER_PROVIDER=fastembed
VECTOR_STORE=pgvector
MCP_TRANSPORT=streamable-http
DATABASE_URL=${{Postgres.DATABASE_URL}}         # Railway reference to the DB
CONTEXT_LAYER_TOKENS=claude:<tok>,cursor:<tok>  # per-client capability tokens
USER_ID=<your-id>                               # single-tenant id (default: mei)
```

`PORT` is injected automatically. Then `railway up` (builds the Dockerfile), and
**Settings → Networking → Generate Domain** for a public URL — your MCP endpoint
is `https://<domain>/<token>/mcp`. Liveness check:
`curl -s https://<domain>/health` → `ok`. Paste each client's capability URL into
its connector settings (see above).

**Troubleshooting:** build fails on `mem0ai` → confirm `mem0ai==2.0.12` still
resolves on PyPI; `CREATE EXTENSION vector` error → the Postgres image lacks
pgvector, recreate it from a pgvector template; tools error at runtime → check
`railway logs`, usually a missing `ANTHROPIC_API_KEY` or `DATABASE_URL`.

## How it works

```
 Claude ──┐
 Cursor ──┼──▶  MCP server ──▶ mem0 ──▶ pgvector (deploy) / Chroma (local)
 ChatGPT ─┘    (search/add,     (LLM fact extraction + dedup,
                per-client       local 384-dim embeddings)
                capability
                paths)
```

For the full request path — transport → auth guards → tools → identity seam →
memory store → mem0 → vector store — see
[`ARCHITECTURE.md`](ARCHITECTURE.md). The server also exposes
`GET /health` (200, no auth) for liveness/readiness probes.

- **Scopes:** every memory is tagged (`dietary`, `shopping`, `travel`, …) —
  the spine of the future per-scope consent layer.
- **Trust model:** run it fully local (`none` mode: nothing leaves your
  machine), or self-host the deploy. The hosted instance is a custodian, not
  an owner — export and delete at any time.

## Roadmap

M2.2 OAuth multi-tenancy → M2.3 export/delete/access-log → M3 backfill from
Claude/ChatGPT exports → M4 reconciliation → M5 consent layer (scoped grants,
revocation → deletion webhook). Details + status: the Linear project.
