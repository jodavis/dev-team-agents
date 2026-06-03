# Dev Team Portability

> **Status:** Draft
> **Will become:** `_doc_DevTeamPortability.md` once implementation is complete

## Overview

Extract the dev-team agents, skills, and orchestration scripts from the AdaptiveRemote repository into a standalone Claude Code Agent SDK plugin installable without committing any agent files to a target repository. A project-config layer — read from existing project files and environment variables — replaces hard-coded project references in agent definitions. Work-tracking and code-host integrations are exposed as swappable adapter skills so that different backends (Jira, GitHub Issues; GitHub PR) can be substituted per project. The plugin is self-updating: a `SessionStart` hook keeps the local installation current without manual update commands.

## Repository for agent work

The dev-team plugin is being developed in
[https://github.com/jodavis/dev-team-agents](https://github.com/jodavis/dev-team-agents), based on the agent implementations that exist in this repository.

All code changes are made in the `dev-team-agents` repository. Task 3 (ADR-265) adopts the plugin in AdaptiveRemote by deleting the per-repo `.claude/` files and validating locally. Tasks 4–5 run the pipeline from AdaptiveRemote to validate CloudDevTeam changes. Tasks 6–11 return to dev-team-agents for the full portability work.

## Responsibilities & Boundaries

- **Owns:** Plugin package structure and distribution; project config discovery (project files + env vars); adapter skill interface definition; initial adapter implementations (Jira work tracking, GitHub Issues work tracking, GitHub PR code hosting); auto-update mechanism
- **Does not own:** Core agent role logic (researcher, developer, reviewer, debugger stay conceptually identical); Jira MCP and GitHub MCP API internals; Claude Code Agent SDK plugin registry; GitHub Copilot cloud agent integration (see Related Epics); additional adapters for Linear, Azure DevOps, GitLab (see Related Epics)
- **Integrates with:** Claude Code Agent SDK (plugin loading and hook system); Jira MCP (`mcp__jira__*` tools) via adapter skill; GitHub MCP via adapter skill

## Key Design Decisions

### Claude Code Agent SDK plugin as delivery mechanism

_Context:_ Agent files committed to each repository cause drift: updates must be copied repo-by-repo, project-specific content is entangled with reusable logic, and bootstrapping a new project requires manually mirroring the entire `.claude/` tree.

_Decision:_ Package the dev-team as a Claude Code Agent SDK plugin hosted in a dedicated GitHub repository. The plugin directory follows the `.claude-plugin/plugin.json` structure documented in the Claude Code plugin reference. For v1, installation is a one-time manual step: clone the plugin repo to `~/.claude/plugins/dev-team/`, then enable it via whatever mechanism the installed Claude Code version supports (either a `plugins` entry in `~/.claude/settings.json` or the `/plugin install` CLI command). The exact settings format will be documented in the plugin README once confirmed against the Claude Code version in use. Once the plugin is adopted by AdaptiveRemote, the existing `.claude/agents/`, `.claude/commands/`, and `.claude/scripts/` directories in this repo are deleted.

_Consequences:_ Each consuming project's only dev-team artifact is the one-time plugin installation in the user's Claude Code environment. No project-level `.claude/` changes are needed. All agent logic updates propagate from the plugin repo via the auto-update hook. Local edits to the plugin clone take effect immediately (no build step); the auto-update pull is silently skipped if the working tree is dirty, so in-progress changes are not overwritten.

---

### Auto-update via SessionStart hook

_Context:_ The plugin must stay current without requiring explicit update commands, and without adding noticeable latency to every tool call.

_Decision:_ The plugin ships a `SessionStart` hook defined in `hooks/hooks.json`. The hook invokes `dev_team_update.py`:

- **Inputs:** `--data-dir ${CLAUDE_PLUGIN_DATA}` (writable per-plugin directory), `--threshold-hours 4`
- **Logic:** reads `${CLAUDE_PLUGIN_DATA}/last_update` (ISO timestamp); if absent or older than threshold, runs `git -C ${CLAUDE_PLUGIN_ROOT} pull --ff-only --quiet`; writes current timestamp to `last_update` on success
- **Exit behaviour:** always exits 0 — a failed pull is logged to stderr but must not block the session start

If the plugin repo is unreachable, the hook exits silently and the last installed version is used.

_Consequences:_ Requires internet access for updates. Sessions within the threshold window incur no network overhead. Stale-version risk is bounded by the threshold.

---

### Path resolution after packaging

_Context:_ `dev_team.py` currently discovers the consuming project's root by walking up from `__file__`, and finds skill files at `.claude/commands/` relative to that root. When installed as a plugin, `__file__` points to `~/.claude/plugins/cache/dev-team/scripts/` — not the consuming project — and the `.claude/commands/` tree of the consuming repo no longer contains the plugin's files.

_Decision:_ Two independent lookups, each using a different source:

- **Project root:** `dev_team.py` walks up from `os.getcwd()` instead of `__file__`. Claude Code always invokes commands from the project root, so `cwd` reliably points to the consuming project.
- **Plugin commands:** `dev_team.py` resolves command and adapter files relative to `${CLAUDE_PLUGIN_ROOT}`, an environment variable set by the Claude Code plugin system to the plugin's installation directory.

**Invocation chain:** `dev-team.md` (the user-facing slash command) instructs Claude to run the orchestrator as a Bash command. The command line uses `${CLAUDE_PLUGIN_ROOT}` as a content substitution variable (substituted by the Claude Code plugin system before execution):

```bash
python -u ${CLAUDE_PLUGIN_ROOT}/scripts/dev_team.py <work-item-id> \
  --workflow ${CLAUDE_PLUGIN_ROOT}/scripts/implement-task-plan.md \
  --research-skill researcher-plan \
  --plugin-root ${CLAUDE_PLUGIN_ROOT}
```

`dev_team.py` accepts `--plugin-root` as an explicit CLI argument so it does not rely on `CLAUDE_PLUGIN_ROOT` being present in the Python subprocess's environment (which is not guaranteed). `dev_team.py` imports `config_loader` as a Python module (both are in `scripts/`; `sys.path` is extended with `Path(__file__).parent` at startup). The log and context files are written to `REPO_ROOT / ".claude" / "dev-team" / "logs"` (a local, gitignored directory created on first run).

**Invocation mechanism:** `dev_team.py` invokes agents by reading the command `.md` file's content and passing it to a `claude` subprocess via stdin (the current `call_agent()` mechanism). This is file-content dispatch, not slash-command dispatch — so plugin namespacing (e.g. `dev-team:researcher-plan`) is irrelevant. The plugin uses the `commands/` directory format for v1; migration to `skills/` is straightforward in the future if needed.

**Adapter content concatenation:** When calling a core agent, `dev_team.py` reads the core command file and appends the selected adapter file's content (based on `DevTeamConfig.work_tracker` / `DevTeamConfig.code_host`) before passing the combined content to the subprocess. This is a Python-level join — core skill files require no changes to accommodate adapters.

**Spec file lookup for issue workflows:** `find_spec_file()` is not called for issue-based workflows (`fix-issue-plan.md`). The `spec_path` argument to `researcher-issue` is omitted or `None`; `researcher-issue` reads the issue directly from the code host and does not require a spec.

_Consequences:_ `_find_repo_root()` in `dev_team.py` changes from walking up `__file__` to walking up `os.getcwd()`. All command file path constructions change from `REPO_ROOT / ".claude" / "commands"` to `Path(os.environ["CLAUDE_PLUGIN_ROOT"]) / "commands"`. Adapter concatenation is added to `call_agent()`. Both changes are concentrated in `dev_team.py`'s initialization and dispatch code.

---

### Project config discovery: project files first, env vars as fallback

_Context:_ Agent definitions currently hard-code project names, Jira project keys, and MCP tool name prefixes. These must become dynamic without requiring a new committed file in every consuming repository.

_Decision:_ A `config_loader.py` script reads configuration in priority order:

1. `CLAUDE.md` — parsed for the project name (first `# Heading`). This is the single source of project context; `config_loader.py` reads it directly on all runtimes.
2. `CONTRIBUTING.md` — strictly additive: only fills in fields that `CLAUDE.md` did not supply. Never overrides a value already set by `CLAUDE.md`.
3. Environment variables (highest precedence, override file-derived values):
   - `DEV_TEAM_WORK_TRACKER` — `"jira"` or `"github-issues"`
   - `DEV_TEAM_CODE_HOST` — `"github"` or `"gitlab"`
   - `DEV_TEAM_PROJECT_NAME` — display name (e.g. `"AdaptiveRemote"`)
   - `DEV_TEAM_JIRA_PROJECT_KEY` — Jira issue prefix (e.g. `"ADR"`); used only by the Jira adapter
   - `DEV_TEAM_REPO` — owner/repo slug (e.g. `"jodavis/AdaptiveRemote"`); used only by GitHub adapters
   - `DEV_TEAM_MCP_PREFIX` — Jira MCP server name prefix (e.g. `"mcp__jira__"`); required when the Jira MCP server name differs from the default `mcp__jira__`; no auto-discovery in v1

The resulting `DevTeamConfig` is serialised to `$TMPDIR/dev-team-config.json` (Windows: `$TEMP\dev-team-config.json`) at pipeline start by `config_loader.py`. Its path is embedded by `dev_team.py` as a literal string in agent prompts (e.g. `Config: /tmp/dev-team-config.json`) so agents can read it without re-running discovery. Plugin `SessionStart` hooks cannot inject environment variables into the session, so `DEV_TEAM_CONFIG_PATH` is a prompt-level substitution, not a shell env var. Adapter file content (which reads from this path) is concatenated with the core skill content by `dev_team.py` before being passed to the subprocess — no in-agent Skill tool call is required.

**`config_loader.py` parsing rules and defaults:**

- `project_name`: first `# Heading` in `CLAUDE.md`; no fallback (required — pipeline errors if absent from both files and env)
- `work_tracker`: default `"github-issues"` if not set
- `code_host`: default `"github"` if not set
- `jira_project_key`: no default (empty string); required only when `work_tracker == "jira"`
- `repo`: if not set via `DEV_TEAM_REPO`, derived from `git remote get-url origin` by stripping the host and `.git` suffix (e.g. `https://github.com/jodavis/AdaptiveRemote.git` → `"jodavis/AdaptiveRemote"`)
- `mcp_prefix`: default `"mcp__jira__"` if not set
- `CONTRIBUTING.md` contributes only `project_name` (if `CLAUDE.md` did not supply it); no other fields are parsed from `CONTRIBUTING.md`

_Consequences:_ No per-project config file is committed — `CLAUDE.md` and env vars are sufficient for v1. The Jira and GitHub adapter fields are separate (`jira_project_key` vs `repo`), so both can be set simultaneously in a mixed environment.

---

### Adapter skills for work-tracking and code-hosting integrations

_Context:_ Agent definitions currently call Jira MCP and GitHub PR tools by name. Projects at new employers may use GitHub Issues, Linear, Azure DevOps, or GitLab, and the tools available will differ.

_Decision:_ Core agents never call Jira or GitHub tools directly. They invoke a named adapter skill (`work-tracking-adapter`, `code-host-adapter`) which dispatches to the correct implementation based on `DevTeamConfig`. Each adapter is a separate skill file exposing a consistent logical interface:

**Work-tracking adapter interface:**
- Get an issue (summary, description, status, acceptance criteria)
- Post a comment
- Transition an issue to a new status
- Create a child task/subtask

**Code-host adapter interface:**
- Create a PR or MR (branch, title, body, draft flag)
- Post an inline review comment
- Submit a review (approve or request changes)
- Fetch current PR status and open comments

Initial adapter implementations: `jira.md` (work tracking), `github-issues.md` (work tracking), `github-pr.md` (code hosting). The adapter selection is performed by `dev_team.py` in Python: it reads `DevTeamConfig.work_tracker` and `DevTeamConfig.code_host`, loads the corresponding adapter file from `$CLAUDE_PLUGIN_ROOT/adapters/`, and appends its content to the core skill file before passing the combined content to the Claude subprocess. Core skill files (researcher-plan.md etc.) are not modified — they contain no adapter-selection logic.

_Consequences:_ Core skill files remain backend-agnostic without any in-agent branching logic. Adding a new backend requires a new adapter file and a new `DevTeamConfig` value — no changes to core skills or to `dev_team.py` beyond the adapter dispatch table. The adapter interface is defined by convention (documented in the plugin repo), not enforced by a type system — adapters that do not implement all interface operations document which operations they omit.

---

## Planned Implementation

### Plugin repository structure

```
dev-team/                         # New standalone GitHub repository
  .claude-plugin/
    plugin.json                   # Claude Code Agent SDK plugin manifest
  agents/
    developer.md                  # Generic — no project-specific names or keys
    researcher.md
    reviewer.md
    debugger.md
  commands/
    dev-team.md                   # Entry-point command (invokes orchestrator)
    spec.md
    create-branch.md
    developer-implement.md
    developer-fix.md
    developer-create-pr.md
    researcher-plan.md
    researcher-issue.md
    researcher-validate.md
    researcher-spec-review.md
    reviewer-review.md
    reviewer-pr-review.md
    reviewer-sign-off.md
    debugger-investigate.md
    add-to-spec.md
  hooks/
    hooks.json                    # SessionStart hook: git pull with 4-hour threshold
  scripts/
    dev_team.py                   # Orchestrator (cwd-based root; CLAUDE_PLUGIN_ROOT for commands)
    config_loader.py              # NEW: discovers DevTeamConfig; writes to $TMPDIR/dev-team-config.json
    implement-task-plan.md
    fix-issue-plan.md
  adapters/
    work-tracking/
      jira.md                     # Jira MCP adapter skill
      github-issues.md            # GitHub Issues (via GitHub MCP) adapter skill
    code-host/
      github-pr.md                # GitHub PR/review adapter skill
  README.md
```

### Key classes and files

**`config_loader.py`**

```python
@dataclass
class DevTeamConfig:
    project_name: str       # required; from CLAUDE.md or CONTRIBUTING.md
    jira_project_key: str   # e.g. "ADR"; "" if not using Jira
    repo: str               # e.g. "jodavis/AdaptiveRemote"; derived from git remote if unset
    work_tracker: str       # "jira" | "github-issues"; default "github-issues"
    code_host: str          # "github" | "gitlab"; default "github"
    mcp_prefix: str         # Jira MCP prefix; default "mcp__jira__"

def load_config(project_root: Path) -> tuple[DevTeamConfig, Path]:
    ...
# Priority: CLAUDE.md → CONTRIBUTING.md (project_name only, additive) → env vars (override)
# Writes DevTeamConfig to $TMPDIR/dev-team-config.json; returns (config, config_path)
```

**`dev_team_update.py`** (invoked by the `SessionStart` hook):
```
dev_team_update.py --data-dir <path> --threshold-hours <n>

Reads <data-dir>/last_update (ISO 8601 timestamp).
If absent or older than threshold: runs git -C $CLAUDE_PLUGIN_ROOT pull --ff-only --quiet.
Writes current timestamp to <data-dir>/last_update on success.
Always exits 0; failed pulls are logged to stderr only.
```

**Adapter files** — each is a Markdown file in `adapters/work-tracking/` or `adapters/code-host/` containing:
- A brief stating which interface operations it implements and which it omits
- The specific MCP tool calls that fulfil each interface operation for this backend, using `<CONFIG_PATH>` as a placeholder for the config file path
- Error-handling instructions for unsupported operations

`dev_team.py` performs `<CONFIG_PATH>` substitution when concatenating the adapter file with the core skill: `adapter_content.replace("<CONFIG_PATH>", str(config_path))`. This uses the existing `substitutions` mechanism in `call_agent()`.

**`find_spec_file()` after packaging:** No change — it continues to search `REPO_ROOT.rglob("_spec_*.md")`, where `REPO_ROOT` now comes from `os.getcwd()`. This correctly searches the consuming project's tree, not the plugin directory. For issue-based workflows (`fix-issue-plan.md`), `find_spec_file()` is not called and `spec_path` is omitted from the `researcher-issue` invocation.

**`userConfig` (Claude Code plugin feature):** The plugin does not use the `userConfig` field in `plugin.json` for v1. Users set the required env vars manually. `userConfig`-driven prompting at enable time is a potential future improvement to simplify onboarding.

**`hooks/hooks.json`** (plugin-level — uses the nested Claude Code plugin hook schema):
```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python ${CLAUDE_PLUGIN_ROOT}/scripts/dev_team_update.py --data-dir ${CLAUDE_PLUGIN_DATA} --threshold-hours 4"
          }
        ]
      }
    ]
  }
}
```
Note: `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` are substituted by the plugin system before the command runs. Validate the exact schema against the [Claude Code plugins reference](https://code.claude.com/docs/en/plugins-reference) during implementation.

### Data flow

```
SessionStart hook fires:
  └── dev_team_update.py → git pull (if >4h since last pull)

User: /dev-team implement ADR-123
  └── dev-team.md (command, cwd = project root)
        └── config_loader.py
              │  reads CLAUDE.md → CONTRIBUTING.md → env vars
              │  writes $TMPDIR/dev-team-config.json
              └── dev_team.py (orchestrator)
                    │  REPO_ROOT   = walk up from os.getcwd()
                    │  PLUGIN_ROOT = $CLAUDE_PLUGIN_ROOT
                    ├── researcher.md → researcher-plan skill
                    │     └── adapter: jira.md
                    │           reads $DEV_TEAM_CONFIG_PATH → mcp_prefix, jira_project_key
                    │           └── mcp__jira__getJiraIssue("ADR-123")
                    ├── developer.md → developer-implement skill
                    │     └── adapter: github-pr.md
                    │           reads $DEV_TEAM_CONFIG_PATH → repo
                    │           └── mcp__github__createPr(...)
                    └── reviewer.md → reviewer-review skill
                          └── adapter: github-pr.md
                                └── mcp__github__postReviewComment(...)
```

## Related Epics

| Epic | Scope |
|------|-------|
| [ADR-220](https://jodasoft.atlassian.net/browse/ADR-220) | Core plugin package, adapter framework, Jira + GitHub adapters |
| [ADR-243](https://jodasoft.atlassian.net/browse/ADR-243) | GitHub Copilot cloud agent integration (Agents tab / @copilot — deferred pending runSubagent() API validation) |
| [ADR-244](https://jodasoft.atlassian.net/browse/ADR-244) | Additional work-tracking adapters (Linear, Azure DevOps) |
| [ADR-245](https://jodasoft.atlassian.net/browse/ADR-245) | GitLab code-host adapter (MR creation, inline review) |

## Open Questions

_(None — all questions resolved during spec review.)_

## Tasks

> **Legend:** 🧑 = must be done manually by a human · 🤖 = can be delegated to an agent

---

### ✅ Task 1: Create the plugin GitHub repository ([ADR-233](https://jodasoft.atlassian.net/browse/ADR-233))

Repository `dev-team-agents` exists at [https://github.com/jodavis/dev-team-agents](https://github.com/jodavis/dev-team-agents). No action needed.

---

### 🤖 Task 2: Verbatim pipeline copy to dev-team-agents ([ADR-264](https://jodasoft.atlassian.net/browse/ADR-264))

Copy all pipeline files from AdaptiveRemote into the dev-team-agents repository verbatim. No genericizing. No auto-update. This establishes the plugin directory structure and makes the pipeline testable in isolation before any code changes.

- [ ] Directory structure created in dev-team-agents: `agents/`, `commands/`, `scripts/`
- [ ] All `.claude/agents/*.md` and `.claude/commands/*.md` from AdaptiveRemote are present verbatim
- [ ] `dev_team.py`, `implement-task-plan.md`, `fix-issue-plan.md` present in `scripts/`
- [ ] Stub `scripts/validate-build.cmd` and `scripts/validate-tests.cmd` exist (exit 0 — dev-team-agents has no build)
- [ ] `dev_team.py` can be invoked with dev-team-agents as the cwd and exits without errors on a dry run

---

### 🤖 Task 3: Minimal plugin adoption in AdaptiveRemote ([ADR-265](https://jodasoft.atlassian.net/browse/ADR-265))

The minimum code changes to make `dev_team.py` work as a Claude Code plugin, then install it manually and cut AdaptiveRemote over to it. The only code changes required are path resolution fixes — everything else in `dev_team.py` is already project-agnostic.

- [ ] `.claude-plugin/plugin.json` manifest is present and passes Claude Code plugin validation
- [ ] `_find_repo_root()` walks up from `os.getcwd()` instead of `__file__`
- [ ] `dev_team.py` accepts `--plugin-root`; agent and command files resolve from `<plugin-root>/agents/` and `<plugin-root>/commands/`
- [ ] `dev-team.md` invokes `dev_team.py` using `${CLAUDE_PLUGIN_ROOT}` substitution and passes `--plugin-root ${CLAUDE_PLUGIN_ROOT}`
- [ ] Plugin manually installed (cloned to `~/.claude/plugins/dev-team/`) and enabled in Claude Code settings
- [ ] `AdaptiveRemote/.claude/agents/`, `.claude/commands/`, `.claude/scripts/` deleted from the AdaptiveRemote repo
- [ ] A pipeline task from ADR-191 ("Refactoring the DVC pipeline") completes end-to-end from AdaptiveRemote using the plugin

---

> **Note:** CloudDevTeam tasks 253 through 256 (see `_spec_CloudDevTeam.md`) are implemented next, entirely in dev-team-agents. Do not update AdaptiveRemote's plugin until all four are complete.

---

### 🤖 Task 4: Local pipeline validation with CloudDevTeam changes ([ADR-266](https://jodasoft.atlassian.net/browse/ADR-266))

After CloudDevTeam (253–256) is fully implemented in dev-team-agents, pull those changes into the local plugin and validate the modified pipeline end-to-end locally before attempting cloud execution.

- [ ] `git pull` in the local plugin installation picks up all CloudDevTeam changes
- [ ] A full researcher → developer → reviewer → sign-off cycle completes locally using an ADR-191 task
- [ ] Review threads appear as GitHub PR comments (not flat inline comments)
- [ ] Sub-agents do not attempt Jira MCP calls

---

### 🤖 Task 5: Cloud pipeline validation ([ADR-267](https://jodasoft.atlassian.net/browse/ADR-267))

_Depends on Task 4 (ADR-266)._

Run the pipeline in a cloud Claude Code session to confirm the ADR-246 connector centralization works in the target environment.

- [ ] A full researcher → developer → reviewer → sign-off cycle completes in a cloud Claude Code session using an ADR-191 task
- [ ] Authenticated connector calls (GitHub MCP, Jira MCP, `gh` CLI) are made only by the top-level scrum master session
- [ ] Sub-agents do not attempt Jira or GitHub connector calls

---

### 🤖 Task 6: Port and genericize agents and commands ([ADR-235](https://jodasoft.atlassian.net/browse/ADR-235))

Remove all project-specific content from the pipeline files copied in Task 2. The verbatim copy is done; this task is content-only cleanup.

> **Note for task brief:** Pipeline files were copied verbatim in ADR-264. This task genericizes their content — no file copying required. The developer agent reads files from the dev-team-agents repo and edits them in place.

- [ ] No literal `"AdaptiveRemote"`, `"ADR-"`, `mcp__jira__`, or `mcp__08e9ccd3__` remains in any agent or command file
- [ ] All agent files load in Claude Code without errors
- [ ] The `spec.md` and `dev-team.md` commands are invokable from a test project

---

### 🤖 Task 7: Implement `config_loader.py` and `DevTeamConfig` ([ADR-236](https://jodasoft.atlassian.net/browse/ADR-236))

Implement config discovery (CLAUDE.md → CONTRIBUTING.md additive → env vars → defaults) and serialise the result to a temp file.

- [ ] `project_name` is read from the first `# Heading` in `CLAUDE.md`; raises an error if absent from all sources
- [ ] `CONTRIBUTING.md` only fills in `project_name` if not already set by `CLAUDE.md`
- [ ] Env vars (`DEV_TEAM_*`) override file-derived values
- [ ] `repo` is derived from `git remote get-url origin` when `DEV_TEAM_REPO` is unset
- [ ] `work_tracker` defaults to `"github-issues"`; `code_host` defaults to `"github"`; `mcp_prefix` defaults to `"mcp__jira__"`
- [ ] Config is serialised to `$TMPDIR/dev-team-config.json`; function returns `(config, config_path)`

---

### 🤖 Task 8: Refactor `dev_team.py` for plugin packaging ([ADR-237](https://jodasoft.atlassian.net/browse/ADR-237))

Add adapter concatenation to `call_agent()` and update supporting infrastructure. Path resolution (`_find_repo_root()`, `--plugin-root`, `dev-team.md` invocation) was already done in ADR-265 and is not repeated here.

- [ ] `call_agent()` appends the correct adapter file content based on `DevTeamConfig.work_tracker` / `DevTeamConfig.code_host`
- [ ] `<CONFIG_PATH>` in adapter content is replaced with the actual config file path before passing to the subprocess
- [ ] Logs are written to `REPO_ROOT/.claude/dev-team/logs/`
- [ ] `find_spec_file()` is not called for issue-based workflows; `spec_path` is omitted from the `researcher-issue` invocation

---

### 🤖 Task 9: Work-tracking adapter skills (Jira and GitHub Issues) ([ADR-238](https://jodasoft.atlassian.net/browse/ADR-238))

Implement `adapters/work-tracking/jira.md` and `adapters/work-tracking/github-issues.md`.

- [ ] Each adapter file declares which interface operations it implements and which it omits
- [ ] Jira adapter: get issue, post comment, transition status, create child task — using `mcp_prefix` from config
- [ ] GitHub Issues adapter: get issue, post comment, close/reopen issue, create linked issue
- [ ] Given a project with `DEV_TEAM_WORK_TRACKER=jira`, when `/dev-team implement ADR-xxx` runs, then the researcher uses the Jira adapter to fetch the issue
- [ ] Given a project with `DEV_TEAM_WORK_TRACKER=github-issues`, when `/dev-team fix #nn` runs, then the researcher uses the GitHub Issues adapter

---

### 🤖 Task 10: GitHub PR code-host adapter skill ([ADR-239](https://jodasoft.atlassian.net/browse/ADR-239))

Implement `adapters/code-host/github-pr.md`.

- [ ] Adapter implements: create PR (draft flag), post inline review comment, submit review (approve / request changes), fetch PR status and open comments
- [ ] Unsupported operations are documented in the file header
- [ ] Given a project with `DEV_TEAM_CODE_HOST=github`, when the developer finishes implementation, then the developer uses the GitHub PR adapter to create a PR

---

### 🤖 Task 11: Add auto-update hook ([ADR-234](https://jodasoft.atlassian.net/browse/ADR-234))

_Deferred until cloud validation (Task 5) is confirmed. Only add the auto-update mechanism after pipeline reliability in cloud is established._

- [ ] `hooks/hooks.json` defines a `SessionStart` hook invoking `dev_team_update.py`
- [ ] `dev_team_update.py` accepts `--data-dir` and `--threshold-hours`; reads/writes the timestamp file; runs `git pull --ff-only`; always exits 0
- [ ] A session started within the threshold window skips the pull (verified via timestamp file)
- [ ] A session started after the threshold runs `git pull` and updates the timestamp

---

### 🧑 Task 12: Configure environment ([ADR-240](https://jodasoft.atlassian.net/browse/ADR-240))

Manual plugin installation is done in Task 3 (ADR-265). This task covers configuring the env vars required for the DevTeamPortability config layer, after `config_loader.py` is implemented.

- [ ] The following env vars are set in the local environment: `DEV_TEAM_WORK_TRACKER`, `DEV_TEAM_CODE_HOST`, `DEV_TEAM_JIRA_PROJECT_KEY`, `DEV_TEAM_REPO`, `DEV_TEAM_MCP_PREFIX`
- [ ] Config is verified by running `config_loader.py` and inspecting the output JSON

---

### 🤖 Task 13: End-to-end pipeline validation ([ADR-242](https://jodasoft.atlassian.net/browse/ADR-242))

Run the full pipeline with auto-update enabled and confirm it produces a working PR.

- [ ] Auto-update hook fires on session start (verified via log in `REPO_ROOT/.claude/dev-team/logs/`)
- [ ] Given the plugin is installed and env vars are configured, when `/dev-team implement <a small task>` runs, then the full researcher → developer → reviewer pipeline completes and produces a PR

---

### Epic: GitHub Copilot cloud agent integration ([ADR-243](https://jodasoft.atlassian.net/browse/ADR-243))

Design and implement the `dev_team.py` runtime adapter for GitHub's `runSubagent()` API, the `.github/agents/dev-team.agent.md` entry point, and validation of the full pipeline from the Agents tab / @copilot assignment flow. Reference: [GitHub Copilot SDK — Custom agents](https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/custom-agents).

### Epic: Additional work-tracking adapters ([ADR-244](https://jodasoft.atlassian.net/browse/ADR-244))

Implement work-tracking adapter skills for Linear and Azure DevOps following the adapter interface defined in ADR-220.

### Epic: GitLab code-host adapter ([ADR-245](https://jodasoft.atlassian.net/browse/ADR-245))

Implement a GitLab MR adapter skill (MR creation, inline review comments) following the code-host adapter interface defined in ADR-220.

## Related Docs

- [`src/_doc_Projects.md`](../src/_doc_Projects.md) — project boundaries for this codebase
- [`.claude/agents/`](.) — current per-repo agent definitions (to be replaced by this plugin)
- [`.claude/commands/`](.) — current per-repo command/skill files (to be replaced by this plugin)
- [GitHub Copilot SDK — Custom agents (subagent spawning, Python sample)](https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/custom-agents) — reference for the GitHub cloud agent epic (ADR-XXX)
