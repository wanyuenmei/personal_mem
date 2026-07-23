<!--
PR TITLE convention (GitHub can't enforce title shape from a template, so keep this manual):

    <ticket>: <short description>

e.g.  PER-7: add tests, tool error handling, and access-log scope/timestamp

The <ticket> is the Linear issue id (PER-N). Fill in the sections below,
then delete this comment.

For a small correction that doesn't warrant its own Linear issue, use

    fix: <short description>

and delete the ## Ticket section. Every other section is still required.
-->

## Ticket
<!-- Link to the Linear issue this PR implements, e.g.
     https://linear.app/personal-context-mcp/issue/PER-7
     Delete this section entirely for a "fix: ..." PR. -->

## Description
<!-- What this PR is, in a sentence or two. -->

## What changed
<!-- The concrete changes, at a file/area level. Bullet points are fine. -->

## Why
<!-- The motivation: the problem being solved, the ticket context, the trade-offs. -->

## Testing
<!-- Tick what you actually did; add or edit items as needed. At least one box
     must be checked (use the N/A box for docs/config-only changes). -->
- [ ] `ruff check .` passes
- [ ] `pyright src` passes
- [ ] `pytest` passes (tests added/updated where relevant)
- [ ] Manually verified (describe below)
- [ ] N/A — docs/config only, nothing to test

## Deploy impact
<!-- Does this change what runs in production? Note env vars added/changed,
     schema/migration, new dependencies, or behavior behind a flag. Write
     "None" for pure code/test/docs changes with no runtime effect.
     Reminder: no deploy without an explicit go-ahead. -->
