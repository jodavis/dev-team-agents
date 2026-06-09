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

_Decision:_ `dev_team.py` becomes a step machine. It runs all deterministic operations
(build, test, git, state transitions) directly, then **exits** with a structured JSON
descriptor on stdout when an agent needs to run. `dev-team.md` drives a loop: invoke the
script, parse the descriptor, spawn the named agent via the `Agent` tool, then re-invoke
the script. The top-level agent's own context accumulates only the compact one-line result
returned by each agent — never the agent's full working output, which goes to the context
file.

_Consequences:_ `dev_team.py` loses its subprocess-spawn loop and monitoring logic.
`dev-team.md` gains a tight orchestration loop. Sub-agents gain full MCP access. The
ADR-246 scrum-master relay milestones are removed; agents post to Jira and GitHub
directly. The pipeline cannot run without an active Claude Code session driving it
(acceptable — the pipeline was always interactive).

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
co-located with context files for easy debugging.

## Planned Implementation

### Interfaces

#### `dev_team.py` exit descriptor (stdout JSON, exit code 0)

```json
{
  "action": "spawn_agent",
  "agent": "developer",
  "skill": "developer-implement",
  "context_file": "/Users/jodavis/.dev-team/jodavis-AdaptiveRemote/ADR-123.md",
  "read_sections": ["Researcher Brief", "Review Threads"],
  "write_section": "Implementation Summary",
  "result_format": "implemented | failed | needs_clarification"
}
```

For pipeline completion:
```json
{ "action": "done", "result": "success | failed", "reason": "..." }
```

For troubleshooter:
```json
{
  "action": "spawn_agent",
  "skill": "troubleshooter",
  "context_file": "/Users/jodavis/.dev-team/jodavis-AdaptiveRemote/ADR-123.md",
  "trigger": "signoff_deadlock",
  "cycle_count": 2
}
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

- Remove: subprocess-spawn loop, `call_agent()`, `monitor_process()`, and all `claude -p`
  invocations.
- Add: `exit_with_action(descriptor: dict) -> NoReturn` — serialises `descriptor` to JSON,
  prints to stdout, and calls `sys.exit(0)`.
- Update: every location that previously called `call_agent()` now calls
  `exit_with_action({"action": "spawn_agent", "skill": ..., ...})`.
- Update: context file path computed as
  `Path(os.environ.get("DEV_TEAM_STATE_DIR", Path.home() / ".dev-team")) / repo_slug / f"{work_item_id}.md"`.
  `repo_slug` derived from `git remote get-url origin` (slashes replaced with dashes, or
  kept as path components in a subdirectory — use subdirectory form to avoid flat
  collisions).
- Add troubleshooter trigger conditions (see Troubleshooter section below).

#### `dev-team.md`

Replace the current "start script, monitor output" pattern with an orchestration loop:

```
1. Compute context_file path (same algorithm as dev_team.py)
2. Loop:
   a. Run: python -u ${CLAUDE_PLUGIN_ROOT}/scripts/dev_team.py <work-item-id>
            --plugin-root ${CLAUDE_PLUGIN_ROOT}
            --context-file <context_file>
   b. Parse JSON from stdout (last JSON object on stdout)
   c. If action == "done": report result to user and stop
   d. If action == "spawn_agent" and skill == "troubleshooter":
        Agent(subagent_type="troubleshooter", prompt=<descriptor fields>)
        If troubleshooter returns action=="terminate": stop and report
        If troubleshooter returns action=="needs_user_input": ask user, write answer
          to context file, continue loop
        If troubleshooter returns action=="continue": continue loop
   e. If action == "spawn_agent" (any other skill):
        Agent(subagent_type="task-runner", prompt=<descriptor fields>)
        (result is one line — ignore beyond logging it)
   f. Go to step 2
```

The loop accumulates only one compact line per iteration (the task-runner's result
indicator). Context stays lean across an entire pipeline run.

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
7. Respond with **exactly one line**: the appropriate value from `result_format`.

The task-runner must not add commentary, apologies, or explanation to its response. The
single-line result is the only output the top-level agent receives.

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

`dev_team.py` tracks `signoff_cycle_count` in context file frontmatter. It increments
`signoff_cycle_count` each time `SignoffStep` returns `changes_requested` and resets it
to 0 on `approved`. The troubleshooter reads GitHub directly for thread state — no local
thread tracking is needed in the context file.

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
  │  │  stdout: {"action":"spawn_agent","skill":"researcher-plan",...}
  │  │  Agent(subagent_type="task-runner",                     │
  │  │        prompt="skill: researcher-plan ...")             │
  │  │    └─ task-runner reads context, Skill("researcher-plan")
  │  │       writes brief to context file, returns "briefed"  │
  │  └─────────────────────────────────────────────────────────┘
  │
  │  ┌─────────────────────────────────────────────────────────┐
  │  │  Loop iteration 2                                       │
  │  │  Bash: python dev_team.py ...  (script runs build/test) │
  │  │  stdout: {"action":"spawn_agent","skill":"developer-implement",...}
  │  │  Agent → task-runner → Skill("developer-implement")    │
  │  │  returns "implemented"                                  │
  │  └─────────────────────────────────────────────────────────┘
  │
  │  ┌─────────────────────────────────────────────────────────┐
  │  │  Loop iteration N (sign-off deadlock)                   │
  │  │  Bash: python dev_team.py ...                          │
  │  │  stdout: {"action":"spawn_agent","skill":"troubleshooter",...}
  │  │  Agent(subagent_type="troubleshooter", ...)            │
  │  │    └─ reads context, detects deadlock, asks user       │
  │  │       edits context (if continue), returns outcome     │
  │  │  if outcome=="continue": re-run script                 │
  │  └─────────────────────────────────────────────────────────┘
  │
  └── {"action":"done","result":"success"} → report to user
```

## Open Questions

- [ ] **`Skill` tool availability in sub-agent sessions:** The current architecture passes
  skill file content directly to `claude -p`. The task-runner uses the `Skill` tool to
  invoke skills by name, which requires the plugin's skills to be discoverable from within
  a spawned sub-agent session. This should be smoke-tested early in the task-runner
  implementation task before the full protocol is built on top of it.

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
