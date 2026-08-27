"""Aura Windows build tooling.

Split out of the former ``scripts/build_nuitka.py`` god-file. Each module owns
one concern:

* ``scripts.aura_build.config``      - shared build constants
* ``scripts.aura_build.environment`` - version resolution, build venv + fingerprint
* ``scripts.aura_build.assets``      - dist normalization and post-build asset preparation
* ``scripts.aura_build.release``     - Inno Setup installer and GitHub release helpers
* ``scripts.aura_build.cli``         - Nuitka command, phase timing, orchestration, CLI

``scripts/build_nuitka.py`` stays a thin executable entry point onto
``scripts.aura_build.cli.main``.
"""

from __future__ import annotations
