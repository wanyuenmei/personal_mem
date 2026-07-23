# personal_mem — conventions for Claude

## Naming & references

- **No milestone references anywhere.** Don't mention internal roadmap
  milestones (e.g. `M2.2`, `M4`, `M2.3`) in code comments, docstrings, PR
  titles, or PR descriptions — they age badly and mean nothing to a future
  reader. Name the concrete thing ("the OAuth layer", "the consent layer")
  and use Linear ticket ids (`PER-N`) when you need a pointer.
- **Describe by context and intent, not by opaque external labels.** Say what
  a value means or what code does. Lookupable references are fine (Linear
  ticket ids, incidents, dates); opaque tracking-artifact labels (sheet-row
  ids, Figma frame names, and the like) are not — fold their meaning into
  plain prose.
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
  A small correction that doesn't warrant its own Linear issue may instead use
  `fix: <short description>` and omit the **Ticket** section below; every other
  section still applies. Reach for a ticket by default — `fix:` is the
  exception, not a way to skip writing one.
- **Description** must contain these sections, matching the template:
  - **Ticket** — a link to the Linear issue (e.g.
    `https://linear.app/personal-context-mcp/issue/PER-7`). Omitted only on a
    `fix:` PR.
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
- **Keep the description in sync with the diff.** Whenever you push to the
  branch (amend, follow-up commit, force-push), update the PR description so it
  matches the current diff — never leave it describing a prior approach.
- **Before/after table for changed values.** When a PR changes existing copy,
  config values, thresholds, enum mappings, or renamed identifiers, add a small
  table (context | before | after, plus a notes column when a caveat matters)
  describing each row by what it *is / does* — reviewable at a glance without
  reading the diff line by line.
- **Pre-PR review pass.** Before pushing a branch for review, re-read the whole
  diff: confirm it's clean (no leftover debug, dead code, or stray changes) and
  consider whether it can be simplified (dedupe, drop needless abstraction,
  tighten control flow). Report findings — don't push straight from "tests
  green".

CI enforces the title format (`.github/workflows/pr-title.yml`) and the
required sections (`.github/workflows/pr-body.yml` via
`.github/scripts/check_pr_body.py`).

## Commits

- **Squash while the PR has no human review yet.** Combine extra commits from
  the same scope via `git commit --amend` or a rebase — aim for one commit per
  PR (one commit ↔ one Linear ticket / logical change) in the pre-review
  window. Merges are squash anyway, but keep the pre-review history clean.
- **Stop rewriting history once a human reviews or comments.** From the first
  human review/comment onward, add follow-up commits instead of force-pushing,
  so comments keep their diff anchors. CI/bot activity doesn't count — check the
  PR's reviews/comments before amending or force-pushing an already-open PR.
- **Rebase onto latest `main` before pushing — but only pre-review.** Rebase so
  the diff reflects current `main` (avoids stale-base surprises). Once a human
  has reviewed, don't rebase/force-push (it breaks their comment anchors) —
  merge `main` in or add follow-up commits instead.

## Tests

- **Don't refactor tests you didn't need to touch.** Add new coverage as a new
  test function (or a row / mock line / tweaked assertion) alongside the
  existing test — don't reshape it into subtests, table-driven form, or a
  shared helper unless asked. Minimize the test-file diff.

## Branches & worktrees

- **Check a PR's merge state before pushing to its branch.** A merged PR is
  finished: pushing follow-up commits to its branch strands them (they land
  after the merge and never reach `main`), and editing a merged PR's body is
  misleading. Before pushing follow-up work or updating a PR, confirm it's
  still open; if it has merged, branch fresh from the latest `main` and open a
  new PR for the follow-up.
- **Branch naming:** `<user>/<ticket>-<slug>` (e.g.
  `wanyuenmei/per-49-modular-layout`).
- **Every PR starts in its own worktree — no exceptions.** Create a dedicated
  `git worktree` (e.g. under `.worktrees/<slug>`) with its own branch for every
  piece of work that becomes a PR — the foreground/main task included, not just
  background ones. Never commit PR work directly in the main checkout, and
  **never use a long-lived per-session branch.** This holds in Claude Code web
  sessions too: each task (foreground or backgrounded) gets its own worktree,
  so several can run in parallel without colliding on the working tree or
  branch.
- **Clean up on merge.** "Automatically delete head branches" is enabled, so
  merged PR branches prune themselves; also remove the matching local
  branch/worktree once its PR merges — don't leave stale worktrees around.
