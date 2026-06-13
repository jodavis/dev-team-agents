---
description: Find the epic base branch and create the task branch for a pipeline run.
argument-hint: <work-item-id> <spec-path>
user-invocable: false
---

## Inputs

Work item ID: first token of `$ARGUMENTS`
Spec path: second token of `$ARGUMENTS`

## Steps

### 1 — Check if already set up

```bash
git branch --show-current
```

If already on `dev/claude/<work-item-id>`, output `setup_done` and stop.

### 2 — Find the epic ID

Read the spec file at the spec path. Look for an Epic reference — a Jira key pattern
(e.g. `ADR-200`) that appears in headings like `Epic:`, `Parent:`, or in the title block.

If not found in the spec, query Jira:
- Call `mcp__08e9ccd3-4093-4425-adec-d98ea766a759__getJiraIssue` with the work item ID
- Look for a `parent` or `epic` field on the issue

If no epic is found, proceed with `main` as the base branch.

### 3 — Find the base branch

If an epic ID was found (e.g. `ADR-200`):

```bash
git fetch origin
git branch -r | grep "feature/<epic-id>"
```

Use the first matching branch (e.g. `origin/feature/ADR-200-infrastructure`), stripping
the `origin/` prefix. If no match is found, fall back to `main`.

If no epic was found, check for the nearest `feature/*` ancestor of the current HEAD:

```bash
git branch -r --merged HEAD | grep "feature/"
```

Use the closest ancestor `feature/*` branch. If none, use `main`.

### 4 — Check out base branch and pull

```bash
git checkout <base-branch>
git pull origin <base-branch>
```

### 5 — Create task branch

```bash
git checkout -b dev/claude/<work-item-id>
```

### 6 — Output

Output exactly: `setup_done`
