# WorkOS AuthKit OAuth — setup & live-verification runbook

**Status:** the OAuth resource-server code is merged and unit-tested, but it has
**never run against a real WorkOS tenant.** This runbook is the checklist for
turning it on with real credentials and confirming the end-to-end connector flow
(discovery + dynamic client registration + sign-in) works in the Claude and
ChatGPT connector UIs. Completing the checklist at the bottom is the acceptance
for PER-19 and unblocks retiring the capability URL (PER-22) and onboarding
friend tenants (PER-23).

## Provider decision (PER-19)

We use **WorkOS AuthKit**, not Stytch. AuthKit is a full OAuth 2.0 / OIDC
Authorization Server with Dynamic Client Registration, so this server only has to
be an [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) Resource Server — verify
bearer tokens and publish protected-resource metadata pointing at WorkOS. That
avoids re-implementing `/authorize`, `/token`, and `/register` in-process. This
runbook is the step that confirms AuthKit actually drives the Claude and ChatGPT
connector flows we saw in the capability-path deploy logs; if it can't, revisit
the decision here.

## How the server behaves (what you're turning on)

The HTTP transport has two mutually exclusive auth modes, selected purely by env:

- **Capability-path mode (today's default).** When the WorkOS vars are unset,
  each client connects at its own secret path `https://<domain>/<token>/mcp`;
  bare `/mcp` and wrong paths get a 404, never a 401.
- **OAuth mode (this runbook).** `config.workos_enabled()` returns `True` as soon
  as **all three** of `WORKOS_CLIENT_ID`, `WORKOS_AUTHKIT_DOMAIN`, and
  `PUBLIC_SERVER_URL` are set. Then FastMCP publishes
  `/.well-known/oauth-protected-resource`, wraps `/mcp` in bearer-auth
  middleware, and every `/mcp` request must carry a WorkOS-issued access token
  that `WorkOSTokenVerifier` validates against the tenant JWKS
  (`{WORKOS_API_BASE_URL}/sso/jwks/{WORKOS_CLIENT_ID}`). `resolve_user_id` then
  namespaces the token subject into `WORKOS_USER_ID_PREFIX + sub` so each
  signed-in user lands in their own isolated memory store.

The two modes don't stack: in OAuth mode only the rate-limit guard runs (the
capability-path guard would 404 the `/.well-known/*` discovery routes and swallow
the 401 the connectors need). So **do not retire `CONTEXT_LAYER_TOKEN` yet** —
that's the separate PER-22 step, gated on this runbook passing first.

## Prerequisites

- A deployed instance reachable over HTTPS with a stable public origin (the
  Railway deploy). You need the exact public MCP URL, e.g.
  `https://<your-domain>/mcp` — this is used verbatim as the RFC 9728 resource
  identifier, so it must match what you register in WorkOS character-for-character
  (scheme, host, `/mcp` path, no trailing slash).
- A WorkOS account with access to the dashboard.

## Part A — WorkOS dashboard setup

Exact labels shift as WorkOS iterates; the capabilities matter more than the menu
names. Look for WorkOS's **"AuthKit for MCP"** guide, which packages these steps.

1. **Create (or pick) a WorkOS project/environment.** Use a non-production
   environment first if you have one — you can point `WORKOS_API_BASE_URL` at it.
2. **Enable AuthKit** and note the **AuthKit domain** (the issuer), e.g.
   `https://<app>.authkit.app`. This becomes `WORKOS_AUTHKIT_DOMAIN`.
3. **Enable Dynamic Client Registration (DCR).** MCP connector UIs register
   themselves at runtime; without DCR the Claude/ChatGPT flow can't create a
   client and sign-in never starts. This is the single most common reason the
   flow fails — confirm it's on.
4. **Register this server's MCP URL** as the allowed resource / redirect target
   per WorkOS's MCP setup, using the exact `PUBLIC_SERVER_URL` value.
5. **Copy the Client ID** (`client_...`) → `WORKOS_CLIENT_ID`. The API key
   (`sk_...`) is optional for us (JWKS is public) but set it if convenient.
6. **(Optional) Scopes / audience.** If your tenant issues tokens with an `aud`
   claim, set `WORKOS_AUDIENCE` to it (the verifier only enforces audience when
   this is set). If you require specific scopes, set `WORKOS_REQUIRED_SCOPES`
   (space/comma-separated); empty means any valid token is accepted.

## Part B — Server env configuration

Set these on the deploy (Railway app service → Variables). Never commit real
values; they're deploy secrets. See `.env.example` for the annotated list.

```
WORKOS_CLIENT_ID=client_...                     # from WorkOS dashboard
WORKOS_AUTHKIT_DOMAIN=https://<app>.authkit.app # AuthKit issuer
PUBLIC_SERVER_URL=https://<your-domain>/mcp     # exact public MCP URL (RFC 9728 resource id)
# optional:
WORKOS_API_KEY=sk_...
WORKOS_API_BASE_URL=https://api.workos.com      # override only for a non-prod WorkOS env
WORKOS_AUDIENCE=
WORKOS_REQUIRED_SCOPES=
WORKOS_USER_ID_PREFIX=workos_                   # per-tenant mem0 namespace prefix
```

Redeploy. On boot, OAuth mode is active iff all three required vars are present
(`workos_enabled()`). A partial set silently stays in capability-path mode — if
verification below shows capability-path behavior, re-check that all three are
set and non-empty.

## Part C — Verification

### C1. Automated pre-checks (no browser)

From a machine that can reach the deploy:

```
python scripts/verify_oauth.py https://<your-domain>
```

It asserts, without any credentials, that:

- `GET /health` → `200 ok` (server is up).
- `GET /.well-known/oauth-protected-resource` (and the `/mcp`-suffixed variant)
  → `200`, and its `authorization_servers` / `resource` point at your
  `WORKOS_AUTHKIT_DOMAIN` and `PUBLIC_SERVER_URL`.
- `POST /mcp` with no token → `401` carrying a `WWW-Authenticate` header with a
  `resource_metadata` hint (this is what makes a connector launch WorkOS
  sign-in). A `404` here means the server is still in capability-path mode — env
  isn't fully set.

Green here means the RFC 9728 wiring is correct. It does **not** prove a real
token verifies — only the browser flows below do.

### C2. Claude connector UI (real sign-in)

1. In Claude (web or desktop) → connectors/MCP settings, add a connector with URL
   `https://<your-domain>/mcp` (the plain URL — no secret path).
2. Expect Claude to discover the protected-resource metadata and bounce you to a
   **WorkOS sign-in** page (this exercises DCR + `/authorize`).
3. Sign in / sign up. You should be redirected back and the connector should show
   connected.
4. In a chat, trigger `add_memory` ("remember that I …") then `search_memory`
   ("what do you know about me?"). Confirm the write is stored and read back.
5. Note the resulting mem0 `user_id` in the deploy logs — it should be
   `workos_<your-workos-subject>`, confirming `resolve_user_id` used the token
   subject, not `DEFAULT_USER_ID`.

### C3. ChatGPT connector UI (real sign-in)

Repeat C2 in ChatGPT (Settings → Connectors / custom MCP). ChatGPT's connector
implementation differs from Claude's, so verify it independently — the flow that
matters is the same: plain `/mcp` URL → WorkOS sign-in → connected → tool call
works.

### C4. Tenant isolation spot check

Sign in as **two different** WorkOS users (two accounts, or one in each of Claude
and ChatGPT). Write a distinct memory as each, then search from the other.
Neither should see the other's memory. This confirms the per-subject namespace
holds end-to-end, not just in unit tests.

## Verification checklist (PER-19 acceptance)

- [ ] `scripts/verify_oauth.py` passes against the live deploy (discovery + 401).
- [ ] Protected-resource metadata points at the real AuthKit issuer and the exact
      `PUBLIC_SERVER_URL`.
- [ ] Claude connector: plain `/mcp` URL → WorkOS sign-in → connected → a
      `add_memory`/`search_memory` round-trip works.
- [ ] ChatGPT connector: same round-trip works.
- [ ] Deploy logs show authenticated calls resolving to `workos_<subject>`, not
      `DEFAULT_USER_ID`.
- [ ] Two distinct users get isolated memory namespaces (C4).
- [ ] Spend watched during the run (extraction bills the shared Anthropic key —
      see PER-23).

When every box is ticked: drop the "not yet verified against a live WorkOS
tenant" caveat in `README.md`, move PER-19 to Done, and pick up PER-22 (retire
the capability URL) and PER-23 (onboard friend tenants).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `/mcp` returns `404` instead of `401` | Not all three required vars set → still in capability-path mode. |
| Connector never shows a sign-in page | DCR not enabled in WorkOS, or the connector couldn't fetch/parse the protected-resource metadata. |
| Sign-in works but `/mcp` calls 401 | Token audience/issuer mismatch — check `WORKOS_AUDIENCE` (unset it if your tenant doesn't set `aud`) and that `WORKOS_AUTHKIT_DOMAIN` equals the token `iss`. |
| 401 with "insufficient scope" behavior | `WORKOS_REQUIRED_SCOPES` demands scopes the token doesn't carry — relax it or grant the scopes. |
| Metadata resource URL doesn't match | `PUBLIC_SERVER_URL` differs from the deploy's real origin — they must match exactly. |

## Rollback

OAuth mode is fully additive. To revert to the capability-path deploy, unset the
WorkOS vars (or just `PUBLIC_SERVER_URL`) and redeploy — `workos_enabled()` goes
`False` and the capability-path guard takes over again with no data migration.
