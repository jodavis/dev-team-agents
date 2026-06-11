---
description: Create a draft GitHub pull request for a completed work item. Determines the correct base branch, writes a developer-authored PR body, and creates the PR as a draft. Idempotent — does nothing if the PR already exists.
argument-hint: <work-item-id>
user-invocable: false
---

## Inputs

Work item ID: `$ARGUMENTS`

### Task brief

$TASK_BRIEF

---

### Work summary (all prior implementation and fix rounds)

$WORK_SUMMARIES

---

### Existing PR URL (empty = not yet created)

$PR_URL

---

## Steps

### 1 — Check if PR already exists

If `$PR_URL` is non-empty, the PR has already been created. Output the following JSON and
stop:

```json
{"pr_url": "$PR_URL"}
```

### 2 — Determine the base branch and repo coordinates

Using Bash:

1. Get the current branch name: `git branch --show-current`
2. Run `git fetch --all --quiet` to ensure remote branches are up to date.
3. List candidate base branches in priority order:
   - `main`
   - Any remote `feature/*` branches: `git branch -r | grep "feature/" | sed "s|.*origin/||"`
4. For each candidate, count how many commits HEAD is ahead of it:
   ```bash
   git rev-list --count origin/<candidate>..HEAD 2>/dev/null || echo 99999
   ```
5. Select the candidate with the fewest commits (the closest ancestor to HEAD). If two
   candidates tie, prefer `main`. If no candidate is reachable, fall back to `main`.
6. Parse owner and repo from the remote URL:
   ```bash
   git remote get-url origin
   ```
   Extract `owner` and `repo` from formats like `https://github.com/owner/repo.git`
   or `git@github.com:owner/repo.git`.

### 3 — Create the draft PR

Use `mcp__plugin_github_github__create_pull_request` with:

- `owner` and `repo` from step 2
- `head`: the current branch name from step 2
- `base`: the base branch from step 2
- `draft`: `true`
- `title`: `"<work-item-id>: <concise one-line description of what the implementation delivers>"`
- `body`: A well-structured description with these sections:
  - **Work item:** `<work-item-id>` with a one-sentence summary of what the task required
  - **Changes:** A bullet list drawn from the work summaries — one bullet per logical change
    (new file, modified interface, new test scenario, etc.)
  - **Design decisions:** Any non-obvious choices made during implementation that a reviewer
    needs context for (omit if there are none)
  - If the work item ID matches `Issue-\d+` (a GitHub issue), append a closing reference as
    the final line of the body: `Closes #<number>` (e.g. `Issue-123` → `Closes #123`). This
    links the PR to the issue under "Development" and closes the issue automatically on merge.

The PR title and body are read by human reviewers — write them clearly and precisely.

### 4 — Output

Output the PR URL as the final JSON line:

{"pr_url": "https://github.com/..."}
