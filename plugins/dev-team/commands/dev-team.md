---
description: >
  Orchestration loop for the dev-team pipeline. Drives the step machine by repeatedly
  invoking dev_team.py, parsing its JSON descriptor, and spawning the appropriate
  agent via the Agent tool. Replaces run-workflow.md.
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

You are the orchestration loop for the dev-team pipeline. You drive the step machine
by invoking `dev_team.py` repeatedly, parsing its JSON output, and spawning the
appropriate agent for each step.

**Never attempt to:**
- Fix build errors, test failures, or code review comments yourself
- Invoke agent skills directly
- Edit source files or test files
- Take any action beyond what the JSON descriptor instructs

## Steps

### 1 — Compute context file path

```bash
context_file=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/get-context-path.sh" "<work-item-id>")
mkdir -p "$(dirname "$context_file")"
```

> **Note:** On Windows this runs via Git Bash, which ships with Git-for-Windows. No
> platform-detection branch is needed.

### 2 — Orchestration loop

Repeat the following until `action == "done"` or a terminal condition is reached:

#### 2a — Run the step machine

```bash
python -u ${CLAUDE_PLUGIN_ROOT}/scripts/dev_team.py <work-item-id> \
  --workflow ${CLAUDE_PLUGIN_ROOT}/scripts/<workflow>.md \
  --research-skill <research-skill> \
  --plugin-root ${CLAUDE_PLUGIN_ROOT} \
  --context-file <context_file>
```

Capture all stdout. The last JSON array on stdout is the action descriptor list.

#### 2b — Parse the descriptor array

Display any non-JSON stdout lines as status updates to the user.

Extract the last line from stdout that is a valid JSON array (starts with `[`).

If the descriptors contain any `"message"` fields, use them to describe to the user
what work is being done before spawning the next agents.

#### 2c — Branch on action

Let `descriptors` be the parsed JSON array. The array always has at least one item.

**If `descriptors` is a single-item array and `descriptors[0].action == "done"`:**
- If `result == "success"`: report success to the user and stop.
- If `result == "failed"`: report the failure reason to the user and stop.

**If `descriptors` is a single-item array and `descriptors[0].skill == "troubleshooter"`:**

Spawn the troubleshooter agent:
```
Agent(
  subagent_type="troubleshooter",
  prompt="""
context_file: <descriptors[0].context_file>
trigger: <descriptors[0].trigger>
cycle_count: <descriptors[0].cycle_count>
"""
)
```

Handle the outcome (a JSON object with `action` field):
- `"continue"` → continue the loop (the troubleshooter has edited the context file)
- `"terminate"` → report the reason to the user and stop
- `"needs_user_input"` →
  1. Ask the user the troubleshooter's question
  2. Write the user's answer to the `troubleshooter_input` frontmatter key in the
     context file by passing the answer via stdin (avoids shell injection):
     ```bash
     python -c "
     from pathlib import Path; import re, sys
     path = Path('<context_file>')
     answer = sys.stdin.read().strip()
     text = path.read_text(encoding='utf-8')
     text = re.sub(r'troubleshooter_input:.*', lambda m: f'troubleshooter_input: {answer}', text)
     path.write_text(text, encoding='utf-8')
     " <<'ANSWER_HEREDOC'
     <user_answer>
     ANSWER_HEREDOC
     ```
  3. Continue the loop

**All other lists (multiple items, a single `spawn_agent` item, or a single `run_script` item):**

Dispatch all items in parallel — `spawn_agent` items via `Agent(subagent_type="task-runner")`,
`run_script` items via `Agent(subagent_type="script-runner")`:

```
results = await [
  Agent(subagent_type="task-runner", prompt="""
agent: <item.agent>
skill: <item.skill>
context_file: <item.context_file>
args: <item.args or "">
read_sections: <item.read_sections joined by ", ">
write_section: <item.write_section>
result_format: <item.result_format>
""")  if item.action == "spawn_agent"  else

  Agent(subagent_type="script-runner", prompt="""
command: <item.command>
log_file: <item.log_file>
result_format: <item.result_format>
""")  if item.action == "run_script"

  for item in descriptors
]
```

Log each result:
```
[<work-item-id>] <item.skill or item.command>: <result>
```

For each `run_script` item that has a `write_section` field, write the one-line result
to that section in the context file:
```bash
python -c "
from pathlib import Path; import sys
path = Path('<context_file>')
result = '<result_line>'   # e.g. 'passed' or 'failed'
section = '<item.write_section>'
sentinel = f'<!-- section:{section} -->'
text = path.read_text(encoding='utf-8')
if sentinel in text:
    import re
    text = re.sub(
        sentinel + r'.*?(?=<!-- section:|$)',
        sentinel + '\n\n' + result + '\n',
        text, flags=re.DOTALL
    )
else:
    text += f'\n{sentinel}\n\n{result}\n'
path.write_text(text, encoding='utf-8')
"
```

Then continue the loop.

> **Note:** Once a pull request exists, build and test validation is performed by
> GitHub Actions on the PR branch. The pipeline reads failing check output from
> `gh pr checks <pr_url>` rather than running validate scripts in-process.

### 3 — Error handling

If `dev_team.py` exits with a non-zero code, report the output to the user and stop.
Do not attempt recovery.
