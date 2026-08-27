"""Thin entry point for the Aura Windows build.

The implementation lives in ``scripts/aura_build/``. The repository root is put
on ``sys.path`` first so direct execution and test imports resolve the very same
``scripts.aura_build.*`` modules rather than duplicate module identities.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.aura_build.cli import main

if __name__ == "__main__":
    main()
