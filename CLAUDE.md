# personal_mem — conventions for Claude

## Naming & references

- **No milestone references anywhere.** Don't mention internal roadmap
  milestones (e.g. `M2.2`, `M4`, `M2.3`) in code comments, docstrings, PR
  titles, or PR descriptions — they age badly and mean nothing to a future
  reader. Name the concrete thing ("the OAuth layer", "the consent layer")
  and use Linear ticket ids (`PER-N`) when you need a pointer.
- **Don't link Linear documents from repo files.** Keep the repo
  self-contained — internal planning/strategy lives in Linear docs (Founding
  Brief, Decision log, Architecture narrative); README, `ARCHITECTURE.md`, and
  other repo files shouldn't point at them. (PR *descriptions* still link the
  Linear ticket — that's issue traceability, not a doc link.)

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
- **No milestone references** in the title or description (see Naming &
  references above).
- **Keep descriptions succinct.** Say just enough to review the diff. Put
  deeper background, rationale, and history in the Linear ticket, not the PR
  body — link the ticket rather than restating it.

CI enforces both: `.github/workflows/pr-title.yml` checks the title format,
and `.github/workflows/pr-body.yml` (via `.github/scripts/check_pr_body.py`)
checks the required sections are present and filled in.

## Branches & merge state

- **Check a PR's merge state before pushing to its branch.** A merged PR is
  finished: pushing follow-up commits to its branch strands them (they land
  after the merge and never reach `main`), and editing a merged PR's body is
  misleading. Before pushing follow-up work or updating a PR, confirm it's
  still open; if it has merged, branch fresh from the latest `main` and open a
  new PR for the follow-up.

<!-- Room to grow: test/lint expectations (see CI in
     .github/workflows/ci.yml and the ruff config in pyproject.toml), etc. -->
