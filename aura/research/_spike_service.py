"""Standalone spike proving research_current_info returns grounded structured results.

Usage:
    python -m aura.research._spike_service "latest deepseek model release"

Exits 0 on both success and graceful degradation.
"""

from __future__ import annotations

import sys


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "latest deepseek model release"
    from aura.research.service import research_current_info

    result = research_current_info(query)
    print(f"ok: {result.ok}")
    print(f"sources: {len(result.sources)}")
    for s in result.sources:
        print(f"  {s.title}: {s.url}")
    print(f"evidence: {len(result.evidence)}")
    if result.evidence:
        first = result.evidence[0]
        print(f"  first evidence text ({len(first.text)} chars): {first.text[:400]}")
    if result.notes:
        print(f"notes: {result.notes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
