#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/validate-build.sh"
"$SCRIPT_DIR/validate-tests.sh"
