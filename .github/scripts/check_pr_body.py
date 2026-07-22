#!/usr/bin/env python3
"""Check that a PR body follows .github/pull_request_template.md.

Reads the PR body from the PR_BODY env var and exits non-zero if any required
section is missing or left empty (i.e. only the template's HTML-comment prompt,
or blank). The Ticket section must also carry a Linear issue link.

Kept deliberately simple: it verifies the *shape* is filled in, not the prose.
"""

import os
import re
import sys

REQUIRED_SECTIONS = [
    "Ticket",
    "Description",
    "What changed",
    "Why",
    "Testing",
    "Deploy impact",
]


def _strip_comments(text: str) -> str:
    """Remove HTML comments (the template's prompts) so an untouched section
    reads as empty."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def _split_sections(body: str) -> dict[str, str]:
    """Map lowercased '## Heading' -> the text between it and the next heading."""
    sections: dict[str, str] = {}
    headings = list(re.finditer(r"(?m)^\s*##\s+(.+?)\s*$", body))
    for i, match in enumerate(headings):
        name = match.group(1).strip().lower()
        start = match.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        sections[name] = body[start:end]
    return sections


def check(body: str) -> list[str]:
    sections = _split_sections(body)
    errors: list[str] = []

    for name in REQUIRED_SECTIONS:
        key = name.lower()
        if key not in sections:
            errors.append(f"missing section: ## {name}")
            continue
        content = _strip_comments(sections[key])
        if not content.strip():
            errors.append(
                f"section '## {name}' is empty — fill it in, don't leave only "
                "the template prompt"
            )
            continue
        # Testing is a checklist: if it has checkboxes, at least one must be
        # ticked (the N/A box counts) so it can't pass all-unchecked.
        if key == "testing":
            boxes = re.findall(r"(?mi)^\s*[-*]\s*\[([ x])\]", content)
            if boxes and not any(b.lower() == "x" for b in boxes):
                errors.append(
                    "section '## Testing' has a checklist but no box is checked "
                    "— tick what you did (or the N/A box)"
                )

    ticket = _strip_comments(sections.get("ticket", "")).strip()
    if ticket and "linear.app/" not in ticket:
        errors.append("section '## Ticket' must contain a Linear issue link (linear.app/...)")

    return errors


def main() -> int:
    body = os.environ.get("PR_BODY", "") or ""
    errors = check(body)
    if errors:
        print("PR body does not follow .github/pull_request_template.md:")
        for err in errors:
            print(f"  - {err}")
            print(f"::error::PR body: {err}")
        return 1
    print("OK: PR body follows the template.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
