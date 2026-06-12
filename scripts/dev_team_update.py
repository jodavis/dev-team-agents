"""
dev_team_update.py — SessionStart auto-update script for the dev-team Claude Code plugin.

Usage:
    python dev_team_update.py --data-dir <path> --threshold-hours <n>

Reads <data-dir>/last_update (ISO 8601 timestamp). If the file is absent or older
than --threshold-hours, runs `git pull --ff-only --quiet` in the plugin root.
On success, writes the current ISO 8601 timestamp to <data-dir>/last_update.
Always exits 0 — failures are logged to stderr only and must not block session start.
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Dev-team plugin auto-update hook")
    parser.add_argument("--data-dir", required=True, help="Plugin data directory path")
    parser.add_argument(
        "--threshold-hours",
        type=float,
        default=4,
        help="Hours between auto-update checks (default: 4)",
    )
    args = parser.parse_args()

    try:
        data_dir = Path(args.data_dir)
        threshold = timedelta(hours=args.threshold_hours)
        last_update_file = data_dir / "last_update"

        # Determine whether an update is needed
        needs_update = True
        if last_update_file.exists():
            try:
                raw = last_update_file.read_text(encoding="utf-8").strip()
                last_update = datetime.fromisoformat(raw)
                # Ensure timezone-aware comparison
                if last_update.tzinfo is None:
                    last_update = last_update.replace(tzinfo=timezone.utc)
                now = datetime.now(tz=timezone.utc)
                if (now - last_update) < threshold:
                    needs_update = False
            except Exception as exc:
                print(
                    f"[dev_team_update] Warning: could not read last_update file: {exc}",
                    file=sys.stderr,
                )

        if not needs_update:
            return

        # Resolve plugin root: prefer env var, fall back to two levels above this script
        plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
        if plugin_root:
            plugin_root_path = Path(plugin_root)
        else:
            plugin_root_path = Path(__file__).parent.parent

        # Run git pull --ff-only
        result = subprocess.run(
            ["git", "-C", str(plugin_root_path), "pull", "--ff-only", "--quiet"],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            print(
                f"[dev_team_update] git pull failed (exit {result.returncode}): {result.stderr.strip()}",
                file=sys.stderr,
            )
            return

        # Write updated timestamp
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            now_iso = datetime.now(tz=timezone.utc).isoformat()
            last_update_file.write_text(now_iso + "\n", encoding="utf-8")
        except Exception as exc:
            print(
                f"[dev_team_update] Warning: could not write last_update file: {exc}",
                file=sys.stderr,
            )

    except Exception as exc:
        # Catch-all: never block session start
        print(f"[dev_team_update] Unexpected error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
    sys.exit(0)
