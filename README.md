# Personal Context Layer

**Own and control your AI memory — see it, audit it, edit it, delete it, and carry it between every AI app.**

One user-owned memory store (built on [mem0](https://github.com/mem0ai/mem0)), exposed to any AI client through [MCP](https://modelcontextprotocol.io). Tell Claude something once; Cursor and ChatGPT know it too — and the data lives in a store *you* run, not inside any one vendor's silo.

"Sign in with your context" (instant personalization for any new AI app) is what this unlocks once trust exists — the destination, not the lead.

> Status: working system, private beta of one. The deployed instance signs users in through WorkOS, so any MCP client that completes the OAuth flow reaches the same store; the memories behind it are still one person's.

## Quickstart: run it locally

Run your own instance on your machine and point a local AI client at it. In `EXTRACTION_MODE=none` nothing leaves your machine.

Prereqs: Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env             # defaults are fine to start
uv venv --python 3.12
uv sync --extra local            # local-dev stack: chromadb + sentence-transformers

# prove the loop (no API key needed in EXTRACTION_MODE=none)
EXTRACTION_MODE=none uv run python scripts/smoke_test.py

# see everything stored about you
uv run python scripts/inspect_db.py
```

Then connect a client that launches the server over stdio — Claude Desktop or Claude Code — by adding this to its MCP config:

```json
"personal-context": {
  "command": "uv",
  "args": ["--directory", "<path-to-this-repo>",
           "run", "python", "-m", "context_layer"]
}
```

Restart the client, then [steer its instructions](#steer-your-ai-client) so it actually calls the store.

## Quickstart: use a hosted instance

Connecting to an instance someone already deployed — including your own, see [Host your own instance](#host-your-own-instance) — takes no install. Paste the instance's URL into each app's connector/MCP settings:

```
https://<your-domain>/mcp
```

Whoever adds it is bounced to a WorkOS sign-in and lands in their own isolated namespace (`WORKOS_USER_ID_PREFIX` + the token's subject), so one deployment can serve several people without any of them seeing another's memories.

What the credential identifies is **you, not the app**: sign in from Claude, Cursor, and ChatGPT and all three resolve to the same namespace and the same memories. That is the portability the whole thing is for, and it's why there is no per-client token to hand out or revoke — the units of control are your WorkOS session and your account.

Then [steer each client's instructions](#steer-your-ai-client) so it actually calls the store.

## See your memories in the browser

`/dashboard` is the memory browser: everything the store holds about you, newest first, with timestamps, as-you-type search, and a row of scope chips that narrows the list to one or more scopes (or to what nothing has claimed yet) — plus the consent-scope panel, where you review the categories connected apps registered, create scopes of your own, and tag or untag individual memories with them. The eye in the header masks every memory's text so the page can be screen-shared or screenshotted in public, and each card has its own eye to reveal a single memory while the rest stay masked; it's a display setting kept in your browser, so it changes nothing about what connected apps can read (and the text is still in the page source — it hides memories from a camera, not from devtools). It's served by the same HTTP transport as `/mcp`, behind whichever auth mode the deploy already runs:

- **OAuth deploy:** open `https://<domain>/dashboard` and sign in with the same WorkOS account your AI clients use — the page shows exactly the namespace they write to. Sign-in needs two extra env vars (`WORKOS_API_KEY` and `WORKOS_COOKIE_PASSWORD` — see `.env.example`) and `https://<domain>/dashboard/callback` registered under **Redirects** in the WorkOS dashboard; until those are set, `/dashboard` answers 503 with the same instructions.
- **Capability mode:** open `https://<domain>/<token>/dashboard` — the same secret-path credential as `/mcp`, showing the shared single-tenant namespace.

A local stdio setup has no HTTP surface; use `uv run python scripts/inspect_db.py` for the same view in the terminal, or start the server with `MCP_TRANSPORT=streamable-http` and open `http://localhost:8000/dashboard`.

The page reads memories and writes only consent state (scopes and tags) and retention state (kept or set aside). Editing or deleting the memories themselves from the browser is tracked as PER-56 and PER-41.

**Suggest scopes from my memories** is where a brand-new account starts. Until some scopes exist there is nothing to tag memories into, so this takes one pass over your store and proposes the categories it sees in it. They arrive as a checklist and only what you tick gets registered — a scope is the thing a future sharing decision is made in, so the vocabulary stays yours to accept or throw away. Everything it registers is your own scope, never one on a connected app's behalf, and a proposal that collides with a scope you already have is dropped rather than overwriting what you wrote there. Ticking them also starts the re-tag below, so the categories you approved arrive with your memories already sorted into them rather than empty. Like re-tagging, it needs `EXTRACTION_MODE=anthropic` — in any other mode no memory is sent to a model and the panel says so.

**Re-tag all memories** runs the scope classifier over your whole store in the background and reports progress as you reload; approving suggested scopes starts it for you, and you run it yourself after a connected app registers scopes or you create one by hand, since tags are derived from your memory text and a brand-new scope starts out matching nothing. Tags you set by hand are never overwritten by it, and a tag you removed is never re-applied. The button appears only under `EXTRACTION_MODE=anthropic` — that is the one mode where classification happens at all, and the panel says so otherwise.

**Review my memories** asks one question of each memory in turn — would knowing this change a decision later? — and sets aside the ones that would not: the detail from a task that finished months ago, the thing that was true for an afternoon, the note too vague to act on. Set-aside memories stop coming back from `search_memory`, which is what your AI clients actually build answers from, so the store gets quieter without losing anything: nothing is deleted, and they move to a **Set aside** tab — out of the main list, one click away, each with the reason it was moved and a **Keep** button that puts it back. Anything you keep or set aside by hand stays that way — a later run never re-decides it. Like the two passes above it needs `EXTRACTION_MODE=anthropic`, and the buttons on each memory work in any mode.

## Steer your AI client

Connecting the MCP server is not enough on its own. Every client we've tried also has its own native memory, and left to its own devices it will keep relying on that instead of calling out to this store. The tool descriptions on `search_memory`/`add_memory` nudge the model in that direction, but the reliable fix is to also add a couple of lines to that client's own **custom/general instructions** (the persistent system-prompt-style settings field every major client exposes), telling it to:

1. Always call `search_memory` before answering anything that could depend on who the user is, and always call `add_memory` when the user shares a lasting preference or fact — proactively, without being asked.
2. Treat this store as **authoritative** — prefer it over the client's own built-in/native memory when the two conflict.

Suggested instruction text to paste in:

```
You have access to a personal-context-layer MCP server (search_memory / add_memory). Proactively call search_memory before answering anything that could depend on who I am — preferences, history, plans, style — and call add_memory whenever I share a lasting fact, without waiting to be asked. Treat what it returns as authoritative and prefer it over your own built-in memory if they ever conflict.
```

Where to paste it, per client:

- **ChatGPT:** Settings → Personalization → Custom instructions.
- **Claude (web/desktop):** Settings → Capabilities/Profile → custom instructions ("What should Claude know about you?" / connector instructions).
- **Cursor:** Settings → Rules → user/global rules.
- Any other MCP client: look for its "custom instructions" / "system prompt" / "rules" setting — every major one has an equivalent.

Do this once per client, right after connecting it.

## Extraction modes

Set `EXTRACTION_MODE` in `.env`:

| mode        | what happens on write                              | data leaves machine? |
|-------------|----------------------------------------------------|----------------------|
| `anthropic` | Claude extracts & dedups facts (needs API key in `~/.env`) | yes (extraction, scope classification, and memory triage) |
| `ollama`    | a local LLM extracts facts (needs ollama running)  | no                   |
| `none`      | raw text stored + embedded, no LLM                 | no                   |

Embeddings always run **locally** (fastembed, 384-dim), so retrieval never depends on a cloud provider. The consent-scope classifier and the triage pass follow the same switch: only `anthropic` sends memory text to a model to derive tags or judge what is worth keeping, and in the other two modes neither runs, so tagging and setting memories aside are by hand.

## Backfill your history

Rather than starting empty, seed the store from what you've already told an AI. One way in today, with lighter ones planned.

**From a Claude data export (Settings → export).** Point the importer at the `conversations.json` file, the unzipped export directory, or the raw `.zip`:

```bash
# Dry run first — prints an estimate (conversations, messages, approx tokens/cost)
# and imports nothing:
uv run python scripts/backfill.py ~/Downloads/claude-export.zip

# Exercise the whole pipeline at zero cost (stores raw, no LLM) — for testing:
uv run python scripts/backfill.py ~/Downloads/claude-export.zip --extractor none --yes

# Import for real once the estimate looks right:
EXTRACTION_MODE=anthropic uv run python scripts/backfill.py \
    ~/Downloads/claude-export.zip --limit 20 --yes
```

Each conversation is fed through mem0 extraction. A full import is the most expensive operation here — every conversation costs extraction tokens — so it never runs without `--yes`, and `--limit N` caps how many conversations go through. `--extractor` picks the fact extractor per run: `auto` (default, follows `EXTRACTION_MODE`), `llm` (force LLM extraction), or `none` (store raw with no LLM — a 0-cost path, for testing the pipeline). Imported memories carry `source`/`source_id` provenance in their metadata. Then `scripts/inspect_db.py` shows what landed.

**Planned — from other exports, and from no file at all.** A ChatGPT export parser is next (PER-28), with an idempotent ingest manifest so re-running an import can't double-store (PER-29). Beyond the export path: in a client that already has the server connected, you'd ask Claude to gather what it knows about you and write it through the `add_memory` tool — the backfill lands as ordinary tool calls in the conversation, nothing to download, unzip, or point a script at.

## How it works

```
 Claude ──┐
 Cursor ──┼──▶  MCP server ──▶ mem0 ──▶ pgvector (deploy) / Chroma (local)
 ChatGPT ─┘    (search/add,     (LLM fact extraction + dedup,
                WorkOS sign-in   local 384-dim embeddings)
                or capability
                paths)
```

For the full request path — transport → auth guards → tools → identity seam → memory store → mem0 → vector store — see [`ARCHITECTURE.md`](ARCHITECTURE.md). The server also exposes `GET /health` (200, no auth) for liveness/readiness probes.

- **Categories:** the vocabulary exists per party rather than global: each third party registers the consent scopes it cares about via the `register_scopes` tool, into a per-user registry (identity is self-asserted until VC-65, so registering grants nothing — it only declares categories the user can see and tag against). Memories carry their tags as one `cs_<scope-key>` metadata key per scope with a provenance value (`user` tags survive re-sweeps, `llm` tags are rebuildable, `user_removed` is a veto the classifier must respect), set by hand from the dashboard or derived by the scope classifier. That classifier runs strictly out of band — a daemon thread after a write, or the dashboard's re-tag sweep — so no read or write ever waits on it and a classifier failure leaves a memory untagged rather than failing the save. Since mem0 keeps the fact text and its embedding, `llm` tags stay derivable and the sweep can rebuild them whenever the vocabulary changes.
- **Audit trail:** every tool call emits one line of JSON — tool, resolved tenant, calling client, timestamp — so a deploy's logs can be filtered by any of them. Under OAuth the client is named from its `User-Agent`, because the token identifies the person rather than the app (PER-65). It's structured logging, not yet a queryable audit datastore.
- **Deletion:** the store has tenant-safe `delete` and `delete_all` primitives, each refusing to act without a valid tenant id. Neither is exposed as an MCP tool yet (PER-57) — they're the foundation the erasure work builds on.
- **Trust model:** run it fully local (`none` mode: nothing leaves your machine), or self-host the deploy. The hosted instance is a custodian, not an owner — export and delete at any time.

## Host your own instance

Stand up your own deployment — for yourself across your devices, or to share with a few people. Railway is the paved path; the two auth modes below decide who a request resolves to.

### Deploy (Railway)

`railway init` from the repo root, then add a **Postgres** service in the dashboard — Railway's Postgres ships `pgvector`, and mem0 runs `CREATE EXTENSION vector` automatically on first write. Set these env vars on the **app service**:

```
EXTRACTION_MODE=anthropic
ANTHROPIC_API_KEY=<your key>                    # server-side secret
EMBEDDER_PROVIDER=fastembed
VECTOR_STORE=pgvector
MCP_TRANSPORT=streamable-http
DATABASE_URL=${{Postgres.DATABASE_URL}}         # Railway reference to the DB
WORKOS_CLIENT_ID=client_...                     # the OAuth trio: all three, or none
WORKOS_AUTHKIT_DOMAIN=https://<app>.authkit.app
PUBLIC_SERVER_URL=https://<domain>/mcp
```

Without the WorkOS trio the deploy falls back to capability paths, which need `CONTEXT_LAYER_TOKENS=claude:<tok>,cursor:<tok>` instead. `USER_ID` names the single-tenant namespace a request lands in when it carries no authenticated principal (stdio, capability paths, OAuth unconfigured); under OAuth the token subject is used and `USER_ID` is never read.

`PORT` is injected automatically. Then `railway up` (builds the Dockerfile), and **Settings → Networking → Generate Domain** for a public URL — your MCP endpoint is `https://<domain>/mcp`, or `https://<domain>/<token>/mcp` in capability mode. Liveness check: `curl -s https://<domain>/health` → `ok`. Paste the URL into each client's connector settings (see [Quickstart: use a hosted instance](#quickstart-use-a-hosted-instance)).

**Troubleshooting:** build fails on `mem0ai` → confirm `mem0ai==2.0.12` still resolves on PyPI; `CREATE EXTENSION vector` error → the Postgres image lacks pgvector, recreate it from a pgvector template; tools error at runtime → check `railway logs`, usually a missing `ANTHROPIC_API_KEY` or `DATABASE_URL`; `/mcp` 404s instead of returning a 401 → one of the three WorkOS vars is missing, so the server is still in capability mode; sign-in dies mid-flow with `error=invalid_target` → `PUBLIC_SERVER_URL` isn't registered in WorkOS as a resource indicator, character for character.

### OAuth mode (WorkOS AuthKit)

The server is an OAuth 2.0 Resource Server, not an authorization server: it publishes `/.well-known/oauth-protected-resource`, answers an unauthenticated `/mcp` with a 401 carrying a `resource_metadata` hint, and verifies the WorkOS-issued bearer token on every subsequent request against WorkOS's public JWKS. Discovery, client registration, and the sign-in page all belong to WorkOS — the connector UIs drive them natively, so there is nothing to paste but the URL.

To turn it on: (a) a WorkOS project with **AuthKit** enabled and client self-registration on — **CIMD**, **Dynamic Client Registration**, or both, since connectors differ in which they use; (b) `https://<your-domain>/mcp` registered as a resource indicator, which WorkOS matches as a literal string; (c) these env vars (see `.env.example` for the annotated list):

```
WORKOS_CLIENT_ID=client_...                       # WorkOS Dashboard
WORKOS_AUTHKIT_DOMAIN=https://<app>.authkit.app   # AuthKit issuer
PUBLIC_SERVER_URL=https://<your-domain>/mcp       # RFC 9728 resource id — include the /mcp path
# optional: WORKOS_API_KEY, WORKOS_AUDIENCE, WORKOS_REQUIRED_SCOPES, WORKOS_LEEWAY
```

OAuth mode switches on only when all three are set; a partial set stays silently in capability-path mode. Step-by-step setup and the verification checklist: [`docs/workos-oauth-runbook.md`](docs/workos-oauth-runbook.md), including the credential-free pre-flight `python scripts/verify_oauth.py https://<your-domain>`.

### Capability URLs — the fallback when WorkOS is unset

With no WorkOS config the server serves each client at its own secret path instead, the URL itself being the credential:

```
https://<your-domain>/<client-token>/mcp
```

Here each client gets its own named token (`CONTEXT_LAYER_TOKENS="claude:…,cursor:…"`), revocable one at a time, and every client shares the single `USER_ID` namespace — the mirror image of OAuth, which identifies the person and lets the clients blur together. Any other path, including a bare `/mcp`, gets a 404, never a 401: a connector reads a 401 as an invitation to start a sign-in flow, which in this mode could not succeed.

The two modes are mutually exclusive, so with WorkOS configured the capability paths 404 and any tokens still set are inert. Retiring this path once every client is on OAuth is PER-22.

## Project layout

One package, one directory per architecture layer (see [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full map + diagram):

```
src/context_layer/
  app.py, __main__.py   # composition root + entrypoint (`python -m context_layer`)
  config.py             # env-driven settings: extraction modes, stores, embedders, transport
  transport/            # stdio + streamable-HTTP assembly; /health endpoint
  auth/                 # WorkOS OAuth resource server; capability-path + rate-limit guards
  tools/                # the MCP tools: search_memory / add_memory / register_scopes
  identity/             # resolve_user_id — the tenant seam
  memory/               # ContextStore over mem0: add/search/all/update_metadata/delete/delete_all
  consent/              # per-user consent-scope registry, the cs_* tag vocabulary, and the out-of-band classifier
  curation/             # retention state per memory + the triage pass that keeps the store worth reading
  dashboard/            # memory browser + scope/tag/retention manager at /dashboard (AuthKit sign-in on OAuth deploys)
  observability/        # access/audit log — one JSON line per tool call or page view
  ingest/               # offline backfill: export parsers → normalized format → batch runner
scripts/
  smoke_test.py         # add + search end-to-end without MCP
  inspect_db.py         # dump everything stored about you (the audit view, in embryo)
  backfill.py           # import a Claude data export (estimate first, then --yes)
  verify_oauth.py       # credential-free pre-flight check of a deploy's OAuth wiring
tests/             # mirrors the layers: tests/<layer>/test_*.py
docs/              # WorkOS OAuth setup + live-verification runbook
ARCHITECTURE.md    # layer-by-layer map + request-flow diagram
Dockerfile, railway.json   # deploy artifacts (Railway)
```

## Develop

```bash
uv run --extra local --with pytest pytest   # tests mirror the layers: tests/<layer>/test_*.py
uvx ruff check .                            # lint: E/F/I/B/PT, line length 100
uv run --with pyright pyright src           # types
```

The tools aren't project dependencies — CI installs them alongside the package, and `--with`/`uvx` does the same locally. The test suite needs the `local` extra: the isolation test writes to a real Chroma store and embeds for real, rather than mocking the thing it exists to prove.

Those three are what CI runs on every pull request, alongside a gitleaks secret scan and a Claude-based diff review ([`.github/workflows/`](.github/workflows)). The conventions those PRs follow — worktree per PR, branch and title format, the required body sections — live in [`CLAUDE.md`](CLAUDE.md) and [`.github/pull_request_template.md`](.github/pull_request_template.md).

[mem0](https://github.com/mem0ai/mem0) comes from PyPI at the pinned version, the same way CI and the deploy image install it. To develop against a local clone instead — say to test a fix worth sending upstream — swap it in at the environment level rather than in `pyproject.toml`, so nothing machine-specific gets committed:

```bash
uv pip install -e ~/repos/mem0   # re-run `uv sync` to go back to the pinned release
```

## Roadmap

Working today: the memory store with its tenant-isolation guard, the MCP tools, both auth modes, the access log, the memory browser at `/dashboard` with consent-scope and tag management, the out-of-band scope classifier behind it, memory triage that sets aside what no longer informs a decision, and backfill from a Claude export.

Next: retire the capability URL now that OAuth carries real traffic (PER-22) and onboard the first friend tenants (PER-23); tenant hygiene — a per-user export endpoint (PER-24), deletion exposed over MCP (PER-57), an erasure receipt (PER-25); a ChatGPT export parser (PER-28) and an idempotent ingest manifest so re-running an import is safe (PER-29); reconciliation, so a changed preference supersedes its old version instead of overwriting it (PER-32, PER-33); then the consent layer — scoped grants, revocation, deletion propagation (PER-16). Details and status live in the Linear project.
