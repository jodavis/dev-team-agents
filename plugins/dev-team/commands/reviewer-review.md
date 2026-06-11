---
description: First-pass code review for the AdaptiveRemote dev-team pipeline. Creates a GitHub PR if one does not exist, retrieves the PR diff, reviews all changes against requirements and quality criteria, and posts a GitHub PR review. Outputs a JSON result indicating approved or changes_requested.
user-invocable: false
---

You are performing the first-pass code review for work item $WORK_ITEM_ID.

**Task brief:**
$TASK_BRIEF

**PR URL (empty = not yet created):** $PR_URL

**Spec file path:** $SPEC_PATH

---

## Step 1 — Load guidelines

Read `CONTRIBUTING.md` in full. This is the authoritative reference for all code
conventions you will evaluate.

## Step 2 — Understand the requirements

Re-read the task brief above and extract the exit criteria — the explicit list of things
the implementation must do or must not do. You will check each one during review.

Read the relevant `_doc_*.md` architecture files for any subsystem touched by this change.
Use `grep -rl "^Summary:" src test --include="_doc_*.md"` to discover available docs, then
read the `Summary:` line of each match to find the relevant ones.

## Step 3 — Verify PR exists

`$PR_URL` must be set before this skill runs — the pipeline creates the PR via
`developer-create-pr` before invoking the reviewer. If `$PR_URL` is empty, report an
error and stop.

## Step 4 — Retrieve and read the diff

Extract the pull number from `$PR_URL` (last path segment). Parse `owner` and `repo` from
the URL (e.g. `https://github.com/owner/repo/pull/123`).

Fetch the PR diff using the GitHub MCP:

```
mcp__plugin_github_github__pull_request_read(method="get_diff", owner=<owner>, repo=<repo>, pullNumber=<number>)
```

Read all changed files in full to understand the complete context of each change.

## Step 5 — Create a pending review

Before reviewing, create a pending review so inline comments can be added as issues are
discovered:

```
mcp__plugin_github_github__pull_request_review_write(method="create", owner=<owner>, repo=<repo>, pullNumber=<number>)
```

## Step 6 — Review the changes

Evaluate the diff against each dimension below, in priority order. For each issue you
find, note the file, line number, and a clear description of the problem.

### Priority 1 — Correctness and fault tolerance

- Are all exception paths handled? No swallowed exceptions, no empty `catch` blocks (unless there's a comment with a good justification).
- Are `CancellationToken` parameters present in every async method signature? No default
  values — callers must pass explicitly.
- Are there blocking calls (`.Result`, `.Wait()`, `Thread.Sleep`) on async code paths?
- Does error handling propagate faithfully, or does it silently discard failures?

### Priority 2 — Security

- Is user input validated at system boundaries?
- Are there SQL injection, command injection, or path traversal risks?
- Is sensitive data (tokens, passwords, PII) logged or returned in error messages?
- Are authentication/authorization checks present where the architecture requires them?

### Priority 3 — Performance

- Are there N+1 query patterns (fetching inside a loop that could be batched)?
- Is there synchronous I/O on async code paths?
- Are there unnecessary allocations in hot loops (string concatenation, LINQ on every
  call, etc.)?
- Are async-backed data fetches happening up front (fetch-first pattern) rather than
  scattered through processing logic?

### Priority 4 — Documentation

- Does new code conform to the design described in the relevant `_doc_*.md` files?
- If the implementation changed the design (new interface, changed responsibility,
  new dependency), has the relevant `_doc_*.md` been updated?
- Have new `_doc_*.md` files beed added where necessary?

### Priority 5 — Code style (note, do not block)

- Do naming conventions follow CONTRIBUTING.md (`ClassName_Method_Scenario_ExpectedResult`
  for tests, etc.)?
- Do log messages use `[LoggerMessage]` source-generated methods?
- Do tests use `MockBehavior.Strict` and `Expect_*` helpers?
- Is there a `CreateSut()` method?

## Step 7 — Post each inline issue as you find it

For each Priority 1–4 issue discovered in step 6, post it immediately to the pending
review using the **source file line number** (not the diff position):

```
mcp__plugin_github_github__add_comment_to_pending_review(owner=<owner>, repo=<repo>, pullNumber=<number>, path=<file>, line=<line>, side="RIGHT", subjectType="LINE", body=<comment>)
```

## Step 8 — Submit the review

Submit the pending review with an overall summary. Use `event: COMMENT` — do not use
`APPROVE` or `REQUEST_CHANGES`, as GitHub rejects those when the reviewer and PR author
share the same account.

```
mcp__plugin_github_github__pull_request_review_write(method="submit_pending", owner=<owner>, repo=<repo>, pullNumber=<number>, body=<overall summary>, event="COMMENT")
```

## Step 9 — Output

Write a concise plain-text summary of all issues found (one bullet per issue, Priority
1–4 first, style issues last). This summary will be passed to the developer so they can
address each point without re-reading the full PR thread.

Then output the following JSON object as the very last line of your response.
Write it as a bare JSON object — do not wrap it in a code block or add any text after it:

{"status": "approved|changes_requested", "pr_url": "https://github.com/..."}

Use `"approved"` if no Priority 1–4 issues were found; `"changes_requested"` otherwise.
Always include the PR URL even when approving.
