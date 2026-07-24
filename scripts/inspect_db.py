"""Dump everything stored for a user — the 'audit your AI memory' view, in miniature.

Run:  uv run python scripts/inspect_db.py [user_id]
"""

import sys

from context_layer.config import DEFAULT_USER_ID, EXTRACTION_MODE
from context_layer.memory import ContextStore


def main() -> None:
    user_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_USER_ID
    store = ContextStore()
    rows = store.all(user_id=user_id)

    print(f"user_id={user_id}  mode={EXTRACTION_MODE}  total={len(rows)} memories\n")
    for i, r in enumerate(rows, 1):
        text = r.get("memory") or r.get("text") or str(r)
        created = r.get("created_at", "")
        print(f"{i:>2}. {text}")
        if created:
            print(f"      created: {created}   id: {r.get('id', '')}")


if __name__ == "__main__":
    main()
