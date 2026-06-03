---
description: First-pass code review for the AdaptiveRemote dev-team pipeline. Creates a GitHub PR if one does not exist, retrieves the PR diff, reviews all changes against requirements and quality criteria, and posts a GitHub PR review. Outputs a JSON result indicating approved or changes_requested.
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

Fetch the PR diff using the `gh` CLI:

```bash
gh pr diff <pull-number>
```

Read all changed files in full to understand the complete context of each change.

## Step 5 — Review the changes

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

## Step 6 — Post the GitHub PR review

Create a **pull request review** (not a regular PR comment) using the `gh` CLI:

```bash
gh api "repos/${GITHUB_REPOSITORY}/pulls/<pull-number>/reviews" \
  --method POST \
  --field body='<overall summary>' \
  --field event='COMMENT' \
  --field comments='[{"path":"<file>","position":<diff-position>,"body":"<comment>"}]'
```

Notes:
- Use `event: COMMENT`, not `APPROVE` or `REQUEST_CHANGES` — GitHub rejects those from
  the PR author's account, and the developer and reviewer agents share the same account.
- Each entry in `comments` must use the **diff position** (line number within the unified
  diff), not the source file line number. Run `gh pr diff <number>`
  and count lines from the start of each file's hunk to get the position.
- Do NOT use `POST /repos/.../issues/{number}/comments` — that creates a plain conversation
  comment, not a structured review thread.

## Step 7 — Output

Write a concise plain-text summary of all issues found (one bullet per issue, Priority
1–4 first, style issues last). This summary will be passed to the developer so they can
address each point without re-reading the full PR thread.

Then output the JSON result as the final line:

```json
{"status": "approved|changes_requested", "pr_url": "https://github.com/..."}
```

Use `"approved"` if no Priority 1–4 issues were found; `"changes_requested"` otherwise.
Always include the PR URL even when approving.
