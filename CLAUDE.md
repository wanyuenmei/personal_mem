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
  - **Testing** — a checklist of what was verified (ruff / pyright / pytest /
    manual); tick at least one box (use the N/A box for docs/config-only).
  - **Deploy impact** — env/schema/dependency/flag changes, or "None". No
    deploy happens without an explicit go-ahead.
- Match the template's structure rather than writing an ad-hoc body.
- **No milestone mentions.** Don't reference internal roadmap milestones
  (e.g. `M2.2`, `M4`) in the title or description — describe the change on its
  own terms. Ticket ids (`PER-N`) are fine; milestone shorthand ages badly and
  means nothing to someone reading the diff later.
- **Keep descriptions succinct.** Say just enough to review the diff. Put
  deeper background, rationale, and history in the Linear ticket, not the PR
  body — link the ticket rather than restating it.

CI enforces both: `.github/workflows/pr-title.yml` checks the title format,
and `.github/workflows/pr-body.yml` (via `.github/scripts/check_pr_body.py`)
checks the required sections are present and filled in.

<!-- Room to grow: branch naming, test/lint expectations (see CI in
     .github/workflows/ci.yml and the ruff config in pyproject.toml), etc. -->
