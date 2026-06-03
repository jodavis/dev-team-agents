---
description: First-pass code review for the AdaptiveRemote dev-team pipeline. Retrieves the branch diff, reviews all changes against requirements and quality criteria, and outputs a structured JSON result with review threads. The scrum master posts all GitHub comments.
---

You are performing the first-pass code review for work item $WORK_ITEM_ID.

**Task brief:**
$TASK_BRIEF

**Base branch:** $BASE_BRANCH

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

## Step 3 — Retrieve and read the diff

Fetch the diff of all changes on this branch relative to the base:

```bash
git diff origin/$BASE_BRANCH
```

Read all changed files in full to understand the complete context of each change.

## Step 4 — Review the changes

Evaluate the diff against each dimension below, in priority order. For each issue you
find, note the file, **source file line number** (1-based line in the actual file, not
the diff position), and a clear description of the problem.

**Important:** `lineNumber` must be a line number in the source file, not an offset within
the unified diff. Do not count diff header lines or context lines — look up the actual line
in the file.

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
- Have new `_doc_*.md` files been added where necessary?

### Priority 5 — Code style (note, do not block)

- Do naming conventions follow CONTRIBUTING.md (`ClassName_Method_Scenario_ExpectedResult`
  for tests, etc.)?
- Do log messages use `[LoggerMessage]` source-generated methods?
- Do tests use `MockBehavior.Strict` and `Expect_*` helpers?
- Is there a `CreateSut()` method?

## Step 5 — Output

Write a concise plain-text summary of all issues found (one bullet per issue, Priority
1–4 first, style issues last). This summary will be passed to the developer so they can
address each point without re-reading the full PR thread.

Then output the JSON result as the final fenced code block. Each thread object represents
one discrete issue at a specific location. Do **not** include an `id` field — IDs are
assigned by the pipeline after parsing.

```json
{
  "body": "<overall review summary>",
  "status": "approved|changes_requested",
  "threads": [
    {
      "filePath": "src/Example/File.cs",
      "lineNumber": 42,
      "resolved": false,
      "comments": [
        {"author": "Reviewer", "comment": "<issue description>"}
      ]
    }
  ]
}
```

Field rules:
- `filePath`: camelCase key, value is the repo-relative file path
- `lineNumber`: 1-based line number in the source file (not the diff position)
- `resolved`: always `false` on first review
- `comments`: array with one entry per comment; each entry has `author` and `comment` string fields
- `threads`: empty array `[]` if no issues were found
- `status`: `"approved"` if no Priority 1–4 issues were found; `"changes_requested"` otherwise
