---
description: Start a dev-team pipeline script for a resolved work item and stream its output. Called by implement and fix after argument parsing.
argument-hint: <work-item-id> <workflow> <research-skill>
user-invocable: false
---

## Arguments

$ARGUMENTS

Parse the three positional arguments from the line above:
- `work-item-id` — the resolved work item identifier (e.g. `ADR-123` or `Issue-444`)
- `workflow` — the pipeline filename stem (e.g. `implement-task-plan` or `fix-issue-plan`)
- `research-skill` — the researcher skill name (e.g. `researcher-plan` or `researcher-issue`)

## Role

You represent a development team. Your job is to start the pipeline script for the
given work item and report its progress to the user. The script is the orchestrator.
Once you have started the script you are a passive observer.

**Never attempt to:**
- Fix build errors or test failures
- Edit source files or test files
- Invoke agent skills directly (researcher-plan, researcher-issue, developer-implement, developer-fix, etc.)
- Take any action in response to failures reported in the script output

If the script exits with an error, report the final output to the user and stop. Do not
attempt recovery.

## Steps

### 1 — Check the platform

Check the platform to determine which tail command to use in step 3.

```bash
python -c "import sys; print(sys.platform)"
```

### 2 — Start the pipeline script in the background

```bash
python -u ${CLAUDE_PLUGIN_ROOT}/scripts/dev_team.py <work-item-id> --workflow ${CLAUDE_PLUGIN_ROOT}/scripts/<workflow>.md --research-skill <research-skill> --plugin-root ${CLAUDE_PLUGIN_ROOT}
```

### 3 — Stream output

**Immediately** call the Monitor tool on the background process to stream its output.
Do not wait. Do not use TaskOutput. Use the platform-appropriate tail command:
- **`win32`**: `powershell -Command "Get-Content -Wait -Path '<task-output-path>'"`
- **anything else**: `tail -f <task-output-path>`

Stream all output to the user as it arrives until the process exits.

### 4 — Report exit status

When the process exits, report its exit status to the user. Take no further action.
