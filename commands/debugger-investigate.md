---
description: Reproduce a GitHub issue bug, trace its root cause, and produce a root-cause report.
argument-hint: <Issue-NNN work item ID>
---

## Inputs

Work item ID: the first token of `$ARGUMENTS` (e.g. `Issue-444`)

If missing, stop and tell the caller:

> Usage: `/debugger-investigate <Issue-NNN>`

---

## Steps

### 1 — Fetch the GitHub issue

Derive the issue number by stripping the `Issue-` prefix from the work item ID
(e.g. `Issue-444` → `444`). Fetch the issue title, body, and all comments:

```bash
gh issue view <number> --comments
```

If the issue is not found, stop and report the error.

### 2 — Ensure a working branch

Check the current branch:

```bash
git branch --show-current
```

If already on a `dev/claude/<work-item-id>-*` branch, continue. Otherwise create one
before making any code changes by invoking the shared skill:

```bash
/create-branch <work-item-id> "<issue title>"
```

### 3 — Read architecture docs

Read `src/_doc_Projects.md`. Then read the `_doc_*.md` files for each subsystem the
issue's description touches. Do not skip this step — the architecture context is
essential for recognising the responsible code path.

### 4 — Classify the issue

Determine whether the bug manifests in an **existing test that is already failing**, or
whether it requires a **new repro test**:

- *Existing test:* The issue references a specific test by name, or running the relevant
  test suite reveals a failure matching the described behaviour. Go to step 5a.
- *New repro needed:* No existing test covers the bad behaviour. Go to step 5b.

### 5a — Existing test: confirm the failure

Run the referenced or relevant test(s) using the `run-tests` skill (with `filter-syntax`
to scope to the specific test if the suite is large). Confirm the failure matches the
behaviour described in the issue. If it does not match, treat the issue as "new repro
needed" and continue at step 5b.

### 5b — New repro: write a failing test

Write a minimal Gherkin scenario in the headless host
(`test/AdaptiveRemote.EndToEndTests.Host.Headless/Features/`) that exercises the
reported bad behaviour. Follow the conventions in `test/_doc_EndToEndTests.md`.
The scenario and each step must be actions or observations that a human can perform and
verify manually.

Run the new test. It should **pass** (confirming the bad behaviour is currently
observable). If it does not pass — the behaviour has already changed — proceed to
step 9 with status `not_reproduced`.

### 6 — Anchor the correct behaviour

Modify the test from step 5b (or the existing test from 5a) to assert the **correct**
behaviour instead of the bad one. Run it again. It should now **fail**. This failing
test is your investigation anchor and the starting point for the developer.

If you cannot get a clean "correct assertion fails" state, document why in the report.

### 7 — Investigate the root cause

Read the source code along the relevant code path. Form 1–3 specific, falsifiable
hypotheses. For each:

1. State the hypothesis clearly.
2. Identify what evidence would disprove it.
3. Look for that evidence — in code, in log output, or at runtime.

Use the available skills where they add value:
- `dotnet-trace-collect` — profile CPU or allocation if a performance anomaly is
  suspected.
- `dump-collect` — capture a process dump if the process is hanging or crashing.
- `mtp-hot-reload` — iterate quickly on test changes without full rebuilds.

If logging is insufficient, add `[LoggerMessage]` entries in
`src/AdaptiveRemote.App/Logging/MessageLogger.cs`, rebuild, and rerun. Use the event ID
range appropriate to the subsystem (see existing entries for reference).

Do not stop at "plausible" — keep going until at least one hypothesis is confirmed with
direct evidence.

### 8 — Clean up

Remove any temporary investigation-only changes from production code (ad-hoc prints,
throw-to-test-flow hacks, etc.). Keep:

- The test from step 5b / 6 (in its failing, correct-assertion form).
- Any `[LoggerMessage]` additions.

Commit all kept changes:

```bash
git add -A
git commit -m "<work-item-id>: repro test + diagnostic logging"
```

### 9 — Output

Write the report and status object.

**If reproduction succeeded**, output:

```
# Debug report for <work-item-id>

## Reproduction steps

<Minimal steps that trigger the bug — enough for someone unfamiliar with the issue
to observe it, and each step is something a human can perform manually.>

## Confirmed root cause

<The specific class, method, and line(s) responsible. Cite file paths. Explain
the mechanism — what invariant is violated, what code path leads there.>

## Ruled-out hypotheses

<One bullet per hypothesis that was investigated and eliminated, with the evidence
that eliminated it.>

## Supporting evidence

<Relevant log snippets, stack traces, or trace output. Keep it concise — enough to
substantiate the root cause, not a full dump.>
```

Then on its own line at the end:

```json
{"status": "reproduced"}
```

**If the bug could not be reproduced** (behaviour has changed, test passes in the
correct-assertion form already), output a brief explanation and then:

```json
{"status": "not_reproduced", "reason": "<one sentence describing what was observed instead>"}
```

### 10 — Comment on the GitHub issue

If status is `reproduced`, add a GitHub issue comment summarizing the confirmed root cause
that the fix will address:

```bash
gh issue comment <number> --body "Root cause summary: <1-3 sentences. Include affected file/class and failing behavior>."
```
