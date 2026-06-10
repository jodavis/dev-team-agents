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

Capture all stdout. The last JSON object on stdout is the action descriptor.

#### 2b — Parse the descriptor

Display any non-JSON stdout lines as status updates to the user.

Extract the last line from stdout that is a valid JSON object.

If the descriptor contains a `"message"` field, display it to the user before
spawning the next agent.

#### 2c — Branch on action

**If `action == "done"`:**
- If `result == "success"`: report success to the user and stop.
- If `result == "failed"`: report the failure reason to the user and stop.

**If `action == "spawn_agent"` and `skill == "troubleshooter"`:**

Spawn the troubleshooter agent:
```
Agent(
  subagent_type="troubleshooter",
  prompt="""
context_file: <descriptor.context_file>
trigger: <descriptor.trigger>
cycle_count: <descriptor.cycle_count>
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

**If `action == "spawn_agent"` and descriptor contains an `"actions"` list:**

Spawn all agents in parallel using multiple `Agent(...)` calls and await all results:

```
results = await [
  Agent(subagent_type="task-runner", prompt="""
agent: <action.agent>
skill: <action.skill>
context_file: <action.context_file>
args: <action.args or "">
read_sections: <action.read_sections joined by ", ">
write_section: <action.write_section>
result_format: <action.result_format>
""")
  for action in descriptor["actions"]
]
```

Log each result:
```
[<work-item-id>] <action.skill>: <result>
```

Then continue the loop.

**If `action == "spawn_agent"` (any other skill):**

Spawn the task-runner agent:
```
Agent(
  subagent_type="task-runner",
  prompt="""
agent: <descriptor.agent>
skill: <descriptor.skill>
context_file: <descriptor.context_file>
args: <descriptor.args or "">
read_sections: <descriptor.read_sections joined by ", ">
write_section: <descriptor.write_section>
result_format: <descriptor.result_format>
"""
)
```

The task-runner returns exactly one line (the result indicator). Log it:
```
[<work-item-id>] <skill>: <result>
```

Then continue the loop.

> **Note:** Once a pull request exists, build and test validation is performed by
> GitHub Actions on the PR branch. The pipeline reads failing check output from
> `gh pr checks <pr_url>` rather than running validate scripts in-process.

### 3 — Error handling

If `dev_team.py` exits with a non-zero code, report the output to the user and stop.
Do not attempt recovery.
