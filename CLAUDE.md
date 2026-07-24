# personal_mem — conventions for Claude

## Naming & references

- **No milestone references.** Don't cite internal roadmap milestones (`M2.2`, `M4`) anywhere — code, docstrings, PR titles, PR bodies. They age badly and mean nothing to a future reader. Name the thing ("the OAuth layer", "the consent layer") and use Linear ids (`PER-N`) as pointers.
- **Describe by intent, not by opaque label.** Say what a value means or what code does. Lookupable references are fine (Linear ids, incidents, dates); tracking artifacts (sheet-row ids, Figma frame names) are not — fold their meaning into prose.
- **Don't link Linear documents from repo files.** The repo stays self-contained; planning and strategy live in Linear. PR *descriptions* still link the ticket — that's traceability, not a doc link.

## Prose & markdown

- **Don't hard-wrap. One line per paragraph.** Required for anything posted to GitHub — PR bodies, issues, review comments — where GitHub Flavored Markdown turns a single newline into a `<br>`, so wrapped text renders as narrow ragged lines. Repo `.md` renders the same either way, but stays unwrapped for one consistent habit. Tables, fenced code, and list items keep their own line structure.
- **Don't reflow existing files to convert them.** That rewrites `git blame` for every prose line and buries the real change. Files convert as their paragraphs are edited anyway.

## Pull requests

Follow `.github/pull_request_template.md` — it's the source of truth for the required sections. CI enforces the title format (`.github/workflows/pr-title.yml`) and the body (`.github/scripts/check_pr_body.py`).

- **Title:** `<ticket>: <short description>`, e.g. `PER-7: add tests and tool error handling`. A correction too small to warrant its own Linear issue may use `fix: <short description>` and omit the **Ticket** section. Reach for a ticket by default — `fix:` is the exception, not a way to avoid writing one.
- **Testing** needs at least one box ticked; use the N/A box for docs- or config-only changes.
- **Deploy impact** lists env/schema/dependency/flag changes, or "None". No deploy happens without an explicit go-ahead.
- **Keep it succinct.** Say enough to review the diff. Background, rationale, and history belong in the Linear ticket — link it rather than restating it.
- **Update the description as part of any push that changes the diff** — amend, force-push, follow-up commit. Not as a later step. A body describing a prior approach misleads human reviewers and reads to automated ones as a discrepancy between what the PR claims and what it does.
- **Before/after table when values change.** For changed copy, config, thresholds, enum mappings, or renamed identifiers: a small table (context | before | after, plus notes when a caveat matters), each row named by what it *is or does*, so the change reads at a glance.
- **Pre-PR review pass.** Before pushing for review, re-read the whole diff: confirm it's clean (no debug leftovers, dead code, stray changes) and consider whether it simplifies (dedupe, drop needless abstraction, tighten control flow). Report what you find — don't push straight from "tests green".

## Commits

- **One commit per PR while it has no human review.** Fold extra commits in with `--amend` or a rebase, and rebase onto latest `main` so the diff reflects current `main`. Merges squash anyway; this keeps the pre-review history clean.
- **Stop rewriting history once a human reviews or comments.** Force-pushing breaks their comment anchors — add follow-up commits or merge `main` in instead. CI and bot activity don't count; check the PR's actual reviews — and its `state` — before amending.

## Tests

- **Don't refactor tests you didn't need to touch.** Add coverage as a new test function, row, or assertion alongside the existing one — don't reshape it into subtests, table-driven form, or a shared helper unless asked. Minimize the test-file diff.

## Branches & worktrees

- **Every PR starts in its own worktree — no exceptions.** A dedicated `git worktree` under `.worktrees/<slug>` on its own branch, for every piece of work that becomes a PR, foreground included. Never commit PR work in the main checkout, and never use a long-lived per-session branch. This holds in Claude Code web sessions too, so parallel tasks don't collide.
- **Branch naming:** `<user>/<ticket>-<slug>`, e.g. `wanyuenmei/per-49-modular-layout`.
- **Re-check `state` immediately before every push, amend, or force-push.** Not reviews — `state`. `gh pr view <n> --json state,mergedAt`. A PR can merge while you're mid-task, and a merged PR is finished: anything pushed to its branch afterwards lands after the merge and never reaches `main`, silently. Checking once at the start of the work is not enough. If it has merged, branch fresh from latest `main` and open a new PR.
- **Clean up on merge.** Head branches auto-delete; remove the matching local branch and worktree too.
