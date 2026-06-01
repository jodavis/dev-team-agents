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

### Issues to fix

$ISSUES

---

## Steps

### 1 — Load standards

Read `CONTRIBUTING.md` for code guidelines.
Read `CLAUDE.md` for quality gates and operational conventions.

### 2 — Understand context

Read the original task brief and work summary to understand what was built and why. Then
read each issue to be fixed.

### 3 — Fetch review comment threads from the PR

If `$ISSUES` contains code review comments (i.e., it references a PR URL), fetch the open
review comment threads directly from the PR using the GitHub MCP rather than relying solely
on the summary in `$ISSUES`. This ensures you see all comments including those from human
reviewers and GitHub Copilot, not only those the orchestrator relayed.

Use `$ISSUES` for non-review-comment issues (build errors, test failures) as provided.

### 4 — Triage

For each issue:

- **Build error:** locate the root cause in the source or test files; do not patch over
  symptoms.
- **Test failure:** before fixing the production code, confirm whether the test itself is
  correct. If the test is wrong, fix the test and explain why in the report. If the test is
  right, write or verify a failing unit test that isolates the defect, then fix the code.
- **Code review comment:** read the comment and understand the intent.
  - **Agree:** apply the change.
  - **Disagree:** post your rationale as a reply to the PR review thread. Do NOT apply the
    change. The Reviewer will see your response during sign-off and decide whether to accept
    the rationale or push back. Only apply the change if the Reviewer pushes back in a
    subsequent round.
  - In either case, always post a reply to the PR review thread explaining what was done
    (or why nothing was done). This keeps all reviewers — agent and human — informed.

### 5 — Fix each issue

Address issues one at a time. After each fix:

1. Build and test only the project(s) you changed to confirm the fix works without
   introducing new failures:

   ```bash
   dotnet build <project-path>
   dotnet test <test-project-path> --filter "FullyQualifiedName~<ClassName>"
   ```

   Where `<ClassName>` is the name of the class you modified. If the filter matches zero
   tests (no dedicated test class yet), run the full project test instead without `--filter`.

   **Scope:** Do **not** run `scripts/validate-build` or `scripts/validate-tests`. Those
   are full pipeline validation scripts run by the orchestrator after this step — running
   them here is redundant and slows the fix loop.

2. Commit the fix immediately with a message describing the specific issue resolved:

   ```bash
   git add -A
   git commit -m "$ARGUMENTS: <one-line description of what was fixed and why>"
   ```

   One commit per issue keeps the git history readable and makes individual fixes easy to
   review. Do not batch multiple fixes into a single commit.

**Do not push** — the pipeline pushes after all fixes pass full validation.

### 6 — Self-review

Review the diff for unintended scope, missed issues, and convention violations.

### 7 — Report

Return a fix summary as structured prose: for each issue, one sentence describing what was
changed and why.
