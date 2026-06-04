---
description: Address build errors, test failures, or code review comments against previously implemented work. Reads the original brief and work summary for context, then fixes each issue and returns a prose summary of changes.
argument-hint: <work-item-id>
---

## Inputs

Work item ID: `$ARGUMENTS`

### Original task brief

$TASK_BRIEF

---

### Work summary (all prior implementation and fix rounds)

$WORK_SUMMARIES

---

### Build/Test Issues

$ISSUES

---

### Review Threads

$REVIEW_THREADS

---

## Mode detection

- If `$REVIEW_THREADS` is populated: operate in **review-fix mode** (see Step 3).
- If `$ISSUES` is populated and `$REVIEW_THREADS` is empty: operate in **build/test-fix mode** (see Step 3).

## Steps

### 1 — Load standards

Read `CONTRIBUTING.md` for code guidelines.
Read `CLAUDE.md` for quality gates and operational conventions.

### 2 — Understand context

Read the original task brief and work summary to understand what was built and why. Then
read each issue or thread to be addressed.

### 3 — Fix each issue

**Build/test-fix mode** (`$ISSUES` populated, `$REVIEW_THREADS` empty):

For each issue:

- **Build error:** locate the root cause in the source or test files; do not patch over
  symptoms.
- **Test failure:** before fixing the production code, confirm whether the test itself is
  correct. If the test is wrong, fix the test and explain why in the report. If the test is
  right, write or verify a failing unit test that isolates the defect, then fix the code.

After each fix:

1. Build and test only the project(s) you changed to confirm the fix works without
   introducing new failures:

   ```bash
   dotnet build <project-path>
   dotnet test <test-project-path> --filter "FullyQualifiedName~<ClassName>"
   ```

   Where `<ClassName>` is the name of the class you modified. If the filter matches zero
   tests (no dedicated test class yet), run the full project test instead without `--filter`.

   **Scope:** Do **not** run `scripts/validate-build` or `scripts/validate-tests`. Those
   are full pipeline validation scripts run by the orchestrator after this step.

2. Commit the fix immediately with a message describing the specific issue resolved:

   ```bash
   git add -A
   git commit -m "$ARGUMENTS: <one-line description of what was fixed and why>"
   ```

   One commit per issue keeps the git history readable. Do not batch multiple fixes into a
   single commit.

**Review-fix mode** (`$REVIEW_THREADS` populated):

`$REVIEW_THREADS` is a JSON array of thread objects. Each thread has an `id`, `filePath`,
`lineNumber`, `resolved` flag, and `comments` array showing the full conversation so far.

For each thread with `"resolved": false`:

- **Agree with the feedback:** apply the requested code change.
- **Disagree with the feedback:** do not apply the change. Instead, append your rationale
  as a new entry in the thread's `comments` array:
  ```json
  {"author": "Developer", "comment": "<your rationale>"}
  ```
  The scrum master will post this reply to GitHub. Do not post directly to GitHub.

After addressing all threads, run the full build and tests to confirm no regressions:

```bash
dotnet build <project-path>
dotnet test <test-project-path>
```

Commit all changes with a single message:

```bash
git add -A
git commit -m "$ARGUMENTS: address review feedback"
```

**Do not push** — the pipeline pushes after all fixes pass full validation.

At the end of your response, output the complete updated `threads[]` array as a fenced
JSON code block. Include **every thread** from `$REVIEW_THREADS` — both the ones you
addressed and the ones you left unchanged. For each thread, include all prior comments
as-is; append your `{"author": "Developer", "comment": "..."}` entry only if you are
disagreeing with the change or need to explain your approach. Do not omit threads — the
pipeline fails if the returned list is empty.

```json
[
  {
    "id": "a1b2c3d4",
    "filePath": "src/Example/File.cs",
    "lineNumber": 42,
    "resolved": false,
    "comments": [
      {"author": "Reviewer", "comment": "<original reviewer comment>"}
    ]
  },
  {
    "id": "e5f6a7b8",
    "filePath": "src/Other/File.cs",
    "lineNumber": 17,
    "resolved": false,
    "comments": [
      {"author": "Reviewer", "comment": "<original reviewer comment>"},
      {"author": "Developer", "comment": "<your rationale for disagreeing>"}
    ]
  }
]
```

### 4 — Self-review

Review the diff for unintended scope, missed issues, and convention violations.

### 5 — Report

Return a fix summary as structured prose: for each issue, one sentence describing what was
changed and why.
