---
description: Ensure the current work runs on a dev/claude/<work-item-id> branch.
argument-hint: <work-item-id>
user-invocable: false
---

## Inputs

- Work item ID: `$ARGUMENTS` (e.g. `Issue-444`, `ADR-172`)

If missing, stop and print:

> Usage: `/create-branch <work-item-id>`

## Steps

1. Check the current branch:
   ```bash
   git branch --show-current
   ```
2. If already on `dev/claude/<work-item-id>`, stop (nothing to do).
3. Create and switch:
   ```bash
   git checkout -b dev/claude/<work-item-id>
   ```
