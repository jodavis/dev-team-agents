#!/usr/bin/env bash
# get-context-path.sh — Deterministically resolve the dev-team context file path.
#
# Usage: get-context-path.sh <work-item-id>
#
# Derives the repo slug from `git remote get-url origin`, then delegates to
# dev_team.py --print-context-path to compute the canonical state-file path.
# Exits non-zero and prints a clear message to stderr on failure.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- Argument check ---------------------------------------------------------
if [[ $# -lt 1 ]]; then
    echo "Usage: $(basename "$0") <work-item-id>" >&2
    exit 1
fi
work_item_id="$1"

# ---- Resolve repo slug from git remote --------------------------------------
# GIT_REMOTE_URL_OVERRIDE is a test seam; it is never set in production.
if [[ -n "${GIT_REMOTE_URL_OVERRIDE:-}" ]]; then
    remote_url="$GIT_REMOTE_URL_OVERRIDE"
elif ! remote_url=$(git remote get-url origin 2>&1); then
    echo "Error: could not read git remote 'origin': $remote_url" >&2
    exit 1
fi

# Extract org/repo, handling four common URL forms:
#   https://github.com/org/repo.git
#   https://github.com/org/repo
#   git@github.com:org/repo.git
#   git@github.com:org/repo
repo_slug=$(
    echo "$remote_url" \
    | sed -E \
        -e 's|^https?://[^/]+/||' \
        -e 's|^[^@]+@[^:]+:||' \
        -e 's|\.git$||'
)

if [[ -z "$repo_slug" ]]; then
    echo "Error: could not extract repo slug from remote URL: $remote_url" >&2
    exit 1
fi

# ---- Delegate to dev_team.py ------------------------------------------------
python "${SCRIPT_DIR}/dev_team.py" "$work_item_id" --print-context-path "$repo_slug"
