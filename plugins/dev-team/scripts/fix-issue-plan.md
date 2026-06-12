```mermaid
stateDiagram-v2
    [*] --> init
    init --> debugging : start
    debugging --> researching : debug_done
    debugging --> failed : reproduction_failed
    researching --> implementing : research_done
    implementing --> validating : impl_done
    validating --> fixing : build_failed
    validating --> fixing : tests_failed
    validating --> creating-pr : clean
    creating-pr --> reviewing : pr_created
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

The `debugging` state runs `debugger-investigate` against the GitHub issue before the
researcher sees it. It reproduces the reported behaviour, investigates the root cause,
and commits a failing repro test and any diagnostic logging to the feature branch. Its
output — a structured root-cause report — is passed to the researcher as additional
context when writing the task brief. If the bug cannot be reproduced the pipeline fails
immediately with `reproduction_failed`.

The `signoff` state runs three tasks in parallel before making its decision:

1. **`reviewer-sign-off`** — checks that all PR review threads have been resolved and scans
   modified files for new code quality issues (Priority 1–4). Resolves satisfied threads;
   leaves unresolved threads where the developer disagreed and the reviewer is pushing back.
2. **`researcher-validate`** — checks each exit criterion proposed by the researcher against
   the actual code and tests. Any `fail` or `partial` result counts as a failure.
3. **Script validation** — runs `validate-build` then (if clean) `validate-tests`.

All three must pass for `signoff` to emit `approved`. Any failure from any task emits
`changes_requested` and routes back to `fixing-pr`, with accumulated failure details.
