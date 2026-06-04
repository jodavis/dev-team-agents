---
description: Run the dev-team implementation pipeline for a Jira task.
argument-hint: <Jira task key, e.g. "ADR-123">
---

## Request

$ARGUMENTS

## Steps

### 1 — Determine work item ID

Parse the Jira task key from the arguments (any `[A-Z]+-\d+` pattern). If no valid
Jira key is found, tell the user:

> Please provide a Jira task key (e.g. ADR-123).

Then stop.

### 2 — Run the workflow

Invoke the `run-workflow` skill with arguments:
`<work-item-id> implement-task-plan researcher-plan`
