Summary: Top-level repository layout and the purpose of each directory and file.

# Project Layout

This repository is a **Claude Code plugin package** — a collection of plugins that each
encapsulate an orchestrated set of agents and skills. The package is published to the
Claude Code marketplace under the name `jodavis-agent-plugins`.

## Repository root

| Path | Purpose |
|------|---------|
| `.claude-plugin/marketplace.json` | Package manifest: declares the package name, owner, and the list of plugins it contains |
| `.github/workflows/` | CI pipeline (build and test) |
| `_doc_*.md` | Architecture and design documentation (this file and any future additions) |
| `_spec_*.md` | In-progress feature specs; become `_doc_*.md` files once implementation is complete |
| `CONTRIBUTING.md` | Contribution workflow, coding standards, and testing conventions |
| `plugins/` | One subdirectory per plugin |

## `plugins/<plugin-name>/`

Each plugin is a self-contained Claude Code plugin. Currently there is one plugin:

### `plugins/dev-team/`

A simulated development team pipeline: Researcher → Developer → Reviewer → Debugger.

| Path | Purpose |
|------|---------|
| `.claude-plugin/plugin.json` | Plugin manifest: name, version, description, and commands directory |
| `agents/` | Agent definition files (`*.md`) — role prompts for each pipeline agent (debugger, developer, researcher, reviewer) |
| `commands/` | Skill/command definition files (`*.md`) — one per slash command exposed to the user |
| `scripts/dev_team.py` | Pipeline orchestrator — accepts a work-item ID and drives the researcher → developer → reviewer loop |
| `scripts/fix-issue-plan.md` | Workflow definition for the fix-issue pipeline |
| `scripts/implement-task-plan.md` | Workflow definition for the implement-task pipeline |
| `scripts/validate*.sh / *.cmd` | CI helper scripts: `validate.sh` runs build + tests; `validate-build.*` and `validate-tests.*` are called individually |

## Adding a new plugin

1. Create `plugins/<plugin-name>/` with its own `.claude-plugin/plugin.json`.
2. Register it in `.claude-plugin/marketplace.json` under `"plugins"`.
3. Add a `_doc_<PluginName>.md` at the repo root describing the plugin's architecture.
