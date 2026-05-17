# -*- coding: utf-8 -*-
"""Command-line updater for cloud cron jobs."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import lottery_auto_update_full as core  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run lottery updater without opening a browser.")
    parser.add_argument("--mode", choices=["daily", "weekly", "full"], default="weekly")
    parser.add_argument("--daily", action="store_true", help="Shortcut for --mode daily")
    parser.add_argument("--weekly", action="store_true", help="Shortcut for --mode weekly")
    parser.add_argument("--full", action="store_true", help="Shortcut for --mode full")
    args = parser.parse_args()
    mode = args.mode
    if args.daily:
        mode = "daily"
    if args.weekly:
        mode = "weekly"
    if args.full:
        mode = "full"
    return core.main(mode=mode, open_dashboard=False)


if __name__ == "__main__":
    raise SystemExit(main())
