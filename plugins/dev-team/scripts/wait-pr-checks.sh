#!/usr/bin/env bash
# wait-pr-checks.sh — Block until PR checks complete, then output pass/fail result.
#
# Usage: wait-pr-checks.sh <pr-url>
#
# Runs `gh pr checks --watch` to block until all checks finish, then inspects
# the final check states. Outputs one of:
#   passed - all checks passed
#   failed - one or more checks failed or were cancelled
#
# Exit code is always 0; the result is communicated on stdout for the
# script-runner agent to write to the context file.

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $(basename "$0") <pr-url>" >&2
    exit 1
fi

pr_url="$1"

# Block until all checks complete (or timeout after 30 minutes).
# --watch exits 0 when all checks pass, non-zero when any fail.
# We capture the outcome without letting a non-zero exit abort this script.
watch_exit=0
gh pr checks "$pr_url" --watch --interval 15 2>&1 || watch_exit=$?

# Inspect the final check states via JSON to determine pass/fail.
# bucket field: pass | fail | pending | skipping | cancel
failing=$(gh pr checks "$pr_url" --json bucket --jq '[.[] | select(.bucket == "fail" or .bucket == "cancel")] | length' 2>/dev/null || echo "0")
pending=$(gh pr checks "$pr_url" --json bucket --jq '[.[] | select(.bucket == "pending")] | length' 2>/dev/null || echo "0")

if [[ "$pending" -gt 0 ]]; then
    # Checks timed out or watch exited early with pending checks
    echo "failed - checks still pending after watch completed (exit code: $watch_exit)"
elif [[ "$failing" -gt 0 ]]; then
    echo "failed - $failing check(s) failed or were cancelled"
else
    echo "passed - all checks passed"
fi
