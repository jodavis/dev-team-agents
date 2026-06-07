---
description: Review a spec file for implementation readiness — surface blocking questions an implementer would need resolved before writing code
argument-hint: <path to _spec_*.md file>
user-invocable: false
---

## Inputs

Spec file path: `$ARGUMENTS`

If not provided, stop and tell the caller:

> Usage: `/researcher-spec-review <path/to/_spec_Feature.md>`

---

## Steps

### 1 — Read the spec

Read the spec file at the provided path in full — every section.

### 2 — Read architecture docs

Based on the spec's "Related Docs" section and the areas it describes, identify which
`_doc_*.md` files are relevant. Always read `src/_doc_Projects.md`. Discover all
available docs with:

```bash
find . -name "_doc_*.md" -not -path "./.git/*" | sort
```

### 3 — Read relevant source

Read the source files and interfaces the spec's planned implementation references or
depends on. Use Glob and Grep to locate them.

### 4 — Research external resources (if needed)

If the spec touches patterns, frameworks, or APIs not fully covered by local docs, use
WebSearch, WebFetch, or other available tools to look up best practices that would inform
the implementation.

### 5 — Assess and return

From the perspective of an implementer with only the spec and the codebase — no
conversation history, no prior context:

- Can every item be implemented without guessing?
- Can unit tests and Gherkin scenarios be written without guessing expected behavior?
- Are there missing decisions, ambiguous behavior, unspecified error cases, or unclear
  interfaces?

If no blocking gaps exist, return exactly:

> No blocking questions — spec is implementation-ready.

If gaps exist, return:
- A numbered list of concrete questions. Each must be specific enough to resolve the gap,
  reference the section or concept it pertains to, and be a genuine blocker — not a
  suggestion or style preference. Apply this test: if a reasonable implementer could make
  a sensible default choice without consulting anyone, it is not a blocker. Only include
  questions where the implementer would be forced to guess at intended behavior.
- Under a `## Useful resources` heading: any external resources found in step 4 that
  would inform implementation.

Do not include anything else — no summary, no recommendations, no file quotes.
