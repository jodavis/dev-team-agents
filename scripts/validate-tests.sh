#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_SCRIPTS="$SCRIPT_DIR/../plugins/dev-team/scripts"
PYTHONPATH="$PLUGIN_SCRIPTS:$PYTHONPATH" python -m pytest "$PLUGIN_SCRIPTS/test_dev_team.py" -v
exit $?
