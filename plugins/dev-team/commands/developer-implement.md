---
description: Implement a feature or bug fix from a Researcher task brief. Writes tests first, then implements, then returns a structured work summary.
argument-hint: <work-item-id>
user-invocable: false
---

## Inputs

Work item ID: `$ARGUMENTS`

Task brief:

$TASK_BRIEF

---

## Steps

### 0 — Ensure feature branch

Check the current branch:

```bash
git branch --show-current
```

If the branch name contains `$ARGUMENTS` (the work item ID, e.g. `Issue-123`), the correct
branch is already active — proceed to Step 1.

Otherwise, invoke the shared branch-creation skill:

```bash
/create-branch $ARGUMENTS "<task brief first sentence>"
```

Do not push — the pipeline pushes after validation passes.

### 1 — Load standards

Read `CONTRIBUTING.md` for code guidelines.
Read `CLAUDE.md` for quality gates and operational conventions.

### 2 — Understand the task

Read the task brief above in full. Identify:

- The exit criteria — these define what "done" means
- Files to create or modify, and the design decisions that constrain each
- Existing utilities, base classes, and patterns to reuse (the brief will call these out)

If anything in the brief is ambiguous and the ambiguity would affect correctness, note it
in your work summary and resolve it conservatively.

### 3 — TDD: E2E / API tests first

Write Gherkin scenarios that cover the exit criteria before writing any implementation code.
Use existing steps whenever possible. When new steps are needed, follow the conventions in
`CONTRIBUTING.md`:

- Generalized `When` / `Then` / `Given` phrasing where each step is something a human could
  do or observe manually to reproduce the behavior
- Step definitions delegate logic to test service methods

Run the new scenarios and confirm they fail (nothing is implemented yet).

### 4 — TDD: unit tests

Write unit tests for each component before implementing it. Follow the CLAUDE.md test
conventions: Moq with `MockBehavior.Strict`, `private readonly` mock fields, `Expect_*`
helpers, `CreateSut()`, and full async dependency coverage.

Confirm the tests fail before proceeding.

### 5 — Implement

Implement the feature layer by layer, making the failing tests pass one layer at a time.

After each layer, build and test only the code you have modified:

```bash
dotnet build <project-path>
dotnet test <test-project-path> --filter "FullyQualifiedName~<ClassName>"
```

Where `<ClassName>` is the name of the class you just implemented. If the filter matches
zero tests (e.g., at the very start before any test classes exist), run the full project
test without `--filter`.

Fix any build errors or new test failures before moving to the next layer.

### 6 — Commit

Commit all changes with a clear message that describes what was implemented and why:

```bash
git add -A
git commit -m "$ARGUMENTS: <short description of what was implemented>"
```

The message body (optional) can list the key decisions if they are non-obvious.

**Do not push** — the pipeline pushes after validation passes.

### 7 — Self-review

Review the diff as if you were doing a code review:

- Does every exit criterion have demonstrable coverage (code + test)?
- Are there missing test cases (branches, error paths, invalid inputs)?
- Do all files follow CONTRIBUTING.md naming, structure, and logging conventions?
- Is there any scope creep — changes not required by the brief?

### 8 — Report

Return a work summary as structured prose:

**Files created or modified**
List each file by path with a one-line description of what changed.

**Key decisions made**
Anything not dictated by the brief that you chose during implementation (design choices,
interface splits, tradeoffs). Omit this section if there are none.

**Unit tests**
File path(s) and test method names for all new or modified unit tests.

**E2E scenarios**
Feature file path(s) and scenario title(s) for all new or modified Gherkin scenarios.
