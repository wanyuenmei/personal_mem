"""End-to-end smoke test: prove mem0 read/write works before wiring MCP.

Run:  python scripts/smoke_test.py
It writes a couple of memories, then searches them back.
"""

from context_layer.config import DEFAULT_USER_ID, EXTRACTION_MODE, infer_enabled
from context_layer.memory import ContextStore


def main() -> None:
    print(f"EXTRACTION_MODE={EXTRACTION_MODE}  (infer={infer_enabled()})")
    store = ContextStore()

    print("\n-- add --")
    print(store.add("I prefer dark roast coffee, no sugar.", user_id=DEFAULT_USER_ID, scope="dietary"))
    print(store.add(
        "I'm shopping for slim-fit selvedge denim in a 32 waist.",
        user_id=DEFAULT_USER_ID,
        scope="shopping",
    ))

    print("\n-- search: 'what coffee do I like?' --")
    for r in store.search("what coffee do I like?", user_id=DEFAULT_USER_ID):
        print("  ", r.get("memory") or r, "| meta:", r.get("metadata"))

    print("\n-- search scoped to 'shopping' --")
    for r in store.search("jeans", user_id=DEFAULT_USER_ID, scope="shopping"):
        print("  ", r.get("memory") or r, "| meta:", r.get("metadata"))

    print("\nOK")


if __name__ == "__main__":
    main()
