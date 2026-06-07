---
description: Draft a _spec_*.md design document for a new feature or GitHub issue, iterating with the user until the document is ready
argument-hint: <ADR-nnn | #nnn | feature name and description>
---

## Feature or issue to spec

$ARGUMENTS

### Interpreting the argument

Determine the source of the work item before doing anything else:

| Argument form | Source | Action |
|---------------|--------|--------|
| `ADR-nnn` (e.g. `ADR-42`) | Jira epic | Fetch the epic via the Jira MCP tool and use its summary and description as the feature brief |
| `#nnn` (e.g. `#123`) | GitHub issue | Fetch the issue via `gh issue view nnn` and use its title and body as the feature brief |
| Anything else | Plain text | Use the argument text directly as the feature brief |

Resolve the feature brief before Phase 1 begins. If the Jira epic or GitHub issue does not exist, tell the user and stop.

## Available architecture docs

!`find . -name "_doc_*.md" -not -path "./.git/*" | sort`

## Workflow

This is an iterative process. Follow the phases below. Never skip ahead —
always wait for user input at each pause point before continuing.

---

### Phase 1 — Orient and gather context

**Step 1 — Resolve the feature brief** (if not already done above):
Follow the argument-routing table in "Interpreting the argument" to fetch the
Jira epic or GitHub issue and produce the feature brief before doing anything
else. In either case, both the main description and discussion comments should
be considered as source material for the spec. If the source cannot be fetched, 
tell the user and stop.

**Step 2 — Read architecture docs and source code:**
Read the relevant `_doc_*.md` files from the list above. Read all that apply
to the feature area; at minimum read `src/_doc_Projects.md`. Also read any
relevant source code in the workspace.

Then use the `AskUserQuestion` tool to ask the user focused questions that fill
gaps the docs and feature description don't answer. Good questions cover:

- Ownership and boundaries (what this feature owns vs. delegates)
- Integration points with existing subsystems
- Key design choices where multiple reasonable approaches exist
- Constraints (performance, accessibility, testability requirements)
- Anything the planned implementation section will need to be concrete

Skip questions you can already answer from the docs, feature description, or
source code. For each question, provide 2–4 concrete option choices reflecting
the most likely approaches; the user can always pick "Other" to write a custom
answer. The tool accepts 1–4 questions per call — if you have more than 4,
ask them in batches and wait for answers between batches.

**PAUSE — wait for the user's answers before continuing.**

If the answers raise new ambiguities that would materially affect the spec,
use `AskUserQuestion` for one more targeted follow-up round. Otherwise proceed
to Phase 2.

---

### Phase 2 — First draft

Determine the spec file location: the `_spec_*.md` lives next to the code it
describes — in the directory where the new feature's code will live. Use the
project boundaries doc if uncertain.

Name: `_spec_<FeatureName>.md` in PascalCase

Draft and write the file using the structure at the end of this prompt.
Fill every section. For anything genuinely unresolved, use `> TBD: reason`
inline and list it again in Open Questions.

After writing, tell the user:

> Draft written to `<path>`. Please review it — edit any section directly
> and add `> **Review:** your comment or question` anywhere you want a
> change made or a question answered. Tell me when you're ready for the
> next pass.

**PAUSE — wait for the user to review and signal readiness.**

---

### Phase 3 — Iterative refinement

When the user signals they're ready:

1. Re-read the spec file with the Read tool.
2. Collect all `> **Review:** ...` markers and note any direct edits.
3. Address review comments **one at a time** in document order:
   a. Present your analysis of the comment — the trade-offs, your
      recommendation, and why.
   b. **PAUSE — wait for the user's decision before editing.**
   c. Update the spec to reflect the resolved decision; remove the
      review marker.
   d. Tell the user what changed, then move to the next comment.
4. After all comments are resolved, invite another review pass.

Repeat Phase 3 until the user says the document is ready.

---

### Phase 4 — Implementation readiness review

When the user says the document is ready:

1. Spawn a Researcher subagent to review the spec:
   - `subagent_type: researcher`
   - Prompt: `"Invoke the researcher-spec-review skill with this spec file path: <path>"`
   - Do not include any conversation context in the prompt — the Researcher
     should have only the spec and the codebase to work from.
2. If the Researcher returns questions, use the `AskUserQuestion` tool to ask
   the user until you have enough information to address all of them. Provide
   2–4 option choices per question; batch up to 4 questions per call.
   **PAUSE between batches — wait for answers before editing.**
   Then update the spec: integrate the answers naturally into the appropriate
   sections (do not append a Q&A block). If the Researcher cited any external
   resources, add them to the `## Related Docs` section.
3. If the Researcher returned `No blocking questions — spec is implementation-ready`,
   proceed to step 4. Otherwise repeat from step 1 — but no more than 3 times total.
   After 3 iterations, proceed to step 4 regardless of the Researcher's output.
4. Tell the user the spec is implementation-ready and proceed to Phase 5.

---

### Phase 5 — Task breakdown and Jira tickets

When Phase 4 is complete:

1. Determine whether a Jira epic is already known:
   - If the original argument was an `ADR-nnn` key, that epic is already
     known — use it directly and skip the question below.
   - Otherwise, use `AskUserQuestion` to ask: "Is there a Jira epic for
     this feature?" Provide options for "Yes — I'll provide the key",
     "No - Create one for this feature", "No - I'll create one later", and
     "No — skip Jira entirely". If the user selects "Yes", follow up with
     another `AskUserQuestion` (or ask for "Other" input) to collect the key.
   **PAUSE — wait for the answer before continuing.**
2. Add a `## Tasks` section at the end of the spec file. Break the work
   into tasks sized to roughly one PR each. For each task write:
   - A short title
   - A one-sentence description
   - Exit criteria as a checkbox list; for tasks that include new E2E tests,
     write those exit criteria as Gherkin-style acceptance scenarios
     (`Given / When / Then`)
3. If the spec has a `## Related Epics` section listing features to be
   spec'd separately, add those as placeholder entries in `## Tasks` as
   well — titled "Create epic: \<name\>" with a one-line scope description.
   These will become Jira epics (not tasks) in step 5.
4. Save the updated spec and ask the user to review the task breakdown.
   **PAUSE — wait for approval or change requests. Apply any changes
   before proceeding.**
5. Create Jira issues for each item:
   - If there is no Jira epic for this feature and the user selected 
     "No - Create one for this feature" in step 1, create an epic now and
     save the epic key.
   - For tasks: create as Task issues. If the user provided an epic key or
     a new epic was created, assign it as the parent. If not, create without a parent.
   - For "Create epic" placeholder items: create as Epic issues (no parent).
     Use the scope description as the epic summary.
6. Update the `## Tasks` section: replace each item title with a hyperlink
   to its Jira ticket. Keep all descriptions and exit criteria in place.
   The section remains in the spec permanently — future agents may not
   have Jira access.
7. Update the `## Related Epics` table with the Jira keys assigned to each
   related epic in step 5.
8. Update the Jira epic's description with a concise summary of the
   finalized design decisions from the spec. The original description
   typically contains early design thoughts that are now superseded; replace
   it with a brief overview and a bulleted list of the key decisions and
   their outcomes. Link to the spec file in the repo.

---

## Spec file structure

Use this structure for the `_spec_*.md` file:

---

# \<Feature Name\>

> **Status:** Draft
> **Will become:** `_doc_<FeatureName>.md` once implementation is complete

## Overview

One paragraph: what this feature does and why it exists.

## Responsibilities & Boundaries

- **Owns:** ...
- **Does not own:** ...
- **Integrates with:** ...

## Key Design Decisions

### \<Decision title\>

_Context:_ Why this choice was needed.
_Decision:_ What was decided.
_Consequences:_ Trade-offs accepted.

_(Repeat for each significant decision.)_

## Planned Implementation

### Interfaces

Public interfaces — method signatures, types, and responsibilities.
This section is more detailed than a `_doc_` file because the source
doesn't exist yet.

### Key Classes

Planned classes, their roles, and important relationships.

### Data Flow

How data moves through the feature from trigger to output.

## Related Epics

Features identified during spec drafting that are out of scope here and will
be spec'd separately. Each row becomes a Jira epic in Phase 5.

| Epic | Scope |
|------|-------|
| (this epic) | ... |
| ADR-XXX | ... |

_(Omit this section if there are no related epics to create.)_

## Open Questions

- [ ] Unresolved question (carry forward any unresolved TBDs from above)

## Related Docs

Links to the `_doc_*.md` files consulted during drafting.
