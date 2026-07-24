"""Server-side rendering for the memory browser page.

One self-contained HTML document: inline CSS + JS, no external assets, no
frontend build step. The memory rows are embedded as a JSON <script> block and
rendered client-side with textContent — never innerHTML — so memory text (which
any connected client can write, and is therefore attacker-influenceable) can't
inject markup into the page. The JSON embed itself escapes "/" so no memory
containing "</script>" can break out of the data block.

Search is client-side substring filtering over the full list — fine for the
size of a personal store, and it keeps this page a pure read of store.all()
with zero extra endpoints.
"""

import html
import json


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
            }
        )
    return payload


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
  .who { color: var(--muted); font-size: .9rem; margin: 0 0 1.25rem; }
  .who a { color: var(--accent); }
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
  <input id="search" type="search" placeholder="Search your memories&hellip;" autocomplete="off">
  <main id="list"></main>
</div>
<script type="application/json" id="data">__DATA__</script>
<script>
  const rows = JSON.parse(document.getElementById("data").textContent);
  rows.sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  const list = document.getElementById("list");
  const search = document.getElementById("search");
  const count = document.getElementById("count");

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
      list.appendChild(card);
    }
  }

  search.addEventListener("input", () => render(search.value));
  render("");
</script>
</body>
</html>
"""


def render_page(rows: list[dict], *, user_label: str, show_logout: bool) -> str:
    """Render the full memory-browser document for one user's rows."""
    who = f"{html.escape(user_label)} &middot; <span id=\"count\"></span> memories"
    if show_logout:
        who += " &middot; <a href=\"/dashboard/logout\">Sign out</a>"
    return _PAGE.replace("__WHO__", who).replace(
        "__DATA__", _json_for_script(_rows_to_payload(rows))
    )
