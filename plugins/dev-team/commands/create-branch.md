---
description: Ensure the current work runs on a feature branch derived from a work-item ID and title/brief text.
argument-hint: <work-item-id> "<issue-title-or-task-brief-sentence>"
user-invocable: false
---

## Inputs

- Work item ID: first token in `$ARGUMENTS` (e.g. `Issue-444`, `ADR-172`)
- Slug source text: remaining argument text (quoted when it includes spaces)

If either input is missing, stop and print:

> Usage: `/create-branch <work-item-id> "<issue-title-or-task-brief-sentence>"`

## Steps

1. Check the current branch:
   ```bash
   git branch --show-current
   ```
2. If already on `dev/claude/<work-item-id>-*`, stop (nothing to do).
3. Derive a slug from the source text:
   - lowercase
   - replace spaces and underscores with hyphens
   - remove non-alphanumeric/non-hyphen characters
   - trim to 40 characters
4. Create and switch:
   ```bash
   git checkout -b dev/claude/<work-item-id>-<slug>
   ```
