# Cloud Dev Team

> **Status:** Draft
> **Will become:** `_doc_CloudDevTeam.md` once implementation is complete

## Overview

ADR-246 refactors the dev-team pipeline so that all authenticated connector calls (GitHub
MCP, Jira MCP, `gh` CLI) are exclusively handled by the top-level scrum master session.
Sub-agents (Developer, Reviewer) lose connector access and communicate review findings
through structured JSON in the pipeline context file. The scrum master mirrors that JSON
into real GitHub PR review threads, acting as the bridge between the isolated sub-agents
and external systems.

The motivation is that sub-agents spawned via `claude -p` cannot use authenticated MCP
connectors in cloud environments; centralizing those calls in the top-level session
eliminates that constraint.

## Repository for agent work

These modifications will be made in
[https://github.com/jodavis/dev-team-agents](https://github.com/jodavis/dev-team-agents), to keep them isolated from the working pipeline until they are operational. That plug-in is being developed based on the development pipeline in this repository.

Code changes, pull requests, reviews, and validation should all be done in the `dev-team-agents` repository, until Task 9 (ADR-241) which adopts the new plug-in in this repository.

## Responsibilities & Boundaries

- **Owns:** structured comment thread schema; scrum master milestone table; agent tool
  lists; thread-ID sidecar file; `dev_team.py` context serialization for the new
  `review_threads` field
- **Does not own:** the review logic itself (what constitutes a blocking issue); the
  pipeline workflow state machine; validate scripts
- **Integrates with:**
  - `dev_team.py` — context file format, `ReviewStep`, `FixPrStep`, `SignoffStep`,
    `ValidateStep`, `ImplementStep`, `DebugStep`, `FixStep`, `main()`
  - `dev-team.md` — scrum master milestone table, sidecar persistence
  - `reviewer-review.md`, `reviewer-sign-off.md` — new output schema
  - `developer-fix.md` — new `$REVIEW_THREADS` substitution replacing GitHub MCP fetch

## Key Design Decisions

### Comment threads replace flat inline comments

_Context:_ The current reviewer output has a flat `comments` array (`path`, `line`,
`body`) that the scrum master posts as single-comment threads. The Developer never
contributes back to those threads. With connector access removed the pipeline needs a
round-trip channel.

_Decision:_ Replace the flat `comments` array with a `threads` array using this schema:

```json
[
  {
    "id": "a1b2c3",
    "filePath": "relative/path/File.cs",
    "lineNumber": 42,
    "resolved": false,
    "comments": [
      { "author": "Reviewer", "comment": "This method needs a CancellationToken." },
      { "author": "Developer", "comment": "Added CancellationToken in latest commit." }
    ]
  }
]
```

Each thread has a stable `id` assigned by `dev_team.py` after parsing the Reviewer's
output (8-char hex UUID). The Reviewer never generates IDs. Only the Reviewer may set
`resolved: true`; the Developer appends reply comments only.

_Consequences:_ `reviewer-review.md` and `reviewer-sign-off.md` must output this schema.
`developer-fix.md` receives unresolved threads via `$REVIEW_THREADS`. `dev_team.py` must
persist the thread array in the context file and supply it to both agents. The scrum
master translates threads to GitHub PR review comments and replies.

### Scrum master owns all GitHub and Jira operations

_Context:_ Sub-agents use the `gh` CLI, GitHub MCP, and Jira MCP — none of which work
in cloud environments where sub-agents lack authenticated connector access.

_Decision:_ Remove all Jira MCP tools from `developer.md`. All other agents retain `Bash`
access (git, dotnet, etc.). The scrum master's milestone table is extended to handle
thread creation, replies, resolution, and all Jira state transitions via MCP connectors.

_Consequences:_ Any Jira operations the Developer was performing must move to scrum master
milestone actions or be dropped.

### Thread IDs persisted in a sidecar file

_Context:_ To reply to an existing GitHub thread, the scrum master needs the GitHub
comment ID for that thread. The context file is owned by `dev_team.py` (scrum master must
not write to it). Session memory breaks on resume.

_Decision:_ The scrum master writes a JSON sidecar file at
`.claude/logs/dev-team/<work-item-id>-threads.json`. Structure:

```json
{
  "pr_url": "https://github.com/owner/repo/pull/42",
  "threads": [
    {
      "id": "a1b2c3",
      "githubCommentId": 123456789,
      "githubThreadId": 987654321
    }
  ]
}
```

`pr_url` is written when the PR is created and read on every subsequent milestone.
`githubCommentId` is used for replies; `githubThreadId` is used for `resolve_thread`.
Both are needed because they map to different GitHub API operations.

On each "Review ready" milestone the scrum master loads this file, matches threads by
`id`, and decides: reply to existing GitHub thread vs. create a new review comment. After
posting, it writes the entire updated sidecar atomically. If the sidecar already contains
`pr_url`, PR creation is skipped.

_Consequences:_ The sidecar is the scrum master's own persistence — writable by the
top-level session, separate from the pipeline context file.

### Sign-off also uses the thread schema

_Context:_ Sign-off should produce inline threads, not just a plain body, so Developer
replies flow back to GitHub.

_Decision:_ `reviewer-sign-off.md` outputs `{"body": "...", "threads": [...]}` — the same
thread schema, no `status` field. `dev_team.py` determines the outcome mechanically: if
any merged thread has `resolved: false` → `changes_requested`; otherwise `approved`.

For sign-off, the Reviewer receives the full `$REVIEW_THREADS` array (including resolved
threads) and must return every thread it evaluated — updated `resolved` flags, plus any
new comments. New threads may be introduced. For threads with no new comment, return the
thread with `comments: []` so the merge can update `resolved` without creating duplicates.

_Consequences:_ `SignoffStep` merges sign-off threads into `ctx.review_threads` (update
existing by `id`, append new), then emits `[DEV-TEAM] Signoff: approved/changes_requested`.

### Scrum master context window management

_Decision:_ Rely on Claude Code's built-in auto-compaction. Token usage optimisation is
deferred to a separate work item.

## Planned Implementation

### Interfaces

#### Review thread schema (shared by Reviewer and Developer)

Two new dataclasses in `dev_team.py`:

```python
@dataclass
class ReviewComment:
    author: str    # "Reviewer" or "Developer"
    comment: str

@dataclass
class ReviewThread:
    id: str                              # 8-char UUID assigned by dev_team.py
    file_path: str                       # repo-relative path
    line_number: int                     # 1-based
    resolved: bool                       # set only by the Reviewer
    comments: list[ReviewComment] = field(default_factory=list)
```

Serialization to/from camelCase JSON wire format (`filePath`, `lineNumber`) is handled by
`save()`/`load()` on `PipelineContext`. `review_threads` is stored in
`<!-- section:Review Threads -->` (not frontmatter). `first_push_done` is stored in
frontmatter as lowercase `true`/`false`.

#### Updated reviewer output JSON

```json
{
  "body": "Overall review summary prose.",
  "threads": [ "<thread objects — no id field>" ],
  "status": "approved | changes_requested"
}
```

The `comments` key is removed; `threads` replaces it. Thread objects must not include an
`id` field — `dev_team.py` assigns IDs after parsing.

#### Updated scrum master milestone table

| Milestone | Action |
|-----------|--------|
| `[DEV-TEAM] Implementation started` | **Jira:** assign work item to self (`atlassianUserInfo`), transition to "In Progress". **Issue:** assign GitHub issue to self. |
| `[DEV-TEAM] Debug complete` | **Issue pipeline only** (never fires for Jira pipelines — `debugging` state is in `fix-issue-plan` only). Read `<!-- section:Debug Report -->`. Post root cause as GitHub issue comment. |
| `[DEV-TEAM] Implementation complete` | Read `<!-- section:Implementation Summary -->`. **Jira:** post as Jira comment. **Issue:** post as GitHub issue comment. |
| `[DEV-TEAM] First push complete` | Read `<!-- section:Researcher Brief -->` and `<!-- section:Implementation Summary -->`. Draft PR title/body. Read `base_branch` from context file frontmatter. Create draft PR. Write sidecar `{"pr_url": "...", "threads": []}`. **Jira:** transition to "In Review". |
| `[DEV-TEAM] Review ready: changes_requested` | Read `<!-- section:Review Threads -->` and sidecar. For each thread: `id` in sidecar → reply to existing comment; else create new inline review comment. Submit `REQUEST_CHANGES`. Write updated sidecar. |
| `[DEV-TEAM] Review ready: approved` | Same thread-posting logic. Submit `APPROVE`. |
| `[DEV-TEAM] Signoff: approved` | Post thread updates (replies or new). Resolve threads with `resolved: true`. Submit `APPROVE`. Request review from pipeline operator (best-effort). Assign work item/issue to pipeline operator. Mark PR ready. |
| `[DEV-TEAM] Signoff: changes_requested` | Post thread updates. Submit `REQUEST_CHANGES`. |

For all Jira transitions: read current issue state first; skip if already in the target
state (idempotency on resume). For new inline comments, `commitID` comes from
`git rev-parse HEAD` (one call per milestone batch). Exact GitHub MCP method names for
reply-to-thread and resolve-thread must be discovered at implementation time
(`mcp__github__help`).

### Key Classes

#### `PipelineContext` (`dev_team.py`)

- Add `review_threads: list[ReviewThread]` (default empty) and `first_push_done: bool`
  (default `False`)
- Remove `pr_details: dict` and `pr_url: str` — both move to the sidecar file
- Retain `base_branch: str`; populate in `main()` after context load, before
  `DevTeamPipeline.run()`, only when `ctx.base_branch == ""`. Algorithm: `git fetch
  --all`; candidates = `main` + remote `feature/*` branches; pick fewest commits ahead
  of HEAD (`git rev-list --count origin/<candidate>..HEAD`); prefer `main` on tie; fall
  back to `"main"`. Call `ctx.save()` immediately after. On resume with `base_branch`
  already set, the guard skips recomputation.
- `ctx.review_notes` stores the plain-text `body` string from the Reviewer's output
  (not the full JSON). It is written unconditionally (even empty) to avoid stale values.

#### `ValidateStep` (`dev_team.py`)

- On a clean pass, if `ctx.first_push_done` is `False`: set it to `True`, call
  `ctx.save()`, then emit `[DEV-TEAM] First push complete`. The save before the marker
  prevents re-emission on resume.
- `ValidateStep.run()` continues to return `"clean"` unconditionally; `first_push_done`
  gates only marker emission, not the trigger. `implement-task-plan.md` is unchanged.

#### `ReviewStep` (`dev_team.py`)

- Remove the `prepare-pr-details` call and its resume guard entirely.
- No resume guard for the reviewer call — it re-runs unconditionally at `reviewing` state.
- After parsing, assign `uuid4().hex[:8]` to each thread dict that lacks an `id` key
  (dict level, before constructing `ReviewThread` instances). Then replace
  `ctx.review_threads` with the deserialized result.
- Set `ctx.review_notes = result.get("body", "")`. Save context. Emit
  `[DEV-TEAM] Review ready: <status>`.

#### `SignoffStep` (`dev_team.py`)

- Pass `$BASE_BRANCH`, `$REVIEW_THREADS` (full list, including resolved), `$WORK_ITEM_ID`,
  and `$TASK_BRIEF` to `reviewer-sign-off`. Remove `$PR_URL` and `$REVIEW_NOTES`.
- `_run_reviewer_signoff()` return type: `(ok: bool, error_msg: str, threads: list[ReviewThread], body: str)`.
  `ok` means the agent ran without exception — not that threads are resolved. Remove the
  `nonlocal sign_off_result` pattern.
- After all futures resolve: if `ok=False` or thread list is empty/absent → `sys.exit(1)`
  (called in the main thread, not inside the worker). These checks fire before the
  `failures` list check.
- Merge sign-off threads into `ctx.review_threads` (by `id`; assign UUIDs to new threads
  same as `ReviewStep`; append new Reviewer comments; update `resolved`). Store `body` in
  `ctx.signoff_notes`. Call `ctx.save()` once after all mutations. Emit
  `[DEV-TEAM] Signoff: <status>`.
- Approval: `not any(not t.resolved for t in ctx.review_threads)` AND scripts pass AND
  researcher passes.

#### `ImplementStep` (`dev_team.py`)

- Emit `[DEV-TEAM] Implementation started` at the very top of `run()`, including on the
  early-return path (`ctx.work_summaries` non-empty). Emit `[DEV-TEAM] Implementation
  complete` at the end. Both fire on resume so the scrum master can catch up on missed
  Jira actions. Jira idempotency prevents double-transition.

#### `DebugStep` (`dev_team.py`)

- Emit `[DEV-TEAM] Debug complete` after the debugger agent returns successfully.

#### `FixStep` (`dev_team.py`)

- Add `"$REVIEW_THREADS": ""` to `call_agent()` substitutions so the placeholder in
  `developer-fix.md` is replaced with an empty string (not left as a literal).

#### `FixPrStep` (`dev_team.py`)

- Gains `__init__(self, context_path: Path)` storing `self._context_path`, matching
  `ReviewStep` and `SignoffStep`. Update instantiation in `DevTeamPipeline.__init__`.
- Pass only unresolved threads (`$REVIEW_THREADS`); pass `"$ISSUES": ""`.
- After the agent returns, call `parse_json_list_output()` to extract the updated threads
  array. If empty, print error and `sys.exit(1)`.
- Merge by `id`: replace `ctx.review_threads[matching_id].comments` with Developer's
  returned list (Developer echoes all existing comments plus its reply). Discard
  Developer's `resolved` field; preserve Reviewer's value. Silently discard unknown IDs.
- Call `ctx.save(self._context_path)` after merge, before returning the trigger.

#### `parse_json_list_output()` (`dev_team.py`)

New helper alongside `parse_json_output()`. Tries fenced code blocks first (from the
end, checking `isinstance(result, list)`), then bare lines where `json.loads(line.strip())`
returns a list. Returns `[]` if nothing parses. The fenced-first order is intentional:
the Developer prompt instructs fenced output.

#### `developer-fix.md` skill

- Remove Steps 3 and 4 (GitHub MCP fetch and reply).
- Add two clearly labeled substitution sections:
  - `## Build/Test Issues` → `$ISSUES`
  - `## Review Threads` → `$REVIEW_THREADS`
- Developer checks which section is populated to determine mode. In review-fix mode,
  Developer returns the full updated `threads[]` array (complete thread objects with all
  comments plus its new reply) in a fenced JSON code block at the end of its response.
  In build/test-fix mode, thread output is omitted.

#### `reviewer-sign-off.md` skill

- Replace `$PR_URL` and `$REVIEW_NOTES` inputs with `$BASE_BRANCH` and `$REVIEW_THREADS`.
- Update git diff commands to use `origin/$BASE_BRANCH`.
- Output `{"body": "...", "threads": [...]}` — no `status` field. For each thread
  evaluated: return it with updated `resolved` and any new comments only (empty `comments`
  list if no new comment added).

### Data Flow

```
Reviewer agent
  └─ outputs {body, threads[], status} JSON
       │
       ▼
ReviewStep (dev_team.py)
  └─ assigns IDs; saves ctx.review_threads; emits [DEV-TEAM] Review ready
       │
       ▼
Scrum master (dev-team.md)
  ├─ reads <!-- section:Review Threads --> and sidecar
  ├─ for each thread: reply existing or create new GitHub review comment
  └─ writes updated sidecar
       │
       ▼
FixPrStep (dev_team.py)
  └─ passes unresolved ctx.review_threads as $REVIEW_THREADS to developer-fix
       │
       ▼
Developer agent (developer-fix.md)
  └─ appends reply to each thread; returns full updated threads[] JSON block
       │
       ▼
FixPrStep (dev_team.py)
  └─ merges updated threads into ctx.review_threads; saves; transitions to signoff
```

## Implementation Notes

- **`resolved` invariant:** Only the Reviewer sets `resolved: true`. `dev_team.py`
  enforces this mechanically: it always discards the `resolved` field from Developer
  output during merge and preserves the Reviewer's last recorded value.

- **Sidecar write atomicity:** The scrum master writes the entire sidecar in a single
  atomic write after all GitHub posts for a milestone complete. If the write fails after
  successful posts, the next run re-posts those threads as new comments — accepted as a
  known harmless limitation.

- **`Signoff: approved` milestone ordering:** (1) Post all thread updates and write
  sidecar; (2) resolve threads with `resolved: true` using `githubThreadId` from the
  now-updated sidecar. New-and-immediately-resolved threads are posted first (gaining a
  sidecar entry), then resolved in step 2.

- **`Signoff: changes_requested` failure reasons:** Non-thread failures (scripts,
  researcher) accumulate in `ctx.review_notes` via the existing `failures` join pattern
  (unchanged). `<!-- section:Review Notes -->` carries those; `<!-- section:Review Threads -->`
  carries thread-based failures.

- **Section sentinel names are exact strings:** `<!-- section:Review Threads -->`,
  `<!-- section:Review Notes -->`, `<!-- section:Signoff Notes -->`. All occurrences in
  `save()`, `load()`, and `dev-team.md` must match exactly.

- **Files in scope:** `dev_team.py`, `dev-team.md`, `reviewer-review.md`,
  `reviewer-sign-off.md`, `developer-fix.md`, `developer.md`, `reviewer.md`.
  `fix-issue-plan.md` and `implement-task-plan.md` are not modified.

- **Migration:** No backward-compatibility handling for in-flight context files. Old runs
  on new code must be restarted or manually migrated by the operator.

## Tasks

> **Prerequisite:** Tasks 2 and 3 from `_spec_DevTeamPortability.md` (ADR-264: verbatim pipeline copy; ADR-265: minimal plugin adoption) must complete and be locally validated before ADR-253 begins. CloudDevTeam implementation runs entirely in the `dev-team-agents` repository. Do not update AdaptiveRemote's plugin until all four tasks below are complete.

### [ADR-253](https://jodasoft.atlassian.net/browse/ADR-253): `dev_team.py` — review thread data model and context

Add the `ReviewComment` and `ReviewThread` dataclasses; update `PipelineContext` to add
`review_threads` and `first_push_done`, remove `pr_details` and `pr_url`, and implement
camelCase JSON serialisation for thread fields; move `base_branch` population to `main()`
with an immediate `ctx.save()`; add the `parse_json_list_output()` helper.

**Exit criteria:**

- [ ] `ReviewComment` and `ReviewThread` dataclasses exist with the fields specified in the spec
- [ ] `PipelineContext.review_threads` serialises to/from `<!-- section:Review Threads -->` as camelCase JSON
- [ ] `PipelineContext.first_push_done` serialises to/from frontmatter as lowercase `true`/`false`
- [ ] `pr_details` and `pr_url` fields removed from `PipelineContext`
- [ ] `base_branch` is computed and saved in `main()` before `DevTeamPipeline.run()`, with the guard that skips recomputation when already set
- [ ] `parse_json_list_output()` exists, tries fenced blocks first, returns `[]` on parse failure
- [ ] All existing unit tests pass; new tests cover serialisation round-trips and `parse_json_list_output()` edge cases

---

### [ADR-254](https://jodasoft.atlassian.net/browse/ADR-254): `dev_team.py` — update pipeline steps

Update all pipeline step classes to use the review thread model introduced in Task 1.

**Exit criteria:**

- [ ] `ReviewStep`: `prepare-pr-details` call removed; UUIDs assigned to parsed thread dicts; `ctx.review_threads` and `ctx.review_notes` saved; emits `[DEV-TEAM] Review ready: <status>`
- [ ] `FixPrStep`: accepts `context_path` constructor arg; passes unresolved threads as `$REVIEW_THREADS` and `$ISSUES: ""`; merges Developer's returned thread list discarding `resolved` field; calls `ctx.save()`
- [ ] `SignoffStep`: uses `$BASE_BRANCH` / `$REVIEW_THREADS` substitutions; `_run_reviewer_signoff()` returns 4-tuple; merges sign-off threads; stores `body` in `ctx.signoff_notes`; emits `[DEV-TEAM] Signoff: <status>`; calls `sys.exit(1)` in main thread on failure
- [ ] `ValidateStep`: sets `ctx.first_push_done = True`, calls `ctx.save()`, then emits `[DEV-TEAM] First push complete` — only when `first_push_done` was `False`
- [ ] `ImplementStep`: emits `[DEV-TEAM] Implementation started` at top of `run()` and `[DEV-TEAM] Implementation complete` at end (both on resume)
- [ ] `DebugStep`: emits `[DEV-TEAM] Debug complete` after debugger agent returns
- [ ] `FixStep`: `$REVIEW_THREADS` substitution present and set to empty string
- [ ] All existing unit tests pass; new tests cover thread-merge logic in `FixPrStep` and `SignoffStep`, and the `sys.exit(1)` guard in `SignoffStep`

---

### [ADR-255](https://jodasoft.atlassian.net/browse/ADR-255): Update agent skill files

Update the three agent skill markdown files to use the new thread schema.

**Exit criteria:**

- [ ] `reviewer-review.md`: output schema uses `threads[]` instead of `comments[]`; thread objects have no `id` field
- [ ] `reviewer-sign-off.md`: inputs changed to `$BASE_BRANCH` and `$REVIEW_THREADS`; diff command uses `origin/$BASE_BRANCH`; output is `{"body": "...", "threads": [...]}` with no `status` field; Reviewer instructed to return every evaluated thread (empty `comments: []` if no new comment)
- [ ] `developer-fix.md`: Steps 3 and 4 (GitHub MCP fetch and reply) removed; `## Build/Test Issues` (`$ISSUES`) and `## Review Threads` (`$REVIEW_THREADS`) sections present; Developer instructed to return updated `threads[]` fenced JSON block in review-fix mode only

---

### [ADR-256](https://jodasoft.atlassian.net/browse/ADR-256): Update agent definitions and dev-team.md

Remove authenticated connector tools from sub-agent definitions and rewrite the scrum master milestone table.

**Exit criteria:**

- [ ] `developer.md`: all Jira MCP tools removed from the `tools:` list
- [ ] `reviewer.md`: output format description updated to reference `threads[]` instead of `comments[]`
- [ ] `dev-team.md`: milestone table replaced with all 8 milestones from the spec; sidecar file read/write instructions present for each relevant milestone; `[DEV-TEAM] PR details ready` reference removed
- [ ] The exact GitHub MCP method names for reply-to-thread and resolve-thread are discovered via `mcp__github__help` and used correctly

## Related Docs

- [`.claude/scripts/dev_team.py`](.claude/scripts/dev_team.py) — pipeline orchestrator
- [`.claude/commands/dev-team.md`](.claude/commands/dev-team.md) — scrum master instructions
- [`.claude/commands/reviewer-review.md`](.claude/commands/reviewer-review.md) — reviewer first-pass skill
- [`.claude/commands/reviewer-sign-off.md`](.claude/commands/reviewer-sign-off.md) — reviewer sign-off skill
- [`.claude/agents/developer.md`](.claude/agents/developer.md) — developer agent definition
- [`.claude/agents/reviewer.md`](.claude/agents/reviewer.md) — reviewer agent definition
