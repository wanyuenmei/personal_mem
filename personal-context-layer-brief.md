# Personal Context Layer — Project Brief

> **One-liner:** A user-owned, portable personal context layer — **own and control everything AI knows about you**: see it, audit it, edit it, delete it, and carry it between AI apps. Each person's context lives behind an MCP server that represents them to the AI world; any AI app can query it at runtime, under scopes the user controls and can revoke. ("Sign in with your context" is what this unlocks once trust exists — the destination, not the lead. See Decision #9.)

This document captures the concept, the decisions made so far, the open questions, and a proposed first build. It's meant to be the founding context for a Claude Code project. Read it top to bottom before writing code.

---

## 1. The concept

Today every AI app keeps its own siloed memory of you. You re-explain yourself to each one, and none of it is portable or under your control. This project inverts that: **you** own a single context store, and apps query *you* rather than each hoarding their own copy.

The mental model: MCP exposes tools and data to AI agents. Here we treat a **person** as an MCP server — exposing a queryable, permissioned interface of "here's who I am, what I prefer, how I work, and what you're allowed to see or do."

The chosen wedge is **"Sign in with your context"**: the "Sign in with Google" flow, but the payload is *you*, not just an email. Land on a new AI app, hit one button, approve a consent screen, and it's personalized from the first second — no cold start, no re-explaining.

## 2. Why now

- Every major platform now ships cross-session memory, and native memory *import* features already exist (Claude, Gemini), proving the portability demand is real.
- MCP is an open, adopted protocol — meaning "you as a server" can ride rails that already exist instead of inventing distribution.
- AI memory is a hot, funded category (Mem0, Letta, Supermemory, etc.), but the pieces are fragmented across three camps — memory ("what you know"), identity/delegation ("who you are, what's authorized"), and digital twins ("acts as you"). **No one owns the user-owned unification.** That gap is the opportunity.

---

## 3. Decisions locked in

These were resolved during brainstorming and should be treated as the current direction (revisable, but with reason):

1. **Open-source first, on GitHub — not a startup (yet).** In this category, OSS *is* the distribution and trust model. Developers won't route personal memory through a closed cloud from an unknown. Local-first + open is the credible on-ramp.
2. **Core capability = "Sign in with your context."** The most protocol-shaped of the candidate wedges (vs. scheduling or inbound-screening) and closest to the core vision — so it remains the *product's destination capability*. NOTE: this is the capability, not the public headline. The launch story leads with control/ownership; see Decision #9 (2026-07-21), which supersedes the earlier assumption that "Sign in with your context" is the marketing lead.
3. **Privacy-first architecture via QUERY semantics, not COPY semantics.** When an app "signs in," it receives a *scoped, revocable token to query the context server at runtime* — NOT a copy of the context bundle it can cache. This single choice is the privacy/reach dial. Copy = fast but unrevocable and leaky (the ad-tech profile problem). Query = user stays in control, single-sourced, revocable, auditable.
4. **OAuth-style context scopes** for legible consent (e.g. `dietary.read`, `schedule.availability`, `shopping.preferences`, `tone.writing_style`). Apps declare what they need; the user approves per-scope. The consent screen must be readable by a non-technical person.
5. **Ride MCP rails to collapse the two-sided market.** The context server IS an MCP server, so existing MCP clients (Claude, Cursor, ChatGPT connectors) can query it with *zero new integration*. "Sign in with your context" becomes a friendly wrapper over an MCP connection.
6. **Bootstrap the user's context by importing** existing memories from ChatGPT / Claude / Gemini exports, so users arrive already populated.
7. **Sequencing: privacy is the wedge, reach is the flywheel.** Lead with control to earn the right to hold context; reach (being everywhere) is the *outcome* once trust exists. Leading with reach gets no adoption; leading with pure privacy gets adoption but no network effect until enough apps consume it.
8. **Moat = neutrality.** The defensibility isn't technology — it's being the platform-independent, user-owned "Switzerland" that no incumbent (OpenAI, Google) can credibly be. Any move that compromises neutrality (taking a platform's money, becoming their feature) quietly kills the moat.

9. **Public headline = control, not reach (decided 2026-07-21).** Two candidate headline stories were weighed: **(A) "Sign in with your context"** (personalization/onboarding framing) vs. **(B) "Own and control your AI memory"** (privacy/control framing — see, audit, edit, delete, port; intent-driven revocation). **Lead with (B); treat (A) as the destination, not the doormat.** Rationale, tied to the logic already in this brief:
   - Decision #7 already ordered it — "privacy is the wedge, reach is the flywheel." (A) is a reach story and has no value until apps consume the context (chicken-and-egg); you cannot headline a network effect you don't have yet.
   - **Single-player value lives entirely on the (B) side** (§6's test: useful to ONE person with ZERO consuming apps). (A) is worthless at zero apps by definition. Only (B) breaks the chicken-and-egg.
   - **Differentiation is a control story.** "Sign in with your context" is a personalization pitch any memory startup (Mem0, Supermemory, Letta) can claim. The actual moat — neutrality (#8) + intent-driven revocation and GDPR/CCPA deletion propagation ([[pcl-gdpr-deletion-wedge]]) — is all control.
   - **Channel fit:** the launch audience is GitHub + HN + "audit your AI data" press (§6), which clicks on ownership, not onboarding-conversion. (A)'s natural audience is app builders — the *sell side* (§7–8), a later motion; you can't run consumer top-of-funnel on a B2B value prop.
   - **Motion:** come for control, stay for reach. (B) earns the right to hold context; (A) is what that trust later unlocks.
   - **Implications:** M1 README leads "Own your AI memory — see, edit, delete, and carry it between every AI app," demoting "Sign in with your context" to a "what this unlocks" section (§10.6 already prescribes this framing). Product naming should connote a personal vault/record/custodian and neutrality — avoid "Sign in / Login / Passport / Onboard" names that pre-commit to the (A)/sell-side frame; reserve "Sign in with your context" as an internal flow name, not the product identity.
   - **Biggest tension to manage:** the hosted-multi-tenant test architecture ([[pcl-stack-decisions]]) makes the founder the data custodian / GDPR controller, which softens a "you own it, not some unknown's cloud" headline. Mitigate by keeping the self-host/local path (the `none` extraction mode + local Chroma) first-class and documented, being explicit in the README about what's stored and where, framing the hosted instance as custodian-not-owner with one-click export/delete, and scoping hosted tenancy to trusted friends until the M5 consent/deletion layer makes the control claims fully real.
   - **When to revisit / steelman (A):** (A) wins only if the binding constraint turns out to be the *consuming* side — i.e. challenger apps are desperate enough for day-one personalization that the "sign in" button pulls users in behind it (the "Sign in with Google" spread pattern, §7). That requires (a) the standalone "own your AI memory" hook proving *not* painful enough to pull adoption alone (Open Question #2), AND (b) real consuming apps ready at launch so "sign in" isn't pointing at an empty room. Neither is true today; reconsider after M2 if the audit hook underperforms and app-builder inbound is strong.

---

## 4. Architecture direction

**Shape:** a personal context MCP server + a local store + an importer + a control dashboard.

- **Transport:**
  - **v0 = local (stdio).** Runs as a process on the user's machine; works with desktop MCP clients (Claude Desktop, Claude Code, Cursor). Easy, private, shippable in a weekend.
  - **v1 = remote (HTTP/streamable).** Needed for web/mobile/ChatGPT/Gemini and for the real "sign in" flow. Adds deployment + auth + consent-screen complexity. Defer until v0 proves useful.
- **Store:** SQLite for facts + FTS for keyword search to start; add a vector store (e.g. ChromaDB + a small local embedding model like `all-MiniLM-L6-v2`) if/when semantic recall is needed. Local-first — the DB is a directory on disk.
- **Tools the server exposes (initial):**
  - `context.get(scope)` — return the user's context for a granted scope
  - `context.search(query, scope)` — semantic/keyword search within granted scopes
  - `context.list_scopes()` — what scopes exist and what each contains
  - (later) `context.request_access(scopes)` — initiate a consent flow
- **Scope manifest:** a schema defining available scopes, what data each maps to, and default sensitivity. This is both the privacy model and the (future) protocol surface.
- **Importer:** parse ChatGPT/Claude/Gemini memory or export files → normalize into the store, mapped onto scopes.
- **Dashboard:** view / edit / delete stored context; see an access log of which client queried what, when. This is also the standalone product (see §6).

**Design principle:** build it as a *product*, but design the scope schema and query interface *as if they're an open protocol*, so a standard can crystallize out of traction if it wins.

---

## 5. Positioning: privacy vs reach

They are not opposing values to pick between — they're a sequence, and the copy-vs-query choice (Decision #3) is the concrete lever.

- **Privacy interpretation** ("help me expose less, safely"): query semantics, tight scopes, short TTL, visible audit log, one-click revocation. This is the v0/trust posture.
- **Reach interpretation** ("help me be present everywhere at once"): rich payloads, broad consumption, ambient context. This is the growth *outcome*, earned later.

Lead privacy. Let reach compound on top.

---

## 6. Go-to-market

**Break the chicken-and-egg with single-player value first.** The product must be useful to ONE person with ZERO apps consuming it.

- **Standalone hook:** "See, own, and port everything AI knows about you." A dashboard over your imported memories — audit, edit, delete, take it with you. Valuable solo; a privacy-curiosity magnet ("what has AI actually stored about me?"); shareable and press-friendly, spikes with every AI-data scandal.
- **First users:** AI power users / developers who juggle multiple AI apps, feel cold-start pain acutely, care about data ownership, and are technical enough to trust early OSS. They star GitHub repos — so OSS is also the acquisition channel.
- **Channels:** GitHub + HN for the dev wave; the "audit your AI data" hook for the prosumer wave; dev-led bottom-up (an MCP server is something a developer installs for themselves first).
- **Motion:** come for "own your AI memory," stay for "use it everywhere." Classic single-player → multiplayer.

## 7. Business adoption (the consuming side)

- **Central tension:** apps want to *own* user data — that's their lock-in. A portable context layer works *against* lock-in, so **incumbents (OpenAI, Google) will never adopt it.** Don't expect them to.
- **Who adopts:** **challenger and vertical AI apps** with no memory moat — a cooking assistant, travel planner, niche coding tool — that can't feel personalized on day one against ChatGPT. "Sign in with your context" lets them be personalized from second zero. You're arming the long tail against the incumbents (exactly how "Sign in with Google/Apple" spread — adopted by everyone *except* the identity giants).
- **The ask is near-zero if you ride MCP:** no SDK integration, just an MCP connection they can already speak. BizDev collapses from "adopt a standard" to "make the connection trivial."
- **The pitch is a metric, not ideology:** personalized onboarding lifts activation and retention.

## 8. Business model (later — OSS/free for now)

- Consumer side: open + free. It's the demand-and-trust engine.
- Likely revenue: **B2B — sell the context-personalization API to AI app builders** (they pay because it measurably improves their funnel). Mirrors Mem0's sell-to-developers model, with the twist that the context is *user-owned and portable* rather than app-siloed.
- Optional prosumer subscription for advanced control/privacy features. Secondary.

---

## 9. Open questions to resolve

1. **Revocation & freshness enforcement (highest priority).** "Revocable" is the whole privacy story's credibility. If an app can cache context on query, revocation becomes theater. Design puzzle: short-TTL / no-cache enforcement vs. the reach-y desire for speed and offline use. How is this actually enforced technically?
2. **Is the standalone single-player feature a *must-have* or merely nice-to-have?** If "own your AI memory" isn't painful enough to pull adoption on its own, the flywheel never spins. Needs validation.
3. **Customer question:** user pays (privacy product) vs. querying agents pay (access/attention marketplace). Leaning **user/privacy for v0**; these pull architecture in opposite directions, so don't straddle.
4. **Curation / dedup / decay:** what's worth storing, and how to reconcile conflicting facts across apps (the update-phase problem that commodity MCP memory servers skip — and arguably the part that makes memory *good*).
5. **Provenance / verifiable claims:** signed assertions so a querying agent can trust the context genuinely represents the user. Deferred, but the long-term moat once agents transact with each other.
6. **Security:** memory poisoning / injection is a named attack class against exactly this design. Treat as a first-class concern, not a bolt-on.

---

## 10. Proposed first build (v0)

Narrowest thing worth shipping — local, single-player, no network required:

1. **Local MCP server (stdio)** exposing `context.get`, `context.search`, `context.list_scopes`.
2. **Local store** (SQLite + FTS; vector optional later).
3. **Scope schema** — a small, well-designed manifest defining a handful of scopes.
4. **Importer** for at least one source (ChatGPT or Claude memory export) → normalized into the store under scopes.
5. **Minimal dashboard** — view / edit / delete context + an access log. This is the standalone hook.
6. **README** that frames it as "own, audit, and port your AI memory" and shows how to add it to a Claude Code / Claude Desktop / Cursor config.

**Deliberately deferred:** remote/HTTP transport, auth, the consent-screen "sign in" flow, provenance, monetization. Those are v1+ once v0 proves the standalone hook lands.

**First decision for the build session:** language/stack — TypeScript (mature MCP SDK, aligned with the client ecosystem) vs. Python (nicer for the embedding/vector path). Pick one before scaffolding.

---

## 11. Suggested next steps for Claude Code

1. Confirm stack (TS vs Python) and scaffold the MCP server with the three read tools stubbed.
2. Design the scope schema first — it's the spine of both privacy and the future protocol.
3. Build the SQLite store + a seed dataset so tools return real data end-to-end.
4. Wire up one importer against a real ChatGPT/Claude export.
5. Stand up the minimal dashboard + access log.
6. Write the README with the "own your AI memory" framing and client-install instructions.
7. Only then revisit the v1 remote/consent path.
