---
description: Entry point for the dev-team agent pipeline. Routes to the correct pipeline based on the work item request, then starts the pipeline script and relays its output.
argument-hint: <request, e.g. "implement ADR-172" or "fix issue #444">
---

## Request

$ARGUMENTS

## Role

You represent a development team. Your job is to take a work item request, choose a work 
plan (pipeline), and then hand off the work item and the plan the pipeline script. Then you
report progress to the user by watching the output of the script. The script is the orchestrator.
Once you have started the script you are a passive observer.

**Never attempt to:**
- Fix build errors or test failures
- Edit source files or test files
- Invoke agent skills directly (researcher-plan, developer-implement, developer-fix, etc.)
- Take any action in response to failures reported in the script output

If the script exits with an error, report the final output to the user and stop. Do not
attempt recovery.

## Steps

### 1 — Determine pipeline and work item ID

Analyze the request using your judgment:

- If the request refers to a **Jira task** — e.g. "implement ADR-123", "ADR-123", or
  any `[A-Z]+-\d+` pattern — use:
  - Pipeline: `implement-task-plan`
  - Research skill: `researcher-plan`
  - Work item ID: the Jira key as-is (e.g. `ADR-123`)

- If the request refers to a **GitHub issue** — e.g. "fix issue #444", "#444", or
  any `#\d+` pattern — use:
  - Pipeline: `fix-issue-plan`
  - Research skill: `researcher-issue`
  - Work item ID: `Issue-<number>` (strip the `#`, e.g. `#444` → `Issue-444`)

- If the intent is unclear, tell the user:

  > I'm not sure which work plan to use for this request. Provide a Jira task key
  > (e.g. ADR-123) to use the implementation pipeline, or a GitHub issue number
  > (e.g. #444) to use the fix-issue plan.

  Then stop.

### 2 — Check the platform

```bash
python -c "import sys; print(sys.platform)"
```

### 3 — Start the pipeline script in the background

```bash
python -u ${CLAUDE_PLUGIN_ROOT}/scripts/dev_team.py <work-item-id> --workflow ${CLAUDE_PLUGIN_ROOT}/scripts/<pipeline>.md --research-skill <research-skill> --plugin-root ${CLAUDE_PLUGIN_ROOT}
```

### 4 — Stream output

**Immediately** call the Monitor tool on the background process to stream its output.
Do not wait. Do not use TaskOutput. Use the platform-appropriate tail command:
- **`win32`**: `powershell -Command "Get-Content -Wait -Path '<task-output-path>'"`
- **anything else**: `tail -f <task-output-path>`

Stream all output to the user as it arrives until the process exits.

### 5 — Report exit status

When the process exits, report its exit status to the user. Take no further action.

### 6 — Handle pipeline milestones

While monitoring the script output in Step 4, act on each `[DEV-TEAM]` marker as it
appears. Each marker triggers GitHub and/or Jira operations described below.

**This step runs concurrently with Step 4.** Do not wait for the pipeline to finish before
acting on a marker.

---

#### Sidecar file

The sidecar is your own persistence store, separate from the context file owned by
`dev_team.py`. Path (use forward slashes — works on both Windows and Linux):

```
.claude/logs/dev-team/<work-item-id>-threads.json
```

Structure:

```json
{
  "pr_url": "https://github.com/owner/repo/pull/42",
  "threads": [
    { "id": "a1b2c3", "githubCommentId": 123456789, "githubThreadId": "PRRT_kwDO..." }
  ]
}
```

- `pr_url`: written when the PR is created; read on every subsequent milestone.
- `githubCommentId`: numeric REST comment ID; used to reply to an existing thread.
- `githubThreadId`: GraphQL node ID (`PRRT_...` format); used to resolve a thread.
- Load the sidecar at the start of each milestone that needs it.
- Write the entire sidecar atomically after all GitHub posts for a milestone complete.
- If the file does not exist when loading, treat it as `{"pr_url": "", "threads": []}`.

#### Context file reads

Read `.claude/logs/dev-team/<work-item-id>-context.md` to extract content. Sections are
delimited by `<!-- section:Name -->` sentinels — everything after a sentinel until the
next sentinel is the section body. Frontmatter fields (between the first two `---`
delimiters) provide metadata such as `base_branch`.

Exact sentinel names:
- `<!-- section:Debug Report -->`
- `<!-- section:Researcher Brief -->`
- `<!-- section:Implementation Summary -->`
- `<!-- section:Review Threads -->`
- `<!-- section:Review Notes -->`
- `<!-- section:Signoff Notes -->`

#### Jira idempotency guard

Before any Jira transition, read the current issue status with
`mcp__claude_ai_Atlassian_Rovo__getJiraIssue`. Skip the transition if the issue is
already in the target state. This allows safe resume after interruption.

#### Thread-posting logic (shared by Review and Signoff milestones)

For each thread object from `<!-- section:Review Threads -->`:

1. Check whether `thread.id` appears in `sidecar.threads`.
2. **If found (existing thread):** reply to the existing GitHub comment:
   ```bash
   gh api "repos/${GITHUB_REPOSITORY}/pulls/comments/<githubCommentId>/replies" \
     --method POST --field body="<last comment body from thread>"
   ```
3. **If not found (new thread):** create a new inline review comment. First get the
   current commit ID (once per milestone): `git rev-parse HEAD`. Then:
   ```bash
   gh api "repos/${GITHUB_REPOSITORY}/pulls/<pull-num>/reviews" \
     --method POST \
     --field body='' \
     --field event='COMMENT' \
     --field "comments=[{\"path\":\"<filePath>\",\"line\":<lineNumber>,\"side\":\"RIGHT\",\"body\":\"<comment>\",\"commit_id\":\"<commitId>\"}]"
   ```
   After posting, retrieve the thread's GraphQL node ID so it can be stored in the
   sidecar for later resolution. Query the PR's review threads and match the comment's
   numeric `id` against `databaseId`:
   ```bash
   OWNER="${GITHUB_REPOSITORY%%/*}"
   REPO="${GITHUB_REPOSITORY##*/}"
   gh api graphql -f query='
     query($owner: String!, $repo: String!, $pullNumber: Int!) {
       repository(owner: $owner, name: $repo) {
         pullRequest(number: $pullNumber) {
           reviewThreads(first: 100) {
             nodes {
               id
               comments(first: 10) { nodes { databaseId } }
             }
           }
         }
       }
     }' -F owner="$OWNER" -F repo="$REPO" -F pullNumber=<pull-num>
   ```
   Match the returned `databaseId` to the numeric comment ID from the review response
   to find `githubThreadId`.  Add `{ "id": "<thread.id>", "githubCommentId": <num>,
   "githubThreadId": "<PRRT_...>" }` to the sidecar threads list.

After processing all threads for a milestone, write the updated sidecar atomically.

---

#### Milestone: `[DEV-TEAM] Implementation started`

1. Get your Atlassian account ID via `mcp__claude_ai_Atlassian_Rovo__atlassianUserInfo`.
2. **Jira pipeline:** Assign the work item to yourself via
   `mcp__claude_ai_Atlassian_Rovo__editJiraIssue` with the `assignee` field.
3. **Jira pipeline:** Transition to "In Progress" via
   `mcp__claude_ai_Atlassian_Rovo__transitionJiraIssue`. Apply idempotency guard.
4. **Issue pipeline:** Assign the GitHub issue to yourself:
   ```bash
   gh issue edit <issue-num> --add-assignee @me
   ```

---

#### Milestone: `[DEV-TEAM] Debug complete` *(Issue pipeline only)*

1. Read `<!-- section:Debug Report -->` from the context file.
2. Post root cause as a GitHub issue comment:
   ```bash
   gh issue comment <issue-num> --body "<debug-report-content>"
   ```

---

#### Milestone: `[DEV-TEAM] Implementation complete`

1. Read `<!-- section:Implementation Summary -->` from the context file.
2. **Jira pipeline:** Post as a Jira comment via
   `mcp__claude_ai_Atlassian_Rovo__addCommentToJiraIssue`.
3. **Issue pipeline:** Post as a GitHub issue comment:
   ```bash
   gh issue comment <issue-num> --body "<implementation-summary>"
   ```

---

#### Milestone: `[DEV-TEAM] First push complete`

1. Read `<!-- section:Researcher Brief -->` and `<!-- section:Implementation Summary -->`
   from the context file.
2. Read `base_branch` from the context file frontmatter.
3. Load the sidecar. If `pr_url` is already set, skip to step 6 (PR already exists).
4. Draft a PR title: `<work-item-id>: <one-line summary from the Researcher Brief>`.
   Draft a PR body that includes the implementation summary.
5. Create the draft PR:
   ```bash
   gh pr create --draft \
     --title "<work-item-id>: <summary>" \
     --body "<pr-body>" \
     --base <base_branch>
   ```
6. Write the sidecar:
   ```json
   {"pr_url": "<url from gh pr create output>", "threads": []}
   ```
7. **Jira pipeline:** Transition to "In Review" via
   `mcp__claude_ai_Atlassian_Rovo__transitionJiraIssue`. Apply idempotency guard.

---

#### Milestone: `[DEV-TEAM] Review ready: changes_requested`

1. Read `<!-- section:Review Threads -->` from the context file (JSON array of threads).
2. Load the sidecar.
3. Extract pull number from `pr_url`.
4. Get `commitId`: `git rev-parse HEAD`.
5. Apply **thread-posting logic** for each thread (see above).
6. Submit the review:
   ```bash
   gh api "repos/${GITHUB_REPOSITORY}/pulls/<pull-num>/reviews" \
     --method POST \
     --field body="<review summary from Review Notes>" \
     --field event='REQUEST_CHANGES'
   ```
7. Write updated sidecar atomically.

---

#### Milestone: `[DEV-TEAM] Review ready: approved`

Same as `Review ready: changes_requested` but submit with `event='APPROVE'`.

---

#### Milestone: `[DEV-TEAM] Signoff: approved`

1. Read `<!-- section:Review Threads -->` from the context file.
2. Load the sidecar.
3. Extract pull number from `pr_url`.
4. Get `commitId`: `git rev-parse HEAD`.
5. Apply **thread-posting logic** for each thread — post all updates (replies or new
   comments) and write the updated sidecar **before** resolving.
6. Resolve every thread whose `resolved` field is `true` in the context file, using
   `githubThreadId` from the now-updated sidecar:
   ```bash
   gh api graphql -f query='mutation {
     resolveReviewThread(input: {threadId: "<githubThreadId>"}) {
       thread { isResolved }
     }
   }'
   ```
7. Submit `APPROVE` review:
   ```bash
   gh api "repos/${GITHUB_REPOSITORY}/pulls/<pull-num>/reviews" \
     --method POST --field body='' --field event='APPROVE'
   ```
8. Mark the PR ready for review:
   ```bash
   gh pr ready <pull-num>
   ```
9. Request a review from the pipeline operator (best-effort — skip on error):
   ```bash
   gh pr edit <pull-num> --add-reviewer <operator-github-username>
   ```
10. **Jira pipeline:** Assign the work item to the pipeline operator via
    `mcp__claude_ai_Atlassian_Rovo__editJiraIssue`.
11. **Issue pipeline:** Assign the GitHub issue to the pipeline operator:
    ```bash
    gh issue edit <issue-num> --add-assignee <operator-github-username>
    ```

---

#### Milestone: `[DEV-TEAM] Signoff: changes_requested`

1. Read `<!-- section:Review Threads -->` from the context file.
2. Load the sidecar.
3. Extract pull number from `pr_url`.
4. Get `commitId`: `git rev-parse HEAD`.
5. Apply **thread-posting logic** for each thread.
6. Write updated sidecar atomically.
7. Submit `REQUEST_CHANGES` review:
   ```bash
   gh api "repos/${GITHUB_REPOSITORY}/pulls/<pull-num>/reviews" \
     --method POST \
     --field body="<signoff notes>" \
     --field event='REQUEST_CHANGES'
   ```
