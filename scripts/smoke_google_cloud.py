#!/usr/bin/env python3
"""Smoke test for Google Cloud / Vertex AI provider.

Usage:
    python scripts/smoke_google_cloud.py --dry-run
    python scripts/smoke_google_cloud.py --live --project my-project
"""

import argparse
import sys
from pathlib import Path

# Ensure the project root is on the path
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))


def main() -> int:
    parser = argparse.ArgumentParser(description="Google Cloud provider smoke test")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Run all offline validations (default)")
    parser.add_argument("--live", action="store_true",
                        help="Attempt a live API call (requires credentials)")
    parser.add_argument("--project", type=str, default=None,
                        help="Override Google Cloud project for live test")
    args = parser.parse_args()

    from aura.providers.google_cloud.smoke import dry_run_all, live_test

    if args.live:
        failures = live_test(project=args.project)
    else:
        failures = dry_run_all()

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  FAIL: {f}")
        return 1

    mode = "live" if args.live else "dry-run"
    print(f"All {mode} validations passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
