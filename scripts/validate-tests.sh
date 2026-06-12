#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_SCRIPTS="$SCRIPT_DIR/../plugins/dev-team/scripts"
PYTHONPATH="$PLUGIN_SCRIPTS${PYTHONPATH:+:$PYTHONPATH}" python -m pytest "$PLUGIN_SCRIPTS" -v