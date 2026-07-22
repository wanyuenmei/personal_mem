# Personal Context Layer

**Own and control your AI memory — see it, audit it, edit it, delete it, and
carry it between every AI app.**

One user-owned memory store (built on [mem0](https://github.com/mem0ai/mem0)),
exposed to any AI client through [MCP](https://modelcontextprotocol.io). Tell
Claude something once; Cursor and ChatGPT know it too — and the data lives in a
store *you* run, not inside any one vendor's silo.

> Status: working system, private beta of one. Claude, Cursor, and ChatGPT
> currently share a deployed store. Roadmap lives in the
> [Linear project](https://linear.app/personal-context-mcp/project/personal-context-layer-99ad394253c2).

## Repo layout

| Path | What |
|---|---|
| [`context-layer/`](context-layer/) | The product: MCP server + mem0 store + deploy artifacts. **Start with its [README](context-layer/README.md).** |
| [`personal-context-layer-brief.md`](personal-context-layer-brief.md) | Founding brief: concept, decisions #1–9, positioning, GTM, open questions. |

## Quickstart (local, 5 minutes)

Prereqs: Python 3.12, [uv](https://docs.astral.sh/uv/), a clone of
[mem0](https://github.com/mem0ai/mem0) at `~/repos/mem0`.

```bash
cd context-layer
cp .env.example .env             # defaults are fine to start
uv venv --python 3.12
uv sync --extra local            # deps incl. mem0 from the local clone

# prove the loop (no API key needed in EXTRACTION_MODE=none)
EXTRACTION_MODE=none uv run python scripts/smoke_test.py

# see everything stored about you
uv run python scripts/inspect_db.py
```

For LLM-powered fact extraction (recommended), put `ANTHROPIC_API_KEY=...` in
`~/.env` and keep `EXTRACTION_MODE=anthropic`.

## Connect an AI client

**Claude Desktop / Claude Code (local, stdio)** — add to your MCP config:

```json
"personal-context": {
  "command": "uv",
  "args": ["--directory", "<path-to>/context-layer",
           "run", "python", "-m", "context_layer.server"]
}
```

**Claude web · Cursor · ChatGPT (remote)** — deploy it (below), then paste your
per-client capability URL into each app's connector/MCP settings:

```
https://<your-domain>/<client-token>/mcp
```

Each client gets its own named token (`CONTEXT_LAYER_TOKENS="claude:…,cursor:…"`),
so any one of them can be revoked without touching the others.

## Deploy (Railway)

Full walkthrough: [`context-layer/DEPLOY.md`](context-layer/DEPLOY.md).
Short version: `railway init` → add Postgres (pgvector) → set env vars →
`railway up` → generate a domain → paste capability URLs into your clients.

## How it works

```
 Claude ──┐
 Cursor ──┼──▶  MCP server ──▶ mem0 ──▶ pgvector (deploy) / Chroma (local)
 ChatGPT ─┘    (search/add,     (LLM fact extraction + dedup,
                per-client       local 384-dim embeddings)
                capability
                paths)
```

- **Extraction modes:** `anthropic` (Claude distills + dedups facts) ·
  `ollama` (local LLM, nothing leaves your machine) · `none` (raw storage,
  zero external calls). Embeddings are always local.
- **Scopes:** every memory is tagged (`dietary`, `shopping`, `travel`, …) —
  the spine of the future per-scope consent layer.
- **Trust model:** run it fully local, or self-host the deploy. The hosted
  instance is a custodian, not an owner — export and delete at any time.

## Roadmap

M2.2 OAuth multi-tenancy → M2.3 export/delete/access-log → M3 backfill from
Claude/ChatGPT exports → M4 reconciliation → M5 consent layer (scoped grants,
revocation → deletion webhook). Details + status: the Linear project.
