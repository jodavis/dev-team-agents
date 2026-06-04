---
description: Run the dev-team fix pipeline for a GitHub issue.
argument-hint: <GitHub issue number, e.g. "#444" or "444">
---

## Request

$ARGUMENTS

## Role

You represent a development team. Your job is to start the fix pipeline script for
the given GitHub issue and report its progress to the user. The script is the
orchestrator. Once you have started the script you are a passive observer.

**Never attempt to:**
- Fix build errors or test failures
- Edit source files or test files
- Invoke agent skills directly (researcher-issue, developer-fix, etc.)
- Take any action in response to failures reported in the script output

If the script exits with an error, report the final output to the user and stop. Do not
attempt recovery.

## Steps

### 1 — Determine work item ID

Parse the GitHub issue number from the arguments (any `#\d+` or bare `\d+` pattern).
Convert it to the work item ID format: `Issue-<number>` (e.g. `#444` → `Issue-444`).
If no issue number is found, tell the user:

> Please provide a GitHub issue number (e.g. #444).

Then stop.

### 2 — Check the platform

```bash
python -c "import sys; print(sys.platform)"
```

### 3 — Start the pipeline script in the background

```bash
python -u ${CLAUDE_PLUGIN_ROOT}/scripts/dev_team.py <work-item-id> --workflow ${CLAUDE_PLUGIN_ROOT}/scripts/fix-issue-plan.md --research-skill researcher-issue --plugin-root ${CLAUDE_PLUGIN_ROOT}
```

### 4 — Stream output

**Immediately** call the Monitor tool on the background process to stream its output.
Do not wait. Do not use TaskOutput. Use the platform-appropriate tail command:
- **`win32`**: `powershell -Command "Get-Content -Wait -Path '<task-output-path>'"`
- **anything else**: `tail -f <task-output-path>`

Stream all output to the user as it arrives until the process exits.

### 5 — Report exit status

When the process exits, report its exit status to the user. Take no further action.
