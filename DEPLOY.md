# Deploying the context layer to Railway (M2.1)

Goal: a public HTTPS MCP server backed by Postgres/pgvector, so **Claude and
ChatGPT** can both connect and share memory. This is the single-tenant demo
(no auth yet — that's M2.2). Don't put anyone else's data in it until then.

## 0. Prerequisites
- A Railway account: https://railway.com (free trial tier is fine).
- The Railway CLI:
  ```bash
  brew install railway        # or: npm i -g @railway/cli
  railway login
  ```

## 1. Create the project + database
From the repo root:
```bash
railway init                  # create a new project (name it e.g. context-layer)
```
Then add a **pgvector-enabled Postgres**:
- In the Railway dashboard for this project → **New** → **Database** → pick
  **Postgres** (Railway's Postgres includes the `pgvector` extension; if you see
  a dedicated "pgvector" template, use that). mem0 runs
  `CREATE EXTENSION IF NOT EXISTS vector` automatically on first write, so no
  manual SQL is needed as long as the extension is available.

## 2. Set environment variables
On the **app service** (not the DB), set:
```
EXTRACTION_MODE=anthropic
ANTHROPIC_API_KEY=<your key>        # server-side secret; all tenants' extraction bills here
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
EMBEDDER_PROVIDER=fastembed
VECTOR_STORE=pgvector
MCP_TRANSPORT=streamable-http
DATABASE_URL=${{Postgres.DATABASE_URL}}   # Railway reference to the DB you added
USER_ID=mei                          # single tenant for now (M2.2 replaces this via auth)
```
`DATABASE_URL` uses Railway's private network, so no SSL config is needed.
`PORT` is injected by Railway automatically — the app reads it.

CLI equivalent (or just use the dashboard Variables tab):
```bash
railway variables --set EXTRACTION_MODE=anthropic --set EMBEDDER_PROVIDER=fastembed \
  --set VECTOR_STORE=pgvector --set MCP_TRANSPORT=streamable-http --set USER_ID=mei \
  --set ANTHROPIC_MODEL=claude-haiku-4-5-20251001
# set the secret + DB reference in the dashboard (avoids the key hitting your shell history)
```

## 3. Deploy
```bash
railway up                    # builds the Dockerfile and deploys from this directory
```

## 4. Get the public URL
Dashboard → app service → **Settings → Networking → Generate Domain**. You'll get
something like `https://context-layer-production.up.railway.app`. Your MCP
endpoint is that URL **+ `/<CONTEXT_LAYER_TOKEN>/mcp`** (the capability path —
the URL itself is the credential; with no token set it's just `/mcp`):
```
https://context-layer-production.up.railway.app/<token>/mcp
```
Paste that full URL into Claude / ChatGPT connector settings — no separate auth
step. Rotate by changing CONTEXT_LAYER_TOKEN and re-pasting.
Quick check it's alive:
```bash
# liveness — no token/auth needed, answered before the guards:
curl -s https://<your-domain>/health          # -> ok

# full MCP handshake:
curl -s -X POST https://<your-domain>/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```
Expect a `serverInfo: personal-context-layer` line.

## 5. Connect the clients (the money demo)
- **Claude** (web/desktop): Settings → Connectors → **Add custom connector** →
  paste the `/mcp` URL. (You can remove the local stdio `personal-context` entry
  first to avoid two servers with the same tools.)
- **ChatGPT**: Settings → Connectors (Developer mode may be required depending on
  your plan) → add the same `/mcp` URL.
- Then: save a fact in **Claude** ("remember I …"), and ask **ChatGPT** to recall
  it. That's the cross-app proof.

## Known risk to watch (verify here, per the plan)
ChatGPT's connector requirements shift — it has historically wanted `search`/
`fetch`-shaped tools; developer mode is looser. If ChatGPT rejects the tools,
that's the signal to add a ChatGPT-compatible tool shape. We find this out now,
before building auth (M2.2) — which is the whole point of doing M2.1 first.

## Troubleshooting
- **Build fails on mem0ai**: confirm `mem0ai==2.0.12` still resolves on PyPI.
- **`CREATE EXTENSION vector` permission/availability error**: the Postgres image
  lacks pgvector — recreate the DB from a pgvector template.
- **Server boots but tools error**: check logs (`railway logs`) for the mem0
  config — usually a missing env var (ANTHROPIC_API_KEY or DATABASE_URL).
