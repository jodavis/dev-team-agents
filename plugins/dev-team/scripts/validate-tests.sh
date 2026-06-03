#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHONPATH="$SCRIPT_DIR:$PYTHONPATH" python -m pytest "$SCRIPT_DIR/test_dev_team.py" -v
exit $?
