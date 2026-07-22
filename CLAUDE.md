# personal_mem — conventions for Claude

## Pull requests

When opening a PR, follow `.github/pull_request_template.md`. Specifically:

- **Title** must be `<ticket>: <short description>`, where `<ticket>` is the
  Linear issue id (e.g. `PER-7: add tests and tool error handling`). GitHub
  can't enforce this from the template, so it's on the author to match it.
- **Description** must contain these sections, matching the template:
  - **Ticket** — a link to the Linear issue (e.g.
    `https://linear.app/personal-context-mcp/issue/PER-7`).
  - **Description** — what the PR is.
  - **What changed** — the concrete changes.
  - **Why** — motivation / ticket context / trade-offs.
  - **Testing** — how it was verified (ruff / pyright / pytest, tests added,
    manual checks); "N/A" only for docs-only changes.
  - **Deploy impact** — env/schema/dependency/flag changes, or "None". No
    deploy happens without an explicit go-ahead.
- Match the template's structure rather than writing an ad-hoc body.

<!-- Room to grow: branch naming, test/lint expectations (see CI in
     .github/workflows/ci.yml and the ruff config in pyproject.toml), etc. -->
