# Agent Orchestration Refactor

> **Status:** Draft
> **Will become:** `_doc_AgentOrchestration.md` once implementation is complete

## Overview

Refactors the dev-team pipeline from script-spawned sub-agents to a **step-machine** pattern
in which the top-level Claude Code session spawns sub-agents directly via the `Agent` tool.
Script-spawned agents (`claude -p`) cannot use authenticated MCP connectors in any
environment; agent-spawned sub-agents inherit full credentials from the top-level session.
This change gives every pipeline agent direct access to Jira and GitHub MCPs.

The refactor also introduces a **task-runner agent** that encapsulates the orchestration
protocol (read context → invoke skill → write result → return compact indicator), keeping
individual skill files pure and insulating them from future orchestration changes. A
**troubleshooter agent** handles unexpected pipeline states, with a sign-off deadlock
detector as the first concrete condition. GitHub account-picker prompts are eliminated by
routing all `gh` CLI calls through a `GH_TOKEN` personal access token. Pipeline state is
stored under `~/.dev-team/<repo-slug>/`, which is pre-approved for writes in
`~/.claude/settings.json`.

## Responsibilities & Boundaries

- **Owns:** Step-machine exit protocol (`dev_team.py`); orchestration loop (`dev-team.md`);
  task-runner agent definition and protocol; troubleshooter agent definition and sign-off
  deadlock condition; context and log file path convention (`~/.dev-team/<repo-slug>/`);
  `GH_TOKEN` authentication pattern
- **Does not own:** Individual skill logic (`researcher-plan`, `developer-implement`, etc. —
  their content is unchanged); adapter framework (ADR-220); Jira/GitHub MCP API internals;
  additional troubleshooter conditions beyond the sign-off deadlock (deferred to future work)
- **Integrates with:**
  - `dev_team.py` — gains structured exit protocol; loses subprocess-spawn loop
  - `dev-team.md` — gains orchestration loop; loses script-monitor pattern
  - `agents/task-runner.md` — new agent; invokes existing skills via `Skill` tool
  - `agents/troubleshooter.md` — new agent; reads and edits context file; may ask user
  - All existing skill files — consumed unchanged via the task-runner

## Key Design Decisions

### Step-machine: script exits when an agent is needed

_Context:_ Sub-agents spawned by the script (`claude -p`) cannot use authenticated MCP
connectors. Sub-agents spawned by the top-level Claude Code session via the `Agent` tool
inherit full credentials. The current architecture (script as sole orchestrator) therefore
cannot give sub-agents direct MCP access.

_Decision:_ `dev_team.py` becomes a step machine. It runs git and state-transition
operations directly, then **exits** with a structured JSON descriptor list on stdout when
agents need to run or scripts need to execute. `dev-team.md` drives a loop: invoke the
script, parse the descriptor list, dispatch all items in parallel via the `Agent` tool
(for agent items) or `Bash` via a `script-runner` agent (for script items), collect
results, then re-invoke the script. The top-level agent's own context accumulates only
the compact one-line results returned by each agent — never full working output, which
goes to the context file.

_Consequences:_ `dev_team.py` loses its subprocess-spawn loop and monitoring logic.
`dev-team.md` gains a tight orchestration loop. Sub-agents gain full MCP access. The
ADR-246 scrum-master relay milestones are removed; agents post to Jira and GitHub
directly. The pipeline cannot run without an active Claude Code session driving it
(acceptable — the pipeline was always interactive).

---

### Parallel dispatch: descriptor list and script-runner agent

_Context:_ Some pipeline steps are naturally parallel — the sign-off step previously ran
researcher-signoff, reviewer-signoff, and build/test validation concurrently.
Serialising these into sequential `exit_with_actions` calls requires complicated
pause-and-resume logic inside `SignOffStep` and is slower. Additionally, deterministic
script runs (build, test) previously executed inside `dev_team.py`, making them
invisible to the top-level agent and preventing any user-visible progress reporting.

_Decision:_ The exit descriptor becomes a **list**. Each item is either `spawn_agent`
(existing) or the new `run_script` type. `dev-team.md` spawns all items in the list in
parallel. For `run_script` items, it spawns a named `script-runner` agent whose sole job
is: run the command, write full output to a log file at
`~/.dev-team/<repo-slug>/logs/`, and return `passed | failed`. The top-level agent
receives a compact one-line result from each item and knows what is happening at each step.

`script-runner` is a **separate agent from `task-runner`**: `task-runner` orchestrates
skill invocations against the context file; `script-runner` executes shell commands and
captures their output to a log. Keeping them separate avoids conditional logic in each
and keeps both files small.

_Consequences:_ `SignOffStep` can express its natural parallelism in the descriptor list
without internal coordination logic. Build/test output stays out of the top-level
context (it goes to a log file). The top-level agent gains visibility into what is
running at each step and can narrate progress to the user.

---

### Task-runner agent encapsulates the orchestration protocol

_Context:_ Every pipeline agent needs to: read relevant sections from the context file,
perform its work, write its output back to the context file, and return a compact result
indicator to the top-level session. Without a dedicated wrapper, this protocol would be
duplicated across every skill file, coupling skill logic to the context file format and
making future orchestration changes expensive.

_Decision:_ A named agent `task-runner` owns the protocol. The top-level `dev-team.md`
spawns it with a short prompt containing the skill name, context file path, and section
descriptors. The task-runner reads the specified sections, presents them in its
conversation context, invokes the skill via the `Skill` tool, captures the output, writes
the result section to the context file using the `Write`/`Edit` tools, and returns the
compact result indicator as its sole response to the top-level session.

Individual skill files remain **context-file-agnostic**: they receive their inputs from
the task-runner's conversation context and return outputs as text. They require no changes
to accommodate the orchestration protocol.

_Consequences:_ Future orchestration changes require editing only `task-runner.md` and
`dev-team.md`. Skills are independently testable. The task-runner must pass the full
context file path to skills that need mid-task reads (e.g., a developer that needs to
re-read the spec during implementation); skills accept an optional `$CONTEXT_FILE`
substitution for this purpose.

---

### Troubleshooter agent for pipeline recovery

_Context:_ The existing loop counter in `dev_team.py` stops the pipeline when a threshold
is exceeded, but only reports failure. Some stuck states are recoverable — a deadlock
between the developer and reviewer can be broken with user input; a corrupted context file
can be reset — but recovering requires reasoning over the pipeline history.

_Decision:_ When a trigger condition fires, `dev_team.py` exits with
`{"action": "spawn_agent", "skill": "troubleshooter", ...}`. The top-level agent spawns
the troubleshooter via the `Agent` tool (full MCP inheritance). The troubleshooter reads
the context file, analyses the pipeline state, and returns one of three outcomes:
`continue` (it edited the context file to fix the state; re-run script), `terminate`
(unrecoverable; report to user), or `needs_user_input` (ask the user a specific question
before proceeding). The top-level agent acts on the outcome with no reasoning of its own.

**First condition (sign-off deadlock):** After a sign-off pass returns
`changes_requested`, the troubleshooter reads the PR review thread history directly from
GitHub via MCP (using the PR URL stored in the context file frontmatter). It identifies
threads where: (a) the developer has replied with a comment indicating inability to address
the issue (phrases such as "can't fix", "out of scope", "by design", "won't address") AND
(b) the reviewer's most recent review on that thread still has `changes_requested` status.
If the pattern is found, the troubleshooter surfaces the deadlocked threads to the user
via `AskUserQuestion`: *"The following threads are deadlocked: the developer cannot address
them but the reviewer still requires it. How would you like to proceed?"*
Options: override the reviewer (post a resolving comment and reset the sign-off cycle
counter in the context file), ask the developer to reconsider (post a note on the thread
prompting another look), or terminate the pipeline.

Additional conditions follow the same shape (trigger → detect pattern → ask or fix →
return outcome) and are documented as they are added.

_Consequences:_ The troubleshooter is the single point of pipeline recovery logic. It has
full MCP access, so it can read the Jira issue and GitHub PR to supplement the context
file. Its edits to the context file are the mechanism for state correction — the script
reads the corrected state on the next invocation and continues.

---

### GH_TOKEN eliminates GitHub account-picker prompts

_Context:_ The `gh` CLI uses the system keychain, which in local development contains both
the user's and Claude's GitHub accounts. Every `gh` command that requires auth triggers an
account-picker prompt, blocking unattended pipeline runs.

_Decision:_ Set `GH_TOKEN` (or `GITHUB_TOKEN`) in Claude's environment. The `gh` CLI
honours this variable unconditionally and bypasses the keychain entirely. The user
maintains a separate PAT in their own shell profile; Claude's PAT is set via
`~/.claude/settings.json` `env` or the system environment. No pipeline code changes are
needed — the env var is transparent to all `gh` calls.

_Consequences:_ Requires the operator to provision a GitHub PAT for Claude with the
appropriate scopes (confirmed separately by the operator). The user and Claude operate as
distinct GitHub identities, which is the desired outcome. PAT rotation is a manual
operator task.

---

### Context and log files at `~/.dev-team/<repo-slug>/`

_Context:_ `REPO_ROOT/.claude/` requires explicit permission grants on every new session.
`~/.claude/` is also restricted. The user experimented with `~/.dev-team/` and confirmed
it can be pre-approved via `permissions.additionalDirectories` in `~/.claude/settings.json`
with no per-write prompts.

_Decision:_ All pipeline state files live under `~/.dev-team/<repo-slug>/`:

```
~/.dev-team/
  <repo-slug>/
    <work-item-id>.md        # pipeline context file (state machine input/output)
    logs/
      <work-item-id>-<timestamp>.log   # per-run log
```

`<repo-slug>` is derived from `git remote get-url origin` by stripping the host and `.git`
suffix (e.g. `jodavis/AdaptiveRemote`). The base path is configurable via
`DEV_TEAM_STATE_DIR` env var for cross-machine sync (e.g. pointing to a OneDrive folder).

`~/.claude/settings.json` ships with `~/.dev-team` in `permissions.additionalDirectories`
as part of plugin setup instructions.

_Consequences:_ Context files persist across sessions and machines (when `DEV_TEAM_STATE_DIR`
points to a synced location). No permission prompts after initial setup. Log files are
co-located with context files for easy debugging. This path convention supersedes the
`REPO_ROOT/.claude/dev-team/logs/` path described in `_spec_DevTeamPortability.md` —
all pipeline file I/O moves to `~/.dev-team/<repo-slug>/`.

## Planned Implementation

### Interfaces

#### `dev_team.py` exit descriptor (stdout JSON, exit code 0)

The exit value is always a **JSON array** — even when it contains a single item. This keeps `dev-team.md` parsing uniform.

Single agent step:
```json
[
  {
    "action": "spawn_agent",
    "agent": "developer",
    "skill": "developer-implement",
    "context_file": "/Users/jodavis/.dev-team/jodavis-AdaptiveRemote/ADR-123.md",
    "read_sections": ["Researcher Brief", "Review Threads"],
    "write_section": "Implementation Summary",
    "result_format": "implemented | failed | needs_clarification"
  }
]
```

Parallel sign-off step (agents + script run together):
```json
[
  {
    "action": "spawn_agent",
    "agent": "task-runner",
    "skill": "researcher-signoff",
    "context_file": "/Users/jodavis/.dev-team/jodavis-AdaptiveRemote/ADR-123.md",
    "read_sections": ["Implementation Summary", "Review Threads"],
    "write_section": "Signoff Research",
    "result_format": "approved | changes_requested | failed"
  },
  {
    "action": "spawn_agent",
    "agent": "task-runner",
    "skill": "reviewer-signoff",
    "context_file": "/Users/jodavis/.dev-team/jodavis-AdaptiveRemote/ADR-123.md",
    "read_sections": ["Implementation Summary", "Review Threads"],
    "write_section": "Signoff Review",
    "result_format": "approved | changes_requested | failed"
  },
  {
    "action": "run_script",
    "command": "dotnet build && dotnet test --no-build",
    "log_file": "/Users/jodavis/.dev-team/jodavis-AdaptiveRemote/logs/ADR-123-build-20260610T142300.log",
    "result_format": "passed | failed"
  }
]
```

For pipeline completion:
```json
[{ "action": "done", "result": "success | failed", "reason": "..." }]
```

For troubleshooter:
```json
[
  {
    "action": "spawn_agent",
    "agent": "troubleshooter",
    "context_file": "/Users/jodavis/.dev-team/jodavis-AdaptiveRemote/ADR-123.md",
    "trigger": "signoff_deadlock",
    "cycle_count": 2
  }
]
```

#### Task-runner agent prompt (from `dev-team.md` to Agent tool)

```
agent: developer
skill: developer-implement
context_file: /Users/jodavis/.dev-team/jodavis-AdaptiveRemote/ADR-123.md
read_sections: Researcher Brief, Review Threads
write_section: Implementation Summary
result_format: implemented | failed | needs_clarification
```

The task-runner returns **one line**: the chosen `result_format` value.

#### Script-runner agent prompt (from `dev-team.md` to Agent tool)

```
command: dotnet build && dotnet test --no-build
log_file: /Users/jodavis/.dev-team/jodavis-AdaptiveRemote/logs/ADR-123-build-20260610T142300.log
result_format: passed | failed
```

The script-runner returns **one line**: `passed` or `failed`.

#### Troubleshooter agent prompt

```
context_file: /Users/jodavis/.dev-team/jodavis-AdaptiveRemote/ADR-123.md
trigger: signoff_deadlock
cycle_count: 2
```

The troubleshooter returns a JSON object:
```json
{
  "action": "continue | terminate | needs_user_input",
  "reason": "Human-readable explanation",
  "state_changes": ["Marked thread a1b2c3 resolved", "Reset signoff_cycle_count to 0"]
}
```

### Key Classes and Files

#### `dev_team.py`

- **Retain:** workflow-file mechanism (`--workflow`, `parse_workflow()`, `WorkflowDefinition`,
  `StateMachine`). The workflow file still drives which steps the state machine executes;
  only the agent-spawning exit mechanism changes.
- **Retain CLI args:** `--workflow`, `--research-skill`, `--plugin-root`. Add `--context-file`
  as a new required argument (path computed by `dev-team.md` and passed in; no internal
  fallback computation needed).
- **Remove:** subprocess-spawn loop, `call_agent()`, `monitor_process()`, and all
  `claude -p` invocations.
- **Add:** `exit_with_actions(descriptors: list[dict]) -> NoReturn` — serialises the list
  to JSON, prints to stdout, and calls `sys.exit(0)`. Single-item steps still pass a
  one-element list.
- **Update:** every location that previously called `call_agent()` now calls
  `exit_with_actions([{"action": "spawn_agent", "agent": ..., "skill": ..., ...}])`.
- **Update:** parallel steps (e.g. `SignOffStep`) call `exit_with_actions([...])` with
  multiple items — a mix of `spawn_agent` and `run_script` descriptors as needed.
- **Add:** troubleshooter trigger conditions (see Troubleshooter section below).
- **Add:** `consecutive_failures` counter in context file frontmatter, incremented on each
  parse error or empty agent return, reset to 0 on a successful agent return.

#### `dev-team.md`

Replace the current "start script, monitor output" pattern with an orchestration loop:

```
1. Compute context_file path:
     base = DEV_TEAM_STATE_DIR env var, or ~/.dev-team if unset
     repo_slug = strip host + .git from `git remote get-url origin`
     context_file = <base>/<repo_slug>/<work-item-id>.md
2. Loop:
   a. Run: python -u ${CLAUDE_PLUGIN_ROOT}/scripts/dev_team.py <work-item-id>
            --workflow ${CLAUDE_PLUGIN_ROOT}/scripts/<workflow>.md
            --research-skill <research-skill>
            --plugin-root ${CLAUDE_PLUGIN_ROOT}
            --context-file <context_file>
   b. Parse JSON array from stdout
   c. If the single item has action == "done": report result to user and stop
   d. If any item has action == "spawn_agent" and agent == "troubleshooter":
        (troubleshooter is always the sole item in its list)
        Agent(subagent_type="troubleshooter", prompt=<descriptor fields>)
        If outcome.action == "terminate": stop and report to user
        If outcome.action == "needs_user_input":
          Ask user the troubleshooter's question
          Write answer to `troubleshooter_input` frontmatter key in context file
          Continue loop
        If outcome.action == "continue": continue loop
   e. Otherwise: dispatch all items in the list in parallel —
        For each spawn_agent item: Agent(subagent_type="task-runner", prompt=<fields>)
        For each run_script item:  Agent(subagent_type="script-runner", prompt=<fields>)
        Collect all one-line results; log them; continue loop
   f. Go to step 2
```

The loop accumulates only compact one-line results per iteration. Context stays lean
across an entire pipeline run even when multiple agents and scripts run in parallel.

#### `agents/task-runner.md` (new)

Named agent. Tools: `Read`, `Write`, `Edit`, `Skill`, `Bash`, `Glob`, `Grep`.

Responsibilities:
1. Parse the prompt to extract `skill`, `context_file`, `read_sections`, `write_section`,
   `result_format`.
2. Read the specified `read_sections` from `context_file`.
3. Present the extracted content as context in the conversation (as a quoted block before
   invoking the skill).
4. Pass `context_file` as `$CONTEXT_FILE` substitution to the skill (for skills that need
   mid-task reads).
5. Invoke the skill: `Skill(<skill-name>)`.
6. Write the skill's output to `write_section` in `context_file` using `Edit`/`Write`.
7. Respond with **exactly one line**: the appropriate value from `result_format`. If the
   skill's output cannot be mapped to any `result_format` value, write a parse-error note
   to `<!-- section:Troubleshooter Log -->` in the context file and return `"failed"`.

The task-runner must not add commentary, apologies, or explanation to its response. The
single-line result is the only output the top-level agent receives. `write_section`
**overwrites** the entire named section — no appending.

#### `agents/script-runner.md` (new)

Named agent. Tools: `Bash`, `Write`.

Responsibilities:
1. Parse the prompt to extract `command`, `log_file`, `result_format`.
2. Run `command` via `Bash`, capturing stdout and stderr.
3. Write the full combined output to `log_file` using `Write`.
4. Respond with **exactly one line**: `passed` if the command exited 0, `failed` otherwise.
   The log file path is included: `passed — log: <log_file>` / `failed — log: <log_file>`.

The script-runner must not add commentary or explanation. Its response is one line only.

---

#### `agents/troubleshooter.md` (new)

Named agent. Tools: `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`, `AskUserQuestion`,
plus all MCP tools inherited from the top-level session.

Responsibilities:
1. Parse the prompt to extract `context_file`, `trigger`, and supporting fields.
2. Read the full context file.
3. Apply the condition handler for the given `trigger`.
4. For `signoff_deadlock`:
   - Read `pr_url` from the context file frontmatter.
   - Use GitHub MCP to fetch the PR's review threads.
   - Identify threads where: (a) a developer reply contains inability phrases ("can't fix",
     "out of scope", "by design", "won't address", etc.) AND (b) the reviewer's most recent
     review on that thread is still `changes_requested`.
   - If pattern found: use `AskUserQuestion` with three options: override reviewer / ask
     developer to reconsider / terminate.
   - Override: post a resolving comment on each flagged thread via GitHub MCP; reset
     `signoff_cycle_count` to 0 in the context file frontmatter; return
     `{"action": "continue", ...}`.
   - Reconsider: post a note on each flagged thread via GitHub MCP asking the developer
     to revisit; return `{"action": "continue", ...}`.
   - Terminate: return `{"action": "terminate", "reason": "User terminated after
     sign-off deadlock on threads: [thread URLs]"}`.
5. For unknown triggers: return `{"action": "needs_user_input", "reason":
   "Unknown trigger: <trigger>. Manual inspection required."}`.
6. Write diagnosis notes to `<!-- section:Troubleshooter Log -->` in the context file
   before returning.

#### `dev_team.py` — troubleshooter trigger conditions

| Condition | Trigger field | Threshold |
|-----------|--------------|-----------|
| Sign-off deadlock | `signoff_deadlock` | `signoff_cycle_count >= 2` (configurable) |
| Consecutive agent failures | `consecutive_failures` | 3 consecutive parse errors or empty returns |
| Unknown pipeline state | `unknown_state` | `ctx.state` not in the known state enum |
| Review loop exceeded | `review_loop` | `review_cycle_count >= N` (existing counter, now routes to troubleshooter instead of hard-stopping) |

`dev_team.py` tracks the following counters in context file frontmatter:

| Frontmatter key | Incremented when | Reset to 0 when |
|---|---|---|
| `signoff_cycle_count` | `SignoffStep` returns `changes_requested` | `SignoffStep` returns `approved` |
| `consecutive_failures` | agent return is empty or unparseable | any successful agent return |
| `review_cycle_count` | `ReviewStep` completes a review cycle | `SignoffStep` returns `approved` |

The troubleshooter reads GitHub directly for thread state — no local thread tracking is
needed in the context file. The `troubleshooter_input` frontmatter key carries user
answers written by `dev-team.md` after a `needs_user_input` outcome; the troubleshooter
reads and acts on it at the start of the next invocation, then clears it.

### Data Flow

```
User: /dev-team implement ADR-123
  │
  ▼
dev-team.md (top-level, persistent Claude Code session)
  │
  │  ┌─────────────────────────────────────────────────────────┐
  │  │  Loop iteration 1                                       │
  │  │  Bash: python dev_team.py ADR-123 ...                  │
  │  │  stdout: [{"action":"spawn_agent","skill":"researcher-plan",...}]
  │  │  Agent(subagent_type="task-runner",                     │
  │  │        prompt="skill: researcher-plan ...")             │
  │  │    └─ task-runner reads context, Skill("researcher-plan")
  │  │       writes brief to context file, returns "briefed"  │
  │  └─────────────────────────────────────────────────────────┘
  │
  │  ┌─────────────────────────────────────────────────────────┐
  │  │  Loop iteration 2                                       │
  │  │  Bash: python dev_team.py ...                          │
  │  │  stdout: [{"action":"spawn_agent","skill":"developer-implement",...}]
  │  │  Agent → task-runner → Skill("developer-implement")    │
  │  │  returns "implemented"                                  │
  │  └─────────────────────────────────────────────────────────┘
  │
  │  ┌─────────────────────────────────────────────────────────┐
  │  │  Loop iteration 3 (sign-off — parallel dispatch)        │
  │  │  Bash: python dev_team.py ...                          │
  │  │  stdout: [                                              │
  │  │    {"action":"spawn_agent","skill":"researcher-signoff"},│
  │  │    {"action":"spawn_agent","skill":"reviewer-signoff"}, │
  │  │    {"action":"run_script","command":"dotnet build && dotnet test --no-build",...}
  │  │  ]                                                      │
  │  │  ┌─ Agent → task-runner → Skill("researcher-signoff")  │
  │  │  ├─ Agent → task-runner → Skill("reviewer-signoff")    │
  │  │  └─ Agent → script-runner → Bash, writes log file      │
  │  │  (all three in parallel)                               │
  │  │  Collect: "approved", "approved", "passed — log: ..."  │
  │  └─────────────────────────────────────────────────────────┘
  │
  │  ┌─────────────────────────────────────────────────────────┐
  │  │  Loop iteration N (sign-off deadlock)                   │
  │  │  Bash: python dev_team.py ...                          │
  │  │  stdout: [{"action":"spawn_agent","agent":"troubleshooter",...}]
  │  │  Agent(subagent_type="troubleshooter", ...)            │
  │  │    └─ reads context, detects deadlock, asks user       │
  │  │       edits context (if continue), returns outcome     │
  │  │  if outcome=="continue": re-run script                 │
  │  └─────────────────────────────────────────────────────────┘
  │
  └── {"action":"done","result":"success"} → report to user
```

## Open Questions

_(None — all questions resolved during spec review.)_

## Tasks

> **Legend:** 🤖 = agent task · 🧑 = human operator task

---

### Task 1: Core step-machine — `dev_team.py`, `dev-team.md` loop, and task-runner agent (ADR-273)

All three components needed to reach a runnable state. `dev_team.py` exits with a JSON descriptor; `dev-team.md` drives the loop and spawns agents; the task-runner agent wraps the orchestration protocol. None of the three is independently runnable — they ship together.

**`dev_team.py`:**
- [ ] `--context-file` CLI argument added; context file written to `~/.dev-team/<repo-slug>/<work-item-id>.md`; log files written to `~/.dev-team/<repo-slug>/logs/`
- [ ] `DEV_TEAM_STATE_DIR` env var overrides the `~/.dev-team` base path
- [ ] `exit_with_actions(descriptors: list[dict]) -> NoReturn` added; serialises list to JSON on stdout and exits 0
- [ ] `call_agent()`, `monitor_process()`, and all `claude -p` invocations removed; every call site replaced with `exit_with_actions([{...}])`
- [ ] Parallel steps (e.g. `SignOffStep`) call `exit_with_actions([...])` with multiple items mixing `spawn_agent` and `run_script` descriptors as appropriate
- [ ] Workflow-file mechanism (`--workflow`, `parse_workflow()`, `WorkflowDefinition`, `StateMachine`) retained unchanged
- [ ] `PipelineContext` gains frontmatter fields: `signoff_cycle_count`, `consecutive_failures`, `review_cycle_count`, `troubleshooter_input`
- [ ] Troubleshooter trigger conditions added (see spec table); each routes to `exit_with_actions` with the appropriate trigger field
- [ ] Unit tests written for: `exit_with_actions()`, context path computation (with and without `DEV_TEAM_STATE_DIR`), and counter increment/reset logic for all three counters

**`dev-team.md`:**
- [ ] Context file path computed from `DEV_TEAM_STATE_DIR` / `~/.dev-team/<repo-slug>/<work-item-id>.md` before the loop starts
- [ ] Loop invokes `dev_team.py` with `--context-file`, `--workflow`, `--research-skill`, and `--plugin-root`
- [ ] Parses stdout as a JSON array
- [ ] `action == "done"` branch (single-item list): reports result to user and stops
- [ ] Troubleshooter branch (single-item list with `agent == "troubleshooter"`): spawns troubleshooter agent; handles `continue`, `terminate`, and `needs_user_input` outcomes; writes user answer to `troubleshooter_input` frontmatter key on `needs_user_input`
- [ ] All other lists: dispatches all items in parallel — `spawn_agent` items via `Agent(subagent_type="task-runner")`, `run_script` items via `Agent(subagent_type="script-runner")`; collects and logs all one-line results
- [ ] `run-workflow.md` retired; `implement.md` and `fix.md` updated to invoke the new loop directly

**Task-runner agent (`agents/task-runner.md`):**
- [ ] Agent defined with tools: `Read`, `Write`, `Edit`, `Skill`, `Bash`, `Glob`, `Grep`
- [ ] Parses prompt fields: `agent`, `skill`, `context_file`, `read_sections`, `write_section`, `result_format`
- [ ] Reads `read_sections` from context file and presents them as quoted context before invoking the skill
- [ ] Passes `context_file` as `$CONTEXT_FILE` to the skill; invokes via `Skill(<skill-name>)`
- [ ] Overwrites `write_section` in context file with skill output
- [ ] Returns exactly one line matching a `result_format` value; returns `"failed"` and writes parse-error note to `<!-- section:Troubleshooter Log -->` if output cannot be mapped
- [ ] Given a pipeline researcher step, when the task-runner is spawned with `skill: researcher-plan`, then the researcher brief is written to the context file and `"briefed"` is returned

**Script-runner agent (`agents/script-runner.md`):**
- [ ] Agent defined with tools: `Bash`, `Write`
- [ ] Parses prompt fields: `command`, `log_file`, `result_format`
- [ ] Runs `command` via `Bash`, capturing stdout and stderr
- [ ] Writes full combined output to `log_file` via `Write`
- [ ] Returns exactly one line: `passed — log: <log_file>` or `failed — log: <log_file>`
- [ ] Given `dotnet build` exits 0, when script-runner runs, then it returns `passed — log: <path>` and the log file contains full build output

---

### Task 2: Step protocol refactor — `get_actions` / `handle_results` / `ParallelSteps` 🤖

_Depends on Task 1. Refactor only — no behaviour changes visible to the pipeline._

Introduce a clean two-method `Step` protocol that replaces the current `execute()`/`exit_with_actions()` pattern. Every step expresses what it wants to do (`get_actions`) and how it interprets the outcome (`handle_results`). Parallelism is expressed through a generic `ParallelSteps` composite rather than bespoke coordination logic inside individual steps.

**`Step` base class:**
- [ ] `get_actions(self) -> list[dict]` — returns a list of action descriptors (`spawn_agent` or `run_script` items); replaces direct calls to `exit_with_actions()`
- [ ] `handle_results(self, results: list[str]) -> str` — accepts the ordered list of one-line result strings (one per action from the previous `get_actions` call) and returns a branch moniker (e.g., `"approved"`, `"changes_requested"`, `"implemented"`)
- [ ] Steps no longer call `exit_with_actions()` directly; the state machine owns the exit

**State machine loop in `dev_team.py`:**
- [ ] Script invoked **with** `--results <comma-separated>`: parse results → `current_step.handle_results(results)` → get branch moniker → transition to next step → `next_step.get_actions()` → `exit_with_actions(actions)`
- [ ] Script invoked **without** `--results` (recovery / initial run): `current_step.get_actions()` → `exit_with_actions(actions)`
- [ ] If `get_actions()` returns `[]` (inline step): immediately call `handle_results([])` → transition → call `get_actions()` on next step → continue; guard against infinite inline loop (raise if step pointer does not advance)
- [ ] `dev-team.md` updated to collect all one-line results from parallel agents/scripts and pass them as `--results "r1,r2,r3"` on the next script invocation

**`ParallelSteps` composite:**
- [ ] `__init__(self, steps: list[Step])` — holds ordered list of child steps
- [ ] `get_actions()` — calls `get_actions()` on each child and returns the concatenated flat list; records how many actions each child contributed (for result distribution)
- [ ] `handle_results(results)` — splits the flat result list by child action counts; calls `handle_results` on each child; passes all child branch monikers to `combine_results()`
- [ ] `combine_results(self, child_monikers: list[str]) -> str` — default implementation: `"failed"` if any child returned `"failed"`; `"changes_requested"` if any returned `"changes_requested"`; otherwise the first moniker (assumes homogeneous success values). Subclasses may override.

**Refactor `SignOffStep`:**
- [ ] Decompose into `ReviewerSignOffStep`, `ResearcherSignOffStep`, and `BuildValidationStep` — each a simple single-action step
- [ ] `SignOffStep` becomes a `ParallelSteps` wrapping these three (or the applicable subset per workflow), with `combine_results` using the precedence rule above
- [ ] All sign-off-specific parallel coordination logic removed from the old `SignOffStep`

**Tests:**
- [ ] Unit tests for `ParallelSteps.get_actions()` — flat list is correct concatenation of children
- [ ] Unit tests for `ParallelSteps.handle_results()` — results distributed correctly by action count; `combine_results` precedence verified
- [ ] Unit tests for inline step (empty `get_actions`) — state machine advances without exiting; infinite-loop guard fires on non-advancing step
- [ ] Unit tests for `--results` parsing and absence (recovery path)
- [ ] Given an existing workflow, when the refactored state machine runs end-to-end in tests, then it produces the same sequence of exits as the pre-refactor code

---

### Task 3: Environment setup 🧑 (ADR-274)

One-time operator configuration required before the pipeline can run. No code changes.

- [ ] `GH_TOKEN` set to Claude's GitHub PAT in `~/.claude/settings.json` `env` section (scopes confirmed separately)
- [ ] `~/.dev-team` added to `permissions.additionalDirectories` in `~/.claude/settings.json` (eliminates per-write permission prompts)
- [ ] Plugin installation confirmed current (latest changes pulled from `dev-team-agents` repo)
- [ ] Given `GH_TOKEN` is set to Claude's PAT, when the developer agent runs `gh pr create`, then no account-picker prompt appears

---

### Task 4: End-to-end pipeline validation 🤖 (ADR-275)

_Depends on Tasks 1, 2, and 3._

Run a full implement pipeline cycle to confirm the step-machine architecture works end-to-end.

- [ ] Given the plugin is installed with `GH_TOKEN` configured and `~/.dev-team` pre-approved, when `/dev-team implement <task>` runs, then `dev-team.md` loops correctly and spawns task-runner agents for each step
- [ ] Given a full researcher → developer → reviewer → sign-off cycle, when it completes, then the context file at `~/.dev-team/<repo-slug>/<work-item-id>.md` contains all expected sections and no `claude -p` processes are spawned
- [ ] Given `GH_TOKEN` is set, when the developer agent creates a PR, then no account-picker prompt appears
- [ ] Sub-agents (researcher, developer, reviewer) successfully make Jira MCP and GitHub MCP calls directly without top-level relay

---

### Task 5: Troubleshooter agent 🤖 (ADR-276)

_Can run after Task 3 or in parallel. The pipeline is fully functional without it._

Implement `agents/troubleshooter.md` with the sign-off deadlock condition as the first concrete condition and blueprint for future additions.

- [ ] Agent defined with tools: `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`, `AskUserQuestion`, plus inherited MCP tools
- [ ] Parses prompt fields: `context_file`, `trigger`, and supporting fields
- [ ] `signoff_deadlock` handler: reads `pr_url` from context file frontmatter; fetches PR review threads via GitHub MCP; identifies threads with developer inability replies that are still `changes_requested`; presents three options to user via `AskUserQuestion`
- [ ] Override path: posts resolving comment on each flagged thread via GitHub MCP; resets `signoff_cycle_count` to 0; returns `{"action": "continue", ...}`
- [ ] Reconsider path: posts a note on each flagged thread asking developer to revisit; returns `{"action": "continue", ...}`
- [ ] Terminate path: returns `{"action": "terminate", "reason": "..."}` with thread URLs
- [ ] Unknown trigger fallback: returns `{"action": "needs_user_input", "reason": "Unknown trigger: <trigger>. Manual inspection required."}`
- [ ] Writes diagnosis to `<!-- section:Troubleshooter Log -->` before returning in all cases
- [ ] Given the pipeline has reached `signoff_cycle_count == 2` with a deadlocked thread, when the troubleshooter runs, then it asks the user how to proceed and acts on the answer without re-running the sign-off

## Related Epics

| Epic | Scope |
|------|-------|
| [ADR-269](https://jodasoft.atlassian.net/browse/ADR-269) | This epic — agent orchestration refactor |
| [ADR-220](https://jodasoft.atlassian.net/browse/ADR-220) | Dev-team plugin core package and adapter framework (portability) |

## Related Docs

- [`_spec_DevTeamPortability.md`](_spec_DevTeamPortability.md) — ADR-264/265; plugin
  packaging, path resolution, and config loader; prerequisites for this spec
- [`plugins/dev-team/commands/run-workflow.md`](plugins/dev-team/commands/run-workflow.md)
  — current orchestration entry point, replaced by the loop in `dev-team.md`
- [`plugins/dev-team/agents/developer.md`](plugins/dev-team/agents/developer.md) — current
  developer agent definition
