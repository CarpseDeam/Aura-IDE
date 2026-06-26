"""Standalone behavioral proof for PlaywrightResearcher.

Usage::

    python -m aura.research._spike_research "your query"

Exits 0 on both success and graceful degradation.
"""

from __future__ import annotations

import sys
from typing import Any


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "playwright mcp python"

    # Late import to avoid circularity at package level
    from aura.research.playwright import PlaywrightResearcher

    researcher: Any = PlaywrightResearcher()

    if not researcher.start():
        print(f"Researcher unavailable: {researcher._unavailable_reason}")
        return 0

    try:
        sources = researcher.search(query)
        print(f"Found {len(sources)} sources")
        for s in sources[:3]:
            print(f"  - {s.title}: {s.url}")

        if sources:
            page = researcher.open(sources[0].url)
            print(f"Opened: {page.title}")
            print(page.clean_text[:500])
    finally:
        researcher.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
