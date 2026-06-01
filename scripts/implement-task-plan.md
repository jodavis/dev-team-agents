```mermaid
stateDiagram-v2
    [*] --> init
    init --> spec-finding : start
    spec-finding --> researching : spec_found
    researching --> implementing : research_done
    implementing --> validating : impl_done
    validating --> fixing : build_failed
    validating --> fixing : tests_failed
    validating --> reviewing : clean
    reviewing --> done : approved
    reviewing --> fixing-pr : changes_requested
    fixing-pr --> signoff : fix_done
    signoff --> done : approved
    signoff --> fixing-pr : changes_requested
    fixing --> validating : fix_done
    fixing --> failed : max_retries
    fixing-pr --> failed : max_retries
    done --> [*]
    failed --> [*]
```

The `signoff` state runs three tasks in parallel before making its decision:

1. **`reviewer-sign-off`** — checks that all PR review threads have been resolved and scans
   modified files for new code quality issues (Priority 1–4). Resolves satisfied threads;
   leaves unresolved threads where the developer disagreed and the reviewer is pushing back.
2. **`researcher-validate`** — checks each exit criterion from the spec against the
   actual code and tests. Any `fail` or `partial` result counts as a failure.
3. **Script validation** — runs `validate-build` then (if clean) `validate-tests`.

All three must pass for `signoff` to emit `approved`. Any failure from any task emits
`changes_requested` and routes back to `fixing-pr`, with accumulated failure details.
