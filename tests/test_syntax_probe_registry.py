from __future__ import annotations

from pathlib import Path

from aura.syntax_probe.protocol import SyntaxProbe
from aura.syntax_probe.python_probe import PythonSyntaxProbe
from aura.syntax_probe.registry import REGISTRY, get_probe, register_probe


class TestRegistry:
    """Tests for the syntax-probe registry."""

    def test_get_probe_py_returns_python_probe(self) -> None:
        probe = get_probe("foo.py")
        assert probe is PythonSyntaxProbe

    def test_get_probe_rs_returns_none(self) -> None:
        probe = get_probe("foo.rs")
        assert probe is None

    def test_last_registered_wins(self) -> None:
        class FakePyProbe(SyntaxProbe):
            language_id = "fake"

            @staticmethod
            def detect(file_path: str | Path) -> bool:
                return str(file_path).endswith(".py")

            def check(self, workspace_root, file_path):
                from aura.syntax_probe.models import SyntaxProbeResult
                return SyntaxProbeResult(
                    path=str(file_path),
                    language_id=self.language_id,
                    evidence="no_evidence",
                )

        register_probe(FakePyProbe)
        try:
            probe = get_probe("bar.py")
            assert probe is FakePyProbe, f"Expected FakePyProbe, got {probe}"
        finally:
            # Clean up so other tests are not affected
            REGISTRY.remove(FakePyProbe)
