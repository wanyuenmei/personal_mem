"""Server-side rendering for the memory browser + scope manager page.

One self-contained HTML document: inline CSS + JS, no external assets, no
frontend build step. The memory rows and the scope registry are embedded as a
JSON <script> block and rendered client-side with textContent — never
innerHTML — so memory text and scope names/descriptions (both of which any
connected client can write, and are therefore attacker-influenceable) can't
inject markup into the page. The JSON embed itself escapes "/" so no value
containing "</script>" can break out of the data block.

Mutations are plain HTML form POSTs to the dashboard's endpoints. Every form
action is computed client-side from location.pathname (the dashboard base as
the BROWSER sees it), because in capability mode the real URL carries a
/<token> prefix the guard strips before this server ever sees the path — an
absolute /dashboard/* action would escape the prefix and 404. Values placed
into forms go through input.value / option.value assignment, never markup.

Search is client-side substring filtering over the full list — fine for the
size of a personal store, and it keeps the page a pure read of store.all()
plus the registry. Narrowing to a set of scopes (VC-95) is client-side for
the same reason and needs nothing new: every memory's tags and the whole
registry are already in the data block, so it is a predicate over what the
page holds, not another read. It is deliberately not a store query — mem0's
pgvector filter builder cannot match inside a JSON array, which is why tags
are one payload key per scope to begin with. The chips narrow within whichever
tab is open and count against it, so they describe the list they sit above.

Archived memories (VC-94) are rendered too, behind their own tab, each with
the reason it was set aside and a button that puts it back. Two views of one
document, switched client-side: the main list is what a client searching this
account would be answered from, so a set-aside memory must not sit in it —
but nowhere else can a user see what an automatic pass decided about their
own store, so "archived" must not mean "invisible here" either.

The map (VC-84) is a third tab on that same bar rather than a second view
switcher: the page already had one way to change what the list below shows,
and two competing ones would be a worse page than either. It is a radial
graph of the user at the centre with their scopes around them, sized by how
much is filed under each, drawn as inline SVG built with createElementNS and
textContent — so it inherits the same "no markup from data" guarantee as
everything else here, and needs no library. Opening a scope fans its memories
out as leaves and dims the rest, and renders the cards themselves below the
graph, reusing memoryCard rather than trying to make memory text legible
inside SVG. The one thing the map cannot compute itself is what a scope is
ABOUT; that comes from the summaries the server holds (consent.summary),
which is why it has a button rather than generating them on load.

Which tab is open lives in localStorage, for the reason the mask toggles do:
the panels above reload this page every few seconds while a pass runs, and
being thrown back to the main list mid-review would make the archived tab
unusable exactly when it matters.

The eye toggles that mask memory text are a display setting and nothing more:
they live in localStorage, never in the store, and change nothing about what
connected apps can read. The text they mask is still in this document's data
block, so they hide memories from a camera pointed at the screen — the case
they exist for — not from anyone reading the page source.
"""

import html
import json
from typing import Optional

from context_layer.consent import ConsentScope, active_tags
from context_layer.curation import decided_by_user, retention_reason, retention_state


# Rendered into the page <script> data block. json.dumps with these settings
# cannot emit a literal "</script>": "/" is escaped, non-ASCII is escaped.
def _json_for_script(data: object) -> str:
    return json.dumps(data, ensure_ascii=True).replace("/", "\\/")


def _rows_to_payload(rows: list[dict]) -> list[dict]:
    """Reduce raw mem0 rows to exactly what the page shows."""
    payload = []
    for r in rows:
        metadata = r.get("metadata") or {}
        payload.append(
            {
                "id": str(r.get("id") or ""),
                "text": str(r.get("memory") or r.get("text") or ""),
                "created_at": str(r.get("created_at") or ""),
                "updated_at": str(r.get("updated_at") or ""),
                "source": str(metadata.get("source") or ""),
                # {scope_key: provenance}; user_removed already reads as untagged
                "tags": active_tags(metadata),
                # Kept or set aside, and by whom — the page shows an automatic
                # decision differently from one the user made, because only the
                # first is something they might want to argue with.
                "retention": {
                    "state": retention_state(metadata),
                    "by_user": decided_by_user(metadata),
                    "reason": retention_reason(metadata),
                },
            }
        )
    return payload


def _scopes_to_payload(scopes: list[ConsentScope]) -> list[dict]:
    return [
        {
            "key": s.key,
            "owner": s.owner_name,
            "name": s.name,
            "description": s.description,
        }
        for s in scopes
    ]


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your personal context</title>
<style>
  :root {
    --bg: #ffffff; --fg: #1a1a1a; --muted: #6b7280; --card: #f6f7f9;
    --border: #e5e7eb; --accent: #4f46e5;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #111317; --fg: #e7e9ee; --muted: #9aa1ad; --card: #1a1d23;
      --border: #2a2e37; --accent: #8b8ff8;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 760px; margin: 0 auto; padding: 2rem 1rem 4rem; }
  .head {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 1rem; margin-bottom: .25rem;
  }
  h1 { font-size: 1.4rem; margin: 0; }
  h2 { font-size: 1.05rem; margin: 0 0 .35rem; }
  .who { color: var(--muted); font-size: .9rem; margin: 0 0 1.25rem; }
  .who a { color: var(--accent); }
  #scopes-panel, #triage-panel {
    background: var(--card); border: 1px solid var(--border);
    border-radius: .5rem; padding: .8rem 1rem; margin-bottom: 1.25rem;
  }
  .scope-group h3 {
    font-size: .8rem; font-weight: 600; color: var(--muted);
    margin: .5rem 0 .25rem;
  }
  .scope, .tag {
    display: inline-flex; align-items: center; gap: .25rem;
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 999px; padding: .05rem .55rem;
    margin: 0 .3rem .3rem 0; font-size: .82rem;
  }
  .tag.llm { border-style: dashed; }
  form.inline { display: inline-flex; margin: 0; }
  .chip-btn {
    background: none; border: none; color: var(--muted); cursor: pointer;
    font: inherit; font-size: .82rem; padding: 0 .1rem;
  }
  .chip-btn:hover { color: var(--accent); }
  .tags { margin-top: .45rem; }
  .add-tag select {
    font-size: .78rem; max-width: 11rem; color: var(--fg);
    background: var(--bg); border: 1px solid var(--border);
    border-radius: .3rem; padding: .05rem .2rem;
  }
  details.new-scope { margin-top: .3rem; }
  details.new-scope summary {
    cursor: pointer; color: var(--muted); font-size: .85rem;
  }
  details.new-scope form {
    display: flex; gap: .4rem; margin-top: .45rem; flex-wrap: wrap;
  }
  details.new-scope input {
    flex: 1 1 9rem; padding: .3rem .5rem; font-size: .85rem; color: var(--fg);
    background: var(--bg); border: 1px solid var(--border); border-radius: .4rem;
  }
  details.new-scope button {
    padding: .3rem .7rem; font-size: .85rem; cursor: pointer; color: var(--fg);
    background: var(--bg); border: 1px solid var(--border); border-radius: .4rem;
  }
  .muted { color: var(--muted); font-size: .85rem; margin: .25rem 0; }
  #sweep, #triage { display: flex; align-items: center; gap: .5rem;
           flex-wrap: wrap; }
  #sweep { margin-top: .6rem; }
  #sweep button, #suggest button, #triage button {
    padding: .25rem .7rem; font-size: .82rem; cursor: pointer; color: var(--fg);
    background: var(--bg); border: 1px solid var(--border); border-radius: .4rem;
  }
  #sweep button:disabled, #triage button:disabled { cursor: default; opacity: .6; }
  #sweep .muted, #triage .muted { margin: 0; }
  #suggest { margin-top: .6rem; }
  #suggest .muted { margin: .35rem 0 0; }
  form.proposals { display: block; margin: 0; }
  label.proposal {
    display: flex; align-items: flex-start; gap: .4rem;
    font-size: .85rem; margin: .3rem 0;
  }
  label.proposal input { margin: .3rem 0 0; }
  label.proposal .what { color: var(--muted); }
  #search {
    width: 100%; padding: .6rem .8rem; font-size: 1rem; color: var(--fg);
    background: var(--card); border: 1px solid var(--border);
    border-radius: .5rem; outline: none; margin-bottom: 1rem;
  }
  #search:focus { border-color: var(--accent); }
  .memory {
    background: var(--card); border: 1px solid var(--border);
    border-radius: .5rem; padding: .8rem 1rem; margin-bottom: .6rem;
  }
  .memory p { margin: 0 0 .35rem; white-space: pre-wrap; word-break: break-word; }
  .memory-head { display: flex; align-items: flex-start; gap: .6rem; }
  .memory-head p { flex: 1; }
  /* Masked text keeps the real text's line breaks and word lengths, so a card
     holds its shape when it flips — no reflow mid-recording. */
  .masked { color: var(--muted); user-select: none; }
  svg.icon { width: 1.05em; height: 1.05em; display: block; flex: none; }
  #hide-all {
    display: inline-flex; align-items: center; gap: .35rem; flex: none;
    padding: .25rem .6rem; font: inherit; font-size: .82rem; cursor: pointer;
    color: var(--muted); background: var(--bg);
    border: 1px solid var(--border); border-radius: .4rem;
  }
  #hide-all:hover, #hide-all[aria-pressed="true"] {
    color: var(--accent); border-color: var(--accent);
  }
  .eye {
    background: none; border: none; padding: .15rem 0 0; cursor: pointer;
    color: var(--muted);
  }
  .eye:hover, .eye[aria-pressed="true"] { color: var(--accent); }
  .meta {
    display: flex; align-items: baseline; gap: .6rem; flex-wrap: wrap;
    color: var(--muted); font-size: .78rem;
  }
  .reason { color: var(--muted); font-size: .8rem; margin: 0 0 .35rem; }
  #tabs { display: flex; gap: .4rem; margin-bottom: .75rem; }
  .tab {
    padding: .3rem .8rem; font: inherit; font-size: .85rem; cursor: pointer;
    color: var(--muted); background: var(--bg);
    border: 1px solid var(--border); border-radius: 999px;
  }
  .tab:hover { color: var(--accent); }
  .tab[aria-pressed="true"] {
    color: var(--accent); border-color: var(--accent); background: var(--card);
  }
  #graph { width: 100%; height: auto; display: block; touch-action: manipulation; }
  #graph text { font-size: 13px; fill: var(--fg); }
  #graph text.count { fill: var(--muted); font-size: 11px; }
  #graph .edge { stroke: var(--border); stroke-width: 1.5; }
  #graph .node { fill: var(--card); stroke: var(--accent); stroke-width: 1.5;
                 cursor: pointer; }
  #graph .node.hub { fill: var(--accent); stroke: var(--accent); cursor: default; }
  #graph .hub-label { fill: var(--bg); font-size: 12px; }
  #graph .leaf { fill: var(--accent); stroke: none; opacity: .75; }
  /* Dimming is what "drilled into one scope" looks like: the rest of the map
     stays on screen for context instead of disappearing. */
  #graph .dim { opacity: .22; }
  #map-bar {
    display: flex; align-items: center; gap: .5rem; flex-wrap: wrap;
    margin-bottom: .4rem;
  }
  #map-bar button {
    padding: .25rem .7rem; font-size: .82rem; cursor: pointer; color: var(--fg);
    background: var(--bg); border: 1px solid var(--border); border-radius: .4rem;
  }
  #map-bar .muted { margin: 0; }
  #map-detail { margin-top: .8rem; }
  .scope-summary {
    background: var(--card); border: 1px solid var(--border);
    border-radius: .5rem; padding: .6rem .9rem; margin-bottom: .5rem;
  }
  .scope-summary h3 { font-size: .9rem; margin: 0 0 .2rem; }
  .scope-summary p { margin: 0; font-size: .88rem; color: var(--muted); }
  #filter { display: flex; flex-wrap: wrap; gap: .3rem; margin: 0 0 1rem; }
  .filter-chip {
    font: inherit; font-size: .8rem; cursor: pointer; padding: .1rem .6rem;
    color: var(--muted); background: var(--bg);
    border: 1px solid var(--border); border-radius: 999px;
  }
  .filter-chip:hover { color: var(--accent); }
  .filter-chip[aria-pressed="true"] {
    color: var(--accent); border-color: var(--accent);
  }
  .filter-chip .n { opacity: .65; }
  .empty { color: var(--muted); padding: 2rem 0; text-align: center; }
</style>
</head>
<body>
<div class="wrap">
  <div class="head">
    <h1>Your personal context</h1>
    <button id="hide-all" type="button"></button>
  </div>
  <p class="who">__WHO__</p>
  <section id="scopes-panel">
    <h2>Consent scopes</h2>
    <div id="scopes"></div>
    <div id="suggest"></div>
    <div id="sweep"></div>
    <details class="new-scope">
      <summary>Create your own scope</summary>
      <form method="post" data-endpoint="scopes">
        <input type="hidden" name="action" value="create">
        <input name="name" maxlength="40" required
               placeholder="Name (e.g. journaling)">
        <input name="description" maxlength="300"
               placeholder="What it covers (optional)">
        <button type="submit">Create</button>
      </form>
    </details>
  </section>
  <section id="triage-panel">
    <h2>What&rsquo;s worth keeping</h2>
    <div id="triage"></div>
  </section>
  <input id="search" type="search" placeholder="Search your memories&hellip;" autocomplete="off">
  <nav id="tabs" hidden></nav>
  <div id="filter"></div>
  <main id="list"></main>
  <section id="map" hidden>
    <div id="map-bar"></div>
    <svg id="graph" viewBox="0 0 760 600" role="img"
         aria-label="Your memories grouped by consent scope"></svg>
    <div id="map-detail"></div>
  </section>
</div>
<script type="application/json" id="data">__DATA__</script>
<script>
  const data = JSON.parse(document.getElementById("data").textContent);
  const rows = data.memories;
  const scopes = data.scopes;
  const scopeByKey = new Map(scopes.map(s => [s.key, s]));
  rows.sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  const list = document.getElementById("list");
  const search = document.getElementById("search");
  const count = document.getElementById("count");

  // The dashboard base as the BROWSER sees it: in capability mode that is
  // /<token>/dashboard — the server only ever sees the stripped path, so
  // every form action must be derived here, never emitted as /dashboard/*.
  const dashBase = location.pathname.replace(/\\/+$/, "");
  for (const f of document.querySelectorAll("form[data-endpoint]")) {
    f.action = dashBase + "/" + f.dataset.endpoint;
  }

  function postForm(endpoint, fields) {
    const form = document.createElement("form");
    form.method = "post";
    form.action = dashBase + "/" + endpoint;
    form.className = "inline";
    for (const [name, value] of Object.entries(fields)) {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = name;
      input.value = value;
      form.appendChild(input);
    }
    return form;
  }

  function chipButton(label, title) {
    const btn = document.createElement("button");
    btn.type = "submit";
    btn.className = "chip-btn";
    btn.textContent = label;
    btn.title = title;
    return btn;
  }

  function scopeLabel(s) {
    return s.owner === "user" ? s.name : s.name + " \\u00b7 " + s.owner;
  }

  // Both background passes advance by reloading the page rather than polling.
  // Shared so that with a re-tag and a triage running at once the page still
  // reloads once every three seconds, not twice.
  let reloadPending = false;
  function scheduleReload() {
    if (reloadPending) return;
    reloadPending = true;
    setTimeout(() => location.reload(), 3000);
  }

  // --- screen privacy ----------------------------------------------------
  // Masking memory text so this page can be recorded or screenshotted in
  // public. A display setting only: it never touches the store, so it changes
  // nothing about what connected apps can read, and the real text is still in
  // the data block above. It hides memories from a camera, not from devtools.
  const HIDE_KEY = "pcl.hide-details";
  let hideAll = false;
  // memory id -> hidden, holding ONLY the memories that differ from hideAll.
  let hideOverrides = new Map();

  function loadHideState() {
    try {
      const saved = JSON.parse(localStorage.getItem(HIDE_KEY) || "{}");
      hideAll = saved.all === true;
      hideOverrides = new Map(
        Object.entries(saved.only || {}).map(([id, h]) => [id, h === true]));
    } catch {
      // Storage off (private mode) or a corrupt value: start fully visible.
      hideAll = false;
      hideOverrides = new Map();
    }
  }

  // Persisted because the sweep panel reloads this page every 3s while a
  // re-tag runs — without this, a recording would un-hide itself mid-take.
  function saveHideState() {
    try {
      localStorage.setItem(HIDE_KEY, JSON.stringify(
        { all: hideAll, only: Object.fromEntries(hideOverrides) }));
    } catch {
      // Storage unavailable: the toggles still work for this pageview.
    }
  }

  function isHidden(r) {
    return hideOverrides.has(r.id) ? hideOverrides.get(r.id) : hideAll;
  }

  // Same shape, no content: every non-space character becomes a bullet, so
  // line breaks and word lengths survive and the card doesn't reflow.
  function mask(text) {
    return text.replace(/\\S/g, "\\u2022");
  }

  const SVG_NS = "http://www.w3.org/2000/svg";

  function svgEl(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attrs)) el.setAttribute(name, value);
    return el;
  }

  // Drawn inline rather than loaded, so the page stays a single asset-free
  // document. Open eye = "details are showing"; struck-through = "masked".
  function eyeIcon(hidden) {
    const svg = svgEl("svg", {
      "class": "icon", viewBox: "0 0 24 24", fill: "none",
      stroke: "currentColor", "stroke-width": "2",
      "stroke-linecap": "round", "stroke-linejoin": "round",
      "aria-hidden": "true",
    });
    if (hidden) {
      svg.append(
        svgEl("path", { d: "M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8" +
          "a18.45 18.45 0 0 1 5.06-5.94" }),
        svgEl("path", { d: "M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8" +
          "a18.5 18.5 0 0 1-2.16 3.19" }),
        svgEl("path", { d: "M14.12 14.12a3 3 0 1 1-4.24-4.24" }),
        svgEl("line", { x1: "1", y1: "1", x2: "23", y2: "23" }),
      );
    } else {
      svg.append(
        svgEl("path", { d: "M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" }),
        svgEl("circle", { cx: "12", cy: "12", r: "3" }),
      );
    }
    return svg;
  }

  function renderHideAll() {
    const btn = document.getElementById("hide-all");
    const label = document.createElement("span");
    label.textContent = hideAll ? "Show details" : "Hide details";
    btn.replaceChildren(eyeIcon(hideAll), label);
    btn.setAttribute("aria-pressed", String(hideAll));
    btn.title = hideAll
      ? "Show every memory's text again"
      : "Mask every memory's text, for screen sharing and screenshots";
  }

  function eyeButton(r) {
    const hidden = isHidden(r);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "eye";
    btn.setAttribute("aria-pressed", String(hidden));
    btn.title = hidden ? "Show this memory" : "Hide this memory";
    btn.setAttribute("aria-label", btn.title);
    btn.appendChild(eyeIcon(hidden));
    btn.addEventListener("click", () => {
      const next = !isHidden(r);
      // Store only what differs from the master toggle, so putting a memory
      // back to the default drops its override instead of pinning it there.
      if (next === hideAll) hideOverrides.delete(r.id);
      else hideOverrides.set(r.id, next);
      saveHideState();
      refresh();
    });
    return btn;
  }

  function toggleHideAll() {
    hideAll = !hideAll;
    // "Hide details" has to mean all of them: single memories revealed during
    // the last take must not stay revealed into this one.
    hideOverrides.clear();
    saveHideState();
    renderHideAll();
    refresh();
  }

  function renderScopes() {
    const panel = document.getElementById("scopes");
    if (!scopes.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No scopes yet — connected apps register the " +
        "categories they care about, or create your own below.";
      panel.appendChild(empty);
      return;
    }
    const groups = new Map();
    for (const s of scopes) {
      if (!groups.has(s.owner)) groups.set(s.owner, []);
      groups.get(s.owner).push(s);
    }
    const owners = [...groups.keys()].sort((a, b) =>
      (a === "user") - (b === "user") || a.localeCompare(b));
    for (const owner of owners) {
      const group = document.createElement("div");
      group.className = "scope-group";
      const heading = document.createElement("h3");
      heading.textContent = owner === "user" ? "Your own scopes" : owner;
      group.appendChild(heading);
      for (const s of groups.get(owner)) {
        const chip = document.createElement("span");
        chip.className = "scope";
        if (s.description) chip.title = s.description;
        const label = document.createElement("span");
        label.textContent = s.name;
        chip.appendChild(label);
        if (owner === "user") {
          const form = postForm("scopes", { action: "delete", key: s.key });
          form.appendChild(chipButton("\\u00d7", "Delete this scope"));
          chip.appendChild(form);
        }
        group.appendChild(chip);
      }
      panel.appendChild(group);
    }
  }

  // The way out of an empty vocabulary: one pass over the memories proposing
  // categories. What comes back is the model's, so it renders as a checklist
  // of UNticked boxes — a scope key is what a future consent grant gates on,
  // and pre-ticking would make "add them all" the one-click default.
  function renderSuggest() {
    const el = document.getElementById("suggest");
    // Same gate as tagging: outside EXTRACTION_MODE=anthropic no memory is
    // sent to a model, and renderSweep already says so.
    if (!data.tagging_enabled) return;
    const suggestions = data.suggestions;
    if (suggestions.proposals.length) {
      const form = postForm("suggest", { action: "confirm" });
      form.className = "proposals";
      const intro = document.createElement("p");
      intro.className = "muted";
      intro.textContent = "Suggested from your memories \\u2014 tick the ones " +
        "you want, the rest are discarded. Adding them starts tagging your " +
        "memories with them straight away:";
      form.appendChild(intro);
      for (const p of suggestions.proposals) {
        const row = document.createElement("label");
        row.className = "proposal";
        const box = document.createElement("input");
        box.type = "checkbox";
        box.name = "key";
        box.value = p.key;
        const name = document.createElement("span");
        name.textContent = p.name;
        row.append(box, name);
        if (p.description) {
          const what = document.createElement("span");
          what.className = "what";
          what.textContent = "\\u2014 " + p.description;
          row.appendChild(what);
        }
        form.appendChild(row);
      }
      const add = document.createElement("button");
      add.type = "submit";
      add.textContent = "Add ticked scopes";
      form.appendChild(add);
      el.appendChild(form);
      return;
    }
    const form = postForm("suggest", { action: "run" });
    const btn = document.createElement("button");
    btn.type = "submit";
    btn.textContent = "Suggest scopes from my memories";
    form.appendChild(btn);
    const note = document.createElement("p");
    note.className = "muted";
    // A run that produced nothing has to read differently from never having
    // run one, or pressing the button again looks like the fix.
    note.textContent = suggestions.generated_at
      ? "That pass found no categories you don't already have."
      : "One pass over your memories, proposing categories you can then " +
        "tick to add. Nothing is added without you ticking it.";
    el.append(form, note);
  }

  // The re-tag control plus whatever the last/current sweep is doing. Status
  // is in-process on the server, so "running" advances by reloading the page
  // rather than by polling an endpoint that doesn't exist.
  // Memories a run walked past because they were already judged against the
  // current vocabulary. Worth saying: it is the difference between a pass that
  // cost a model call per memory and one that cost almost nothing.
  function skippedNote(s) {
    return s.skipped ? " (" + s.skipped + " already up to date)" : "";
  }

  function renderSweep() {
    const el = document.getElementById("sweep");
    const note = document.createElement("p");
    note.className = "muted";
    if (!data.tagging_enabled) {
      note.textContent = "Automatic tagging is off on this server " +
        "(EXTRACTION_MODE is not anthropic), so no memory is ever sent to a " +
        "model. You can still tag memories yourself below.";
      el.appendChild(note);
      return;
    }
    // Tags ARE scopes, so with none registered the button's only possible
    // outcome is a no-op. The control that fills the vocabulary sits directly
    // above; point at it rather than offering a button that can't do anything.
    if (!scopes.length) {
      note.textContent = "Nothing to tag into yet \\u2014 add some scopes above, " +
        "then come back and re-tag your memories.";
      el.appendChild(note);
      return;
    }
    const s = data.sweep || {};
    // Queued and running are both "in flight" to the person watching: the run
    // is a durable row, so it is happening whether or not a worker has reached
    // it yet — and whether or not it is THIS process doing the work.
    const running = s.state === "running" || s.state === "queued";
    const form = postForm("sweep", {});
    const btn = document.createElement("button");
    btn.type = "submit";
    btn.textContent = running ? "Re-tagging\\u2026" : "Re-tag all memories";
    btn.disabled = running;
    form.appendChild(btn);
    el.appendChild(form);
    if (running) {
      note.textContent = s.state === "queued" ? "Queued\\u2026"
        : s.total ? "Classifying " + s.processed + " of " + s.total +
            skippedNote(s) + "\\u2026"
        : "Starting\\u2026";
      scheduleReload();
    } else if (s.state === "done" && !s.scope_count) {
      // Scopes exist now (checked above) but didn't when that run happened,
      // so the stored "0 of 0" describes an empty vocabulary, not a no-op.
      note.textContent = "The last run had no scopes to tag into \\u2014 run " +
        "it again now that you have some.";
    } else if (s.state === "done") {
      note.textContent = "Last run: " + s.changed + " of " + s.total +
        " memories updated" + skippedNote(s) +
        (s.failed ? " \\u00b7 " + s.failed + " could not be tagged " +
          "(see the server logs)" : "") +
        " \\u00b7 " + fmt(s.finished_at);
    } else if (s.state === "error") {
      // all_failed is every memory failing on its own rather than the sweep
      // itself blowing up, so it gets the actionable copy instead of an
      // exception name that means nothing to the person reading it.
      note.textContent = s.error === "all_failed"
        ? "Last run could not tag any of your " + s.total + " memories. That " +
          "usually means this server can't reach the model \\u2014 check its " +
          "API key and model settings, then the server logs for the error."
        : "Last run failed (" + s.error + "). Try again.";
    } else {
      note.textContent = "Tags are derived from your memories \\u2014 re-run " +
        "this after adding or changing scopes.";
    }
    el.appendChild(note);
  }

  // The pass that decides which memories still earn their place, plus what
  // the last one did. Deliberately worded so nothing here reads like a
  // delete: it sets memories aside, and it says how to undo that.
  function renderTriage() {
    const el = document.getElementById("triage");
    const note = document.createElement("p");
    note.className = "muted";
    if (!data.triage_enabled) {
      note.textContent = "Automatic review is off on this server " +
        "(EXTRACTION_MODE is not anthropic), so no memory is ever sent to a " +
        "model. You can still set memories aside yourself.";
      el.appendChild(note);
      return;
    }
    const t = data.triage || {};
    // Same as the tagging panel: a queued run is already happening, it just
    // has not been picked up yet.
    const running = t.state === "running" || t.state === "queued";
    const form = postForm("retention", { action: "sweep" });
    const btn = document.createElement("button");
    btn.type = "submit";
    btn.textContent = running ? "Reviewing\\u2026" : "Review my memories";
    btn.disabled = running;
    form.appendChild(btn);
    el.appendChild(form);
    if (running) {
      note.textContent = t.state === "queued" ? "Queued\\u2026"
        : t.total ? "Reviewing " + t.processed + " of " + t.total +
            skippedNote(t) + "\\u2026"
        : "Starting\\u2026";
      scheduleReload();
    } else if (t.state === "done") {
      // Both directions, because a run that put memories back is the one the
      // user most needs to know happened.
      const moved = [
        t.archived ? t.archived + " set aside" : "",
        t.restored ? t.restored + " put back" : "",
      ].filter(Boolean).join(", ") || "nothing changed";
      note.textContent = "Last run over " + t.total + " memories: " + moved +
        skippedNote(t) +
        (t.failed ? " \\u00b7 " + t.failed + " could not be reviewed " +
          "(see the server logs)" : "") +
        " \\u00b7 " + fmt(t.finished_at);
    } else if (t.state === "error") {
      note.textContent = t.error === "all_failed"
        ? "Last run could not review any of your " + t.total + " memories. " +
          "That usually means this server can't reach the model \\u2014 check " +
          "its API key and model settings, then the server logs for the error."
        : "Last run failed (" + t.error + "). Try again.";
    } else {
      note.textContent = "One pass over every memory, keeping what would " +
        "inform a later decision and setting the rest aside. Nothing is " +
        "deleted \\u2014 set-aside memories move to their own tab, and you " +
        "can put any of them back.";
    }
    el.appendChild(note);
  }

  function tagRowFor(r) {
    const tags = r.tags || {};
    const tagRow = document.createElement("div");
    tagRow.className = "tags";
    for (const key of scopeKeysOf(r)) {
      const s = scopeByKey.get(key);
      const chip = document.createElement("span");
      chip.className = tags[key] === "llm" ? "tag llm" : "tag";
      chip.title = tags[key] === "llm" ? "Tagged automatically" : "Tagged by you";
      const label = document.createElement("span");
      label.textContent = scopeLabel(s);
      chip.appendChild(label);
      const form = postForm("tags",
        { action: "remove", memory_id: r.id, scope_key: key });
      form.appendChild(chipButton("\\u00d7", "Remove this tag"));
      chip.appendChild(form);
      tagRow.appendChild(chip);
    }
    const untagged = scopes.filter(s => !(s.key in tags));
    if (untagged.length) {
      const form = postForm("tags", { action: "add", memory_id: r.id });
      form.className = "inline add-tag";
      const select = document.createElement("select");
      select.name = "scope_key";
      for (const s of untagged) {
        const opt = document.createElement("option");
        opt.value = s.key;
        opt.textContent = scopeLabel(s);
        select.appendChild(opt);
      }
      form.appendChild(select);
      form.appendChild(chipButton("+ tag", "Tag this memory with the selected scope"));
      tagRow.appendChild(form);
    }
    return tagRow;
  }

  function fmt(ts) {
    if (!ts) return "";
    const d = new Date(ts);
    return isNaN(d) ? ts : d.toLocaleString();
  }

  // --- narrowing the list to a set of scopes -----------------------------

  // Which scopes the list is narrowed to. Empty means "everything", not
  // "nothing" — an untouched filter must never hide the store.
  const selectedScopes = new Set();

  // Stands for "nothing has claimed this yet". Worth filtering for in its own
  // right: it is how you find what a sweep missed, or what a new scope should
  // cover. Not a registry key, and can't collide with one — a scope key is
  // built from slugified characters and never contains a NUL.
  const UNTAGGED = "\\u0000untagged";

  // The scopes a memory actually reads as. A tag whose scope is no longer
  // registered counts as untagged here, exactly as it already does on the card.
  function scopeKeysOf(r) {
    return Object.keys(r.tags || {}).filter(k => scopeByKey.has(k));
  }

  function matchesScopes(r) {
    if (!selectedScopes.size) return true;
    const keys = scopeKeysOf(r);
    if (!keys.length) return selectedScopes.has(UNTAGGED);
    // OR, not AND: most memories carry a single scope, so an intersection
    // would answer nearly every multi-select with an empty list.
    return keys.some(k => selectedScopes.has(k));
  }

  function filterChip(key, label, n) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "filter-chip";
    btn.setAttribute("aria-pressed", selectedScopes.has(key) ? "true" : "false");
    const name = document.createElement("span");
    name.textContent = label;
    const num = document.createElement("span");
    num.className = "n";
    num.textContent = " " + n;
    btn.append(name, num);
    btn.addEventListener("click", () => {
      if (selectedScopes.has(key)) selectedScopes.delete(key);
      else selectedScopes.add(key);
      renderFilter();
      render(search.value);
    });
    return btn;
  }

  function renderFilter() {
    const el = document.getElementById("filter");
    el.replaceChildren();
    // Counts are over the OPEN TAB — the chips describe the list they sit
    // above, so switching to Set aside recounts against what's in there. They
    // deliberately ignore the text search: they say how much sits under each
    // scope, which shouldn't flicker while someone types.
    const wantArchived = tab === "archived";
    const counts = new Map();
    let untagged = 0;
    for (const r of rows.filter(r => isArchived(r) === wantArchived)) {
      const keys = scopeKeysOf(r);
      if (!keys.length) untagged++;
      for (const k of keys) counts.set(k, (counts.get(k) || 0) + 1);
    }
    // Scopes something is actually filed under — a registered scope matching
    // nothing would be a chip whose only outcome is an empty list, and the
    // panel above already says it exists — PLUS anything currently selected,
    // even where this tab holds none of it. A selection survives a tab switch,
    // so without that second clause switching to a tab that doesn't have the
    // selected scope would narrow the list with no chip to show for it and no
    // way to undo it: an untouched filter hiding the store, which is the one
    // thing this control must never do.
    const chips = scopes
      .filter(s => counts.has(s.key) || selectedScopes.has(s.key))
      .map(s => [s.key, scopeLabel(s), counts.get(s.key) || 0]);
    if (untagged || selectedScopes.has(UNTAGGED)) {
      chips.push([UNTAGGED, "Untagged", untagged]);
    }
    // One bucket narrows to itself, so the row is only worth drawing when
    // there is a choice to make — or a selection to see and undo.
    if (chips.length < 2 && !selectedScopes.size) return;
    for (const [key, label, n] of chips) el.appendChild(filterChip(key, label, n));
    if (!selectedScopes.size) return;
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "filter-chip";
    clear.textContent = "Clear";
    clear.addEventListener("click", () => {
      selectedScopes.clear();
      renderFilter();
      render(search.value);
    });
    el.appendChild(clear);
  }

  function isArchived(r) {
    return (r.retention || {}).state === "archived";
  }

  // Why a memory is out of the way, in the words of whoever put it there.
  // An automatic decision says it was automatic: that is the one the user
  // might want to argue with, and the button beside it is how they do.
  function reasonLine(r) {
    const retention = r.retention || {};
    const why = document.createElement("p");
    why.className = "reason";
    why.textContent = retention.by_user
      ? "Set aside by you"
      : "Set aside automatically" +
        (retention.reason ? " \\u2014 " + retention.reason : "");
    return why;
  }

  function retentionForm(r) {
    const archived = isArchived(r);
    const form = postForm("retention",
      { action: archived ? "keep" : "archive", memory_id: r.id });
    form.appendChild(chipButton(
      archived ? "Keep" : "Set aside",
      archived
        ? "Put this back \\u2014 searches will return it again"
        : "Stop this coming back in searches. It moves to the set-aside " +
          "tab, and nothing is deleted."));
    return form;
  }

  function memoryCard(r) {
    const card = document.createElement("div");
    card.className = "memory";
    const hidden = isHidden(r);
    const text = document.createElement("p");
    text.textContent = hidden ? mask(r.text) : r.text;
    if (hidden) text.className = "masked";
    const head = document.createElement("div");
    head.className = "memory-head";
    head.append(text, eyeButton(r));
    card.appendChild(head);
    if (isArchived(r)) card.appendChild(reasonLine(r));
    const meta = document.createElement("div");
    meta.className = "meta";
    const when = document.createElement("span");
    when.textContent = [fmt(r.created_at), r.source && "from " + r.source]
      .filter(Boolean).join(" · ");
    meta.append(when, retentionForm(r));
    card.appendChild(meta);
    const tagRow = tagRowFor(r);
    if (tagRow.childNodes.length) card.appendChild(tagRow);
    return card;
  }

  // Which view is open. Persisted for the reason the mask toggles are: the
  // panels above reload this page every few seconds while a pass runs, and
  // being thrown back to the main list mid-review would make the set-aside
  // tab unusable exactly when someone is reviewing what a pass just did.
  const TAB_KEY = "pcl.tab";
  const TABS = ["active", "archived", "map"];
  let tab = "active";

  function loadTab() {
    try {
      const held = localStorage.getItem(TAB_KEY);
      tab = TABS.includes(held) ? held : "active";
    } catch {
      tab = "active";  // storage off (private mode)
    }
  }

  function saveTab() {
    try {
      localStorage.setItem(TAB_KEY, tab);
    } catch {
      // Storage unavailable: the tabs still work for this pageview.
    }
  }

  // The map is a tab rather than a second view switcher: the bar already
  // decides what shows below it, and two competing controls for that would
  // read worse than either. "Set aside" is still only offered once something
  // has been, but the bar itself now always has somewhere to go.
  function renderTabs() {
    const el = document.getElementById("tabs");
    const archived = rows.filter(r => isArchived(r)).length;
    el.hidden = false;
    if (!archived && tab === "archived") tab = "active";
    el.replaceChildren();
    const entries = [
      ["active", "In your context (" + (rows.length - archived) + ")",
       "What a connected app searching your memories is answered from"],
    ];
    if (archived) {
      entries.push(["archived", "Set aside (" + archived + ")",
        "Memories searches no longer return. Nothing here is deleted."]);
    }
    entries.push(["map", "Map",
      "Your memories grouped by scope, and what each one holds"]);
    for (const [name, label, why] of entries) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tab";
      btn.textContent = label;
      btn.setAttribute("aria-pressed", String(tab === name));
      btn.title = why;
      btn.addEventListener("click", () => {
        tab = name;
        saveTab();
        showTab();
      });
      el.appendChild(btn);
    }
  }

  // Re-render whichever view is on screen. The eye toggles reach for this
  // rather than render() directly: memory cards render in both the list and
  // under the map now, so redrawing the list would leave the map stale.
  function refresh() {
    if (tab === "map") renderMap();
    else render(search.value);
  }

  // The search box and the scope chips belong to the two list tabs; the map
  // narrows by clicking a scope and would be answering two questions at once
  // if they stayed on screen.
  function showTab() {
    const map = tab === "map";
    document.getElementById("map").hidden = !map;
    for (const id of ["search", "filter", "list"]) {
      document.getElementById(id).hidden = map;
    }
    renderTabs();
    // Chip counts are per-tab, so they move with it.
    if (!map) renderFilter();
    refresh();
  }

  // --- the map view ------------------------------------------------------

  // Scopes fan out from the user at the centre, sized by how much is filed
  // under each, so the shape of the store reads at a glance: which areas are
  // dense, which are thin, what nothing has claimed yet. Opening one fans its
  // memories out as leaves and dims the rest — the others stay on screen, so
  // drilling in never loses the context it was drilled into from.
  const GEO = { w: 760, h: 600, cx: 380, cy: 285, ring: 155, leaf: 66 };
  // Enough leaves to read density from; past this the arc turns into a
  // scribble and the cards below are the honest way to see them.
  const MAX_LEAVES = 14;

  let openScope = null;

  // Every bucket the map draws: the registry's populated scopes, plus what
  // nothing has claimed. An untagged pile is the normal state before a sweep,
  // and a map that quietly dropped it would misstate how much is held.
  function buckets() {
    const active = rows.filter(r => !isArchived(r));
    const counts = new Map();
    const untagged = [];
    for (const r of active) {
      const keys = scopeKeysOf(r);
      if (!keys.length) untagged.push(r);
      for (const k of keys) {
        if (!counts.has(k)) counts.set(k, []);
        counts.get(k).push(r);
      }
    }
    const out = scopes.filter(s => counts.has(s.key)).map(s => ({
      key: s.key, label: scopeLabel(s), memories: counts.get(s.key),
    }));
    if (untagged.length) {
      out.push({ key: UNTAGGED, label: "Untagged", memories: untagged });
    }
    return out;
  }

  function summaryFor(key) {
    const found = (data.summaries.summaries || []).find(s => s.key === key);
    return found ? found.text : "";
  }

  function countLabel(n) {
    return n + (n === 1 ? " memory" : " memories");
  }

  function nodeRadius(n) {
    // Area-proportional rather than radius-proportional: a scope holding four
    // times as much should look four times as big, not sixteen.
    return Math.max(15, Math.min(38, 11 + Math.sqrt(n) * 4));
  }

  function graphText(x, y, str, cls, anchor) {
    const t = svgEl("text", { x: x, y: y, "text-anchor": anchor || "middle" });
    if (cls) t.setAttribute("class", cls);
    t.textContent = str;
    return t;
  }

  function renderGraph(list) {
    const svg = document.getElementById("graph");
    svg.replaceChildren();
    if (!list.length) return;
    const positions = list.map((b, i) => {
      // Start at twelve o'clock so a single scope sits above the hub rather
      // than beside it, where its label would collide with the hub's.
      const a = -Math.PI / 2 + (i * 2 * Math.PI) / list.length;
      return { ...b, a,
        x: GEO.cx + GEO.ring * Math.cos(a),
        y: GEO.cy + GEO.ring * Math.sin(a) };
    });

    for (const p of positions) {
      const edge = svgEl("line",
        { x1: GEO.cx, y1: GEO.cy, x2: p.x, y2: p.y });
      edge.setAttribute("class",
        "edge" + (openScope && openScope !== p.key ? " dim" : ""));
      svg.appendChild(edge);
    }

    const hub = svgEl("circle", { cx: GEO.cx, cy: GEO.cy, r: 34 });
    hub.setAttribute("class", "node hub");
    const active = rows.filter(r => !isArchived(r)).length;
    svg.append(hub,
      graphText(GEO.cx, GEO.cy + 1, "You", "hub-label"),
      graphText(GEO.cx, GEO.cy + 15, String(active), "hub-label"));

    for (const p of positions) {
      const dim = openScope && openScope !== p.key;
      if (openScope === p.key) renderLeaves(svg, p);
      const node = svgEl("circle",
        { cx: p.x, cy: p.y, r: nodeRadius(p.memories.length) });
      node.setAttribute("class", "node" + (dim ? " dim" : ""));
      const why = svgEl("title", {});
      why.textContent = summaryFor(p.key) ||
        p.memories.length + " memories filed under " + p.label;
      node.appendChild(why);
      node.addEventListener("click", () => {
        openScope = openScope === p.key ? null : p.key;
        renderMap();
      });
      svg.appendChild(node);

      // Labels sit outside the node, pushed along whichever way the node
      // faces, so they never run back across the middle of the map.
      const r = nodeRadius(p.memories.length);
      const out = r + 14;
      const lx = p.x + out * Math.cos(p.a);
      const ly = p.y + out * Math.sin(p.a);
      const anchor = Math.cos(p.a) > 0.3 ? "start"
        : Math.cos(p.a) < -0.3 ? "end" : "middle";
      const label = graphText(lx, ly, p.label, dim ? "dim" : "", anchor);
      const n = graphText(lx, ly + 14, countLabel(p.memories.length),
        "count" + (dim ? " dim" : ""), anchor);
      svg.append(label, n);
    }
  }

  function renderLeaves(svg, p) {
    const shown = p.memories.slice(0, MAX_LEAVES);
    // Fanned outward, away from the hub, so leaves never land on top of it.
    const spread = Math.PI * 1.1;
    const step = shown.length > 1 ? spread / (shown.length - 1) : 0;
    const start = p.a - spread / 2;
    shown.forEach((r, i) => {
      const a = shown.length > 1 ? start + i * step : p.a;
      const lx = p.x + GEO.leaf * Math.cos(a);
      const ly = p.y + GEO.leaf * Math.sin(a);
      const edge = svgEl("line", { x1: p.x, y1: p.y, x2: lx, y2: ly });
      edge.setAttribute("class", "edge");
      const dot = svgEl("circle", { cx: lx, cy: ly, r: 5 });
      dot.setAttribute("class", "leaf");
      // Native tooltip, and it honours the masking toggle — a map left open
      // on a shared screen must not leak what the cards below are hiding.
      const what = svgEl("title", {});
      what.textContent = isHidden(r) ? mask(r.text) : r.text;
      dot.appendChild(what);
      svg.append(edge, dot);
    });
  }

  function summaryCard(key, label, text, n) {
    const card = document.createElement("div");
    card.className = "scope-summary";
    const h = document.createElement("h3");
    h.textContent = label + " · " + countLabel(n);
    const p = document.createElement("p");
    p.textContent = text || (key === UNTAGGED
      ? "Nothing has claimed these yet — run a re-tag, or add a scope that covers them."
      : "No summary yet.");
    card.append(h, p);
    return card;
  }

  // The button that fills the summaries in, plus how old they are — these
  // describe memory text that keeps changing, so their age is part of how
  // much to trust them.
  function renderMapBar(list) {
    const bar = document.getElementById("map-bar");
    bar.replaceChildren();
    const note = document.createElement("p");
    note.className = "muted";
    if (!data.tagging_enabled) {
      note.textContent = "Summaries are off on this server " +
        "(EXTRACTION_MODE is not anthropic), so nothing is sent to a model. " +
        "The map still shows what is filed under each scope.";
      bar.appendChild(note);
      return;
    }
    if (!list.length) {
      note.textContent = "Nothing to map yet — once memories are stored and " +
        "tagged, they show up here grouped by scope.";
      bar.appendChild(note);
      return;
    }
    const form = postForm("summarize", {});
    const btn = document.createElement("button");
    btn.type = "submit";
    btn.textContent = data.summaries.generated_at
      ? "Re-summarize my scopes" : "Summarize my scopes";
    form.appendChild(btn);
    note.textContent = data.summaries.generated_at
      ? "Summarized " + fmt(data.summaries.generated_at)
      : "One pass over your memories, describing what sits under each scope.";
    bar.append(form, note);
  }

  function renderMap() {
    const list = buckets();
    if (openScope && !list.some(b => b.key === openScope)) openScope = null;
    renderMapBar(list);
    renderGraph(list);
    const detail = document.getElementById("map-detail");
    detail.replaceChildren();
    if (!list.length) return;
    const open = list.find(b => b.key === openScope);
    if (!open) {
      // Overview: every scope's summary, which is the part of the map you
      // read rather than look at.
      for (const b of list) {
        detail.appendChild(
          summaryCard(b.key, b.label, summaryFor(b.key), b.memories.length));
      }
      return;
    }
    detail.appendChild(
      summaryCard(open.key, open.label, summaryFor(open.key),
                  open.memories.length));
    if (open.memories.length > MAX_LEAVES) {
      const more = document.createElement("p");
      more.className = "muted";
      more.textContent = "Showing " + MAX_LEAVES + " of " +
        open.memories.length + " as leaves; all of them are below.";
      detail.appendChild(more);
    }
    for (const r of open.memories) detail.appendChild(memoryCard(r));
  }

  function render(filter) {
    const q = filter.trim().toLowerCase();
    const wantArchived = tab === "archived";
    const inTab = rows.filter(r => isArchived(r) === wantArchived);
    const shown = inTab.filter(r =>
      (!q || r.text.toLowerCase().includes(q)) && matchesScopes(r));
    // Whether the list is answering a question or just showing the tab —
    // which is what decides between "3 of 142" and a bare total.
    const narrowed = Boolean(q) || selectedScopes.size > 0;
    list.replaceChildren();
    count.textContent = narrowed ? shown.length + " of " + inTab.length
                                 : String(inTab.length);
    if (!shown.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = !inTab.length
        ? (wantArchived ? "Nothing has been set aside."
           : rows.length ? "Every memory is set aside — see the other tab."
           : "Nothing stored yet — connect a client and start talking.")
        : selectedScopes.size && !q ? "Nothing here is filed under that."
        : "No memories match that search.";
      list.appendChild(empty);
      return;
    }
    for (const r of shown) list.appendChild(memoryCard(r));
  }

  loadHideState();
  loadTab();
  renderScopes();
  renderSuggest();
  renderSweep();
  renderTriage();
  renderHideAll();
  renderFilter();
  document.getElementById("hide-all").addEventListener("click", toggleHideAll);
  search.addEventListener("input", () => render(search.value));
  showTab();
</script>
</body>
</html>
"""


def render_page(
    rows: list[dict],
    scopes: list[ConsentScope],
    *,
    user_label: str,
    show_logout: bool,
    sweep: Optional[dict] = None,
    suggestions: Optional[dict] = None,
    summaries: Optional[dict] = None,
    tagging_enabled: bool = False,
    triage: Optional[dict] = None,
    triage_enabled: bool = False,
) -> str:
    """Render the full memory-browser document for one user's rows + scopes.

    ``sweep`` is the in-process status of that user's re-tagging run (see
    consent.tagging.SweepStatus), ``suggestions`` the scope candidates waiting
    for them to tick (consent.discovery.ProposalSet), ``triage`` the status of
    their last retention pass (curation.sweep.TriageStatus), and the two
    ``*_enabled`` flags say whether a model can be called here at all — each
    panel explains its off state rather than offering a button that would do
    nothing.

    ``rows`` is the WHOLE store, archived memories included: this page is the
    one place a set-aside memory is still visible, and it splits them out
    itself rather than being handed a pre-filtered list.
    """
    who = f"{html.escape(user_label)} &middot; <span id=\"count\"></span> memories"
    if show_logout:
        who += " &middot; <a href=\"/dashboard/logout\">Sign out</a>"
    data = {
        "memories": _rows_to_payload(rows),
        "scopes": _scopes_to_payload(scopes),
        "sweep": sweep or {"state": "idle"},
        "suggestions": suggestions or {"proposals": [], "generated_at": ""},
        "summaries": summaries or {"summaries": [], "generated_at": ""},
        "tagging_enabled": bool(tagging_enabled),
        "triage": triage or {"state": "idle"},
        "triage_enabled": bool(triage_enabled),
    }
    return _PAGE.replace("__WHO__", who).replace("__DATA__", _json_for_script(data))
