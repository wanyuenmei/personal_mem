"""Backfill the memory store from a Claude data export.

Parse an export → infer a scope per conversation → extract + store via mem0.
A full import is the most expensive operation in the product, so this previews
an estimate and only runs when you pass --yes.

Run:
  python scripts/backfill.py <export> [--scope S] [--limit N] [--user-id U] [--yes]

<export> is the conversations.json file, the unzipped export directory, or the
raw export .zip. Without --yes it prints the estimate and stops (a dry run).
"""

import argparse
import sys

from context_layer.config import DEFAULT_USER_ID, EXTRACTION_MODE, infer_enabled
from context_layer.ingest import estimate, parse_claude_export, run_backfill
from context_layer.memory import ContextStore

# --extractor → the per-run `infer` override passed to the store.
#   auto: follow EXTRACTION_MODE (infer_enabled())
#   llm:  force LLM fact-extraction
#   none: 0-cost — store raw, no LLM (for testing the pipeline)
_EXTRACTOR_INFER = {"auto": None, "llm": True, "none": False}


def _progress(i: int, total: int, conv, scope: str, n_facts: int) -> None:
    title = conv.title or "(untitled)"
    print(f"[{i}/{total}] {title[:60]} → scope={scope} ({n_facts} facts)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill the memory store from a Claude data export."
    )
    parser.add_argument(
        "export", help="conversations.json, the export directory, or the export .zip"
    )
    parser.add_argument(
        "--extractor",
        choices=["auto", "llm", "none"],
        default="auto",
        help="fact extractor: 'auto' follows EXTRACTION_MODE, 'llm' forces LLM "
        "extraction, 'none' stores raw with no LLM (0-cost, for testing)",
    )
    parser.add_argument(
        "--scope",
        default=None,
        help="force a fixed scope for all imported facts (skips per-conversation inference)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="import at most N conversations"
    )
    parser.add_argument(
        "--user-id",
        default=DEFAULT_USER_ID,
        help=f"target user id (default: {DEFAULT_USER_ID})",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="actually run the import; without it, only the estimate is printed",
    )
    args = parser.parse_args(argv)

    infer = _EXTRACTOR_INFER[args.extractor]
    if args.extractor == "llm" and EXTRACTION_MODE not in ("anthropic", "ollama"):
        parser.error(
            f"--extractor llm needs EXTRACTION_MODE=anthropic or ollama "
            f"(currently {EXTRACTION_MODE!r}); no real extraction LLM is configured."
        )

    conversations = parse_claude_export(args.export)

    # Will fact-extraction actually run this pass? (drives the cost estimate.)
    extracting = infer_enabled() if infer is None else infer
    # Scope inference is itself an LLM call, so it only runs on a paid pass with
    # no forced --scope. A 0-cost pass skips it and falls back to a fixed scope.
    inferring_scope = args.scope is None and extracting and EXTRACTION_MODE == "anthropic"
    run_scope = args.scope if args.scope is not None or inferring_scope else "general"

    est = estimate(
        conversations,
        with_extraction=extracting,
        with_scope_inference=inferring_scope,
        limit=args.limit,
    )

    call_parts = (["extraction"] if extracting else []) + (
        ["scope inference"] if inferring_scope else []
    )
    if args.scope is not None:
        scope_desc = f"fixed={args.scope}"
    elif inferring_scope:
        scope_desc = "inferred per conversation"
    else:
        scope_desc = f"fixed={run_scope} (no inference)"
    spending = EXTRACTION_MODE == "anthropic" and (extracting or inferring_scope)

    print(f"Source:            {args.export}")
    print(f"Parsed:            {len(conversations)} conversations")
    print(f"To import:         {est.conversations} conversations, {est.messages} messages")
    print(f"Approx input tok:  {est.approx_input_tokens:,}")
    print(f"LLM calls:         ~{est.llm_calls} ({' + '.join(call_parts) or 'none'})")
    print(f"Approx cost:       ~${est.approx_cost_usd:.2f}  (rough input-token estimate)")
    print(f"Extractor:         {args.extractor} ({'LLM' if extracting else '0-cost, raw store'})")
    warn = "  ⚠ real Anthropic spend" if spending else ""
    print(f"Extraction mode:   {EXTRACTION_MODE}{warn}")
    print(f"Scope:             {scope_desc}")

    if not args.yes:
        print("\nDry run — nothing imported. Re-run with --yes to execute.")
        return 0

    print("\nImporting…")
    store = ContextStore()
    result = run_backfill(
        conversations,
        store,
        args.user_id,
        scope=run_scope,
        limit=args.limit,
        infer=infer,
        on_progress=_progress,
    )
    print(
        f"\nDone. {result.conversations} conversations · "
        f"{result.facts_added} facts added · "
        f"{result.skipped} skipped · {result.failures} failed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
