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
plus the registry.
"""

import html
import json

from context_layer.consent import ConsentScope, active_tags


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
  h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
  h2 { font-size: 1.05rem; margin: 0 0 .35rem; }
  .who { color: var(--muted); font-size: .9rem; margin: 0 0 1.25rem; }
  .who a { color: var(--accent); }
  #scopes-panel {
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
  .meta { color: var(--muted); font-size: .78rem; }
  .empty { color: var(--muted); padding: 2rem 0; text-align: center; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Your personal context</h1>
  <p class="who">__WHO__</p>
  <section id="scopes-panel">
    <h2>Consent scopes</h2>
    <div id="scopes"></div>
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
  <input id="search" type="search" placeholder="Search your memories&hellip;" autocomplete="off">
  <main id="list"></main>
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

  function tagRowFor(r) {
    const tags = r.tags || {};
    const tagRow = document.createElement("div");
    tagRow.className = "tags";
    for (const key of Object.keys(tags)) {
      const s = scopeByKey.get(key);
      if (!s) continue; // scope no longer registered: reads as untagged
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

  function render(filter) {
    const q = filter.trim().toLowerCase();
    const shown = q ? rows.filter(r => r.text.toLowerCase().includes(q)) : rows;
    list.replaceChildren();
    count.textContent = q ? shown.length + " of " + rows.length : String(rows.length);
    if (!shown.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = rows.length
        ? "No memories match that search."
        : "Nothing stored yet — connect a client and start talking.";
      list.appendChild(empty);
      return;
    }
    for (const r of shown) {
      const card = document.createElement("div");
      card.className = "memory";
      const text = document.createElement("p");
      text.textContent = r.text;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = [fmt(r.created_at), r.source && "from " + r.source]
        .filter(Boolean).join(" · ");
      card.append(text, meta);
      const tagRow = tagRowFor(r);
      if (tagRow.childNodes.length) card.appendChild(tagRow);
      list.appendChild(card);
    }
  }

  renderScopes();
  search.addEventListener("input", () => render(search.value));
  render("");
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
) -> str:
    """Render the full memory-browser document for one user's rows + scopes."""
    who = f"{html.escape(user_label)} &middot; <span id=\"count\"></span> memories"
    if show_logout:
        who += " &middot; <a href=\"/dashboard/logout\">Sign out</a>"
    data = {"memories": _rows_to_payload(rows), "scopes": _scopes_to_payload(scopes)}
    return _PAGE.replace("__WHO__", who).replace("__DATA__", _json_for_script(data))
