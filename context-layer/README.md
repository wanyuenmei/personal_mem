# Personal Context Layer (M1)

**Own and control your AI memory — see it, audit it, edit it, delete it, and
carry it between every AI app.** A single user-owned memory store (built on
[mem0](https://github.com/mem0ai/mem0)) exposed to any AI app through MCP, so the
context you build up in one app is available in the next — under your control.

"Sign in with your context" (instant personalization for any new AI app) is what
this *unlocks* once trust exists — the destination, not the lead. See Decision #9
in `../personal-context-layer-brief.md`.

**On trust:** the store can run entirely on your machine — `EXTRACTION_MODE=none`
plus the local embedder means **nothing leaves your device**. A hosted
multi-tenant beta exists so friends can test without self-hosting; there the host
is a **custodian, not an owner** — you can export and delete your data at any
time. Self-hosting is always available.

This is **M1**: prove the loop locally — write memory from Claude, read it back
— before deploying multi-tenant (M2). See the brief for the full vision and
`MEMORY.md` (agent memory) for locked decisions.

## What's here

```
src/context_layer/
  config.py   # builds mem0 config; pluggable extraction (anthropic|ollama|none)
  memory.py   # ContextStore: add/search/all, every memory tagged with a scope
  server.py   # MCP server (stdio) exposing search_memory + add_memory
scripts/
  smoke_test.py  # add + search without MCP, to verify mem0 works
```

## Extraction modes

Set `EXTRACTION_MODE` in `.env`:

| mode        | what happens on write                              | data leaves machine? |
|-------------|----------------------------------------------------|----------------------|
| `anthropic` | Claude extracts & dedups facts (needs API key)     | yes (extraction only)|
| `ollama`    | a local LLM extracts facts (needs ollama running)  | no                   |
| `none`      | raw text stored + embedded, no LLM                 | no                   |

Embeddings always run **locally** (HuggingFace MiniLM), so search never needs a
cloud provider.

## Setup

```bash
cd context-layer
cp .env.example .env
# put ANTHROPIC_API_KEY=... in ~/.env (global, chmod 600) if using anthropic mode
uv venv --python 3.12
uv sync --extra local            # deps incl. mem0 from the ~/repos/mem0 clone (see tool.uv.sources)
uv pip install -e ".[local]"     # this package + local dev stack (HuggingFace + Chroma)

# prove it works (downloads a small embedding model on first run)
uv run python scripts/smoke_test.py
```

To **deploy** (public, pgvector, fastembed, HTTP), see [`DEPLOY.md`](DEPLOY.md).

## Add to Claude Desktop / Claude Code

Point an MCP client at the stdio server:

```json
{
  "mcpServers": {
    "personal-context": {
      "command": "uv",
      "args": ["--directory", "/Users/mei/repos/personal_mem/context-layer",
               "run", "python", "-m", "context_layer.server"]
    }
  }
}
```

Then ask Claude to save a preference, start a fresh chat, and ask it to recall
that preference via `search_memory`.

## Roadmap

- **M1** (this) — local loop: mem0 + MCP stdio, `search`/`add`.
- **M2** — Railway deploy, pgvector, OAuth, multi-tenant namespaces; ChatGPT connector.
- **M3** — repeatable, idempotent backfill from Claude/ChatGPT exports + uploads.
- **M4** — supersession + scheduled reconciliation.
- **M5** — consent layer: scoped grants, revocation → deletion webhook / erasure receipt.
