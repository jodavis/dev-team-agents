---
description: Sign-off review for the AdaptiveRemote dev-team pipeline. After the developer has addressed review comments, checks whether each previously requested change has been resolved and scans modified files for new issues. Outputs a JSON result indicating approved or changes_requested.
user-invocable: false
---

You are performing a sign-off review for work item $WORK_ITEM_ID.

**Task brief:**
$TASK_BRIEF

**PR URL:** $PR_URL

---

## Step 1 — Load guidelines

Read `CONTRIBUTING.md` in full. These are the standards against which you are reviewing.

## Step 2 — Note recent changes

The pipeline pushes the latest commits before invoking this sign-off. Check
`git log --oneline -5` to understand what has changed since the previous review pass.

## Step 3 — Retrieve prior review threads

Extract the pull number from `$PR_URL` (last path segment). Parse `owner` and `repo` from
the URL (e.g. `https://github.com/owner/repo/pull/123`).

Fetch all review threads using the GitHub MCP:

```
mcp__plugin_github_github__pull_request_read(method="get_review_comments", owner=<owner>, repo=<repo>, pullNumber=<number>)
```

Each thread has:
- `id` — the node ID (`PRRT_...`) needed to resolve the thread
- `isResolved` — whether the thread is already resolved
- `comments.nodes[0]` — the first comment: its `body`, `path`, and numeric `id`

For each thread where `isResolved` is `false`, note:
- What was the original issue (from the first comment body)?
- What file was it on (from the first comment path)?
- Has that file been modified since the comment was posted?

## Step 4 — Create a pending review

Create a pending review before checking threads, so any replies or new inline comments
can be added to it as they are written:

```
mcp__plugin_github_github__pull_request_review_write(method="create", owner=<owner>, repo=<repo>, pullNumber=<number>)
```

## Step 5 — Check each prior thread for resolution

For each unresolved review comment:

1. Read the relevant section of the latest code and any developer replies in the thread.
2. Determine the outcome:
   - **Addressed satisfactorily:** the problem no longer exists in the code. Resolve the
     thread using the GitHub MCP:
     ```
     mcp__plugin_github_github__pull_request_review_write(method="resolve_thread", owner=<owner>, repo=<repo>, pullNumber=<number>, threadId=<PRRT_... node ID>)
     ```
   - **Developer disagreed (posted rationale):** read the developer's rationale.
     - **Accept the rationale:** add a reply acknowledging it, then resolve the thread.
     - **Reject the rationale:** add a reply restating the requirement and explaining why
       the rationale doesn't address the concern. Leave the thread unresolved.
   - **Partially addressed:** the developer made a change but the underlying problem
     remains or a different instance was missed. Add a follow-up comment explaining what
     still needs to be done. Leave the thread unresolved.
   - **Not addressed:** the code is unchanged and no rationale was posted. Add a follow-up
     comment restating what is needed and why. Leave the thread unresolved.

   To reply to a thread, use the numeric comment ID of the thread's first comment:
   ```
   mcp__plugin_github_github__add_reply_to_pull_request_comment(owner=<owner>, repo=<repo>, pullNumber=<number>, commentId=<numeric id>, body=<reply>)
   ```

## Step 6 — Scan modified files for new issues

Identify all files that were modified since the last review push (use the PR diff or git
log). Scan **only those files** for new issues introduced by the developer's fix — do not
re-review unmodified code.

Apply the same priority order as the first-pass review:
1. Correctness/fault tolerance, 2. Security, 3. Performance,
4. Documentation, 5. Code style (note only)

For each new Priority 1–4 issue found, post it immediately to the pending review:

```
mcp__plugin_github_github__add_comment_to_pending_review(owner=<owner>, repo=<repo>, pullNumber=<number>, path=<file>, line=<line>, side="RIGHT", subjectType="LINE", body=<comment>)
```

## Step 7 — Submit the sign-off review

**Sign-off decision:** Set `approved` if all review threads are resolved (no unresolved
threads remain) AND no new Priority 1–4 issues were found in the modified files.
Set `changes_requested` if any threads remain unresolved or if new blocking issues were found.

Submit the pending review. Use `event: COMMENT` — do not use `APPROVE` or
`REQUEST_CHANGES`, as GitHub rejects those when the reviewer and PR author share the same account.

```
mcp__plugin_github_github__pull_request_review_write(method="submit_pending", owner=<owner>, repo=<repo>, pullNumber=<number>, body=<overall summary>, event="COMMENT")
```

## Step 7a — Hand off to human reviewer (approved only)

If the review outcome is **approved**, do the following before writing the output summary:

1. Convert the PR from draft to Ready for Review:
   ```
   mcp__plugin_github_github__update_pull_request(owner=<owner>, repo=<repo>, pullNumber=<number>, draft=false)
   ```
2. Call `mcp__jira__lookupJiraAccountId` with `$REVIEW_ASSIGNEE_EMAIL` to get the human reviewer's account ID.
3. Assign the Jira issue to that account with `mcp__jira__editJiraIssue`.
4. Request a GitHub review from the assignee:
   ```
   mcp__plugin_github_github__update_pull_request(owner=<owner>, repo=<repo>, pullNumber=<number>, reviewers=["<github-username>"])
   ```
5. Add a brief Jira comment with `mcp__jira__addCommentToJiraIssue`: "PR ready for human review — reviewer requested on GitHub."

## Step 8 — Output

Write a concise summary:
- List each prior comment and whether it was resolved or still needs work
- List any new issues found in the modified files

Then output the following JSON object as the very last line of your response.
Write it as a bare JSON object — do not wrap it in a code block or add any text after it:

{"status": "approved|changes_requested"}
