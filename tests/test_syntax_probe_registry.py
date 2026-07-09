from __future__ import annotations

from pathlib import Path

from aura.syntax_probe.protocol import SyntaxProbe
from aura.syntax_probe.python_probe import PythonSyntaxProbe
from aura.syntax_probe.rust_probe import RustSyntaxProbe
from aura.syntax_probe.json_probe import JSONSyntaxProbe
from aura.syntax_probe.toml_probe import TOMLSyntaxProbe
from aura.syntax_probe.yaml_probe import YAMLSyntaxProbe
from aura.syntax_probe.registry import REGISTRY, get_probe, register_probe


class TestRegistry:
    """Tests for the syntax-probe registry."""

    def test_get_probe_py_returns_python_probe(self) -> None:
        probe = get_probe("foo.py")
        assert probe is PythonSyntaxProbe

    def test_get_probe_rs_returns_rust_probe(self) -> None:
        probe = get_probe("foo.rs")
        assert probe is RustSyntaxProbe

    def test_get_probe_json_returns_json_probe(self) -> None:
        probe = get_probe("foo.json")
        assert probe is JSONSyntaxProbe

    def test_get_probe_toml_returns_toml_probe(self) -> None:
        probe = get_probe("foo.toml")
        assert probe is TOMLSyntaxProbe

    def test_get_probe_yaml_returns_yaml_probe(self) -> None:
        probe = get_probe("foo.yaml")
        assert probe is YAMLSyntaxProbe

    def test_get_probe_yml_returns_yaml_probe(self) -> None:
        probe = get_probe("foo.yml")
        assert probe is YAMLSyntaxProbe

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
