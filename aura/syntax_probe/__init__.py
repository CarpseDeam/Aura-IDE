from __future__ import annotations

from aura.syntax_probe.c_probe import CSyntaxProbe  # noqa: F401 — triggers self-registration
from aura.syntax_probe.cpp_probe import CppSyntaxProbe  # noqa: F401 — triggers self-registration
from aura.syntax_probe.csharp_probe import CSharpSyntaxProbe  # noqa: F401 — triggers self-registration
from aura.syntax_probe.css_probe import CSSSyntaxProbe  # noqa: F401 — triggers self-registration
from aura.syntax_probe.go_probe import GoSyntaxProbe  # noqa: F401 — triggers self-registration
from aura.syntax_probe.html_probe import HTMLSyntaxProbe  # noqa: F401 — triggers self-registration
from aura.syntax_probe.java_probe import JavaSyntaxProbe  # noqa: F401 — triggers self-registration
from aura.syntax_probe.javascript_probe import JavaScriptSyntaxProbe  # noqa: F401 — triggers self-registration
from aura.syntax_probe.json_probe import JSONSyntaxProbe  # noqa: F401 — triggers self-registration
from aura.syntax_probe.models import SyntaxProbeResult
from aura.syntax_probe.powershell_probe import PowerShellSyntaxProbe  # noqa: F401 — triggers self-registration
from aura.syntax_probe.protocol import SyntaxProbe
from aura.syntax_probe.python_probe import PythonSyntaxProbe  # noqa: F401 — triggers self-registration
from aura.syntax_probe.registry import get_probe, iter_probes, register_probe
from aura.syntax_probe.rust_probe import RustSyntaxProbe  # noqa: F401 — triggers self-registration
from aura.syntax_probe.shell_probe import ShellSyntaxProbe  # noqa: F401 — triggers self-registration
from aura.syntax_probe.sql_probe import SQLSyntaxProbe  # noqa: F401 — triggers self-registration
from aura.syntax_probe.toml_probe import TOMLSyntaxProbe  # noqa: F401 — triggers self-registration
from aura.syntax_probe.gdscript_probe import GDScriptSyntaxProbe  # noqa: F401 — triggers self-registration
from aura.syntax_probe.typescript_probe import TypeScriptSyntaxProbe  # noqa: F401 — triggers self-registration
from aura.syntax_probe.yaml_probe import YAMLSyntaxProbe  # noqa: F401 — triggers self-registration

__all__ = [
    "SyntaxProbeResult",
    "SyntaxProbe",
    "register_probe",
    "iter_probes",
    "get_probe",
    "JavaScriptSyntaxProbe",
    "TypeScriptSyntaxProbe",
    "PythonSyntaxProbe",
    "RustSyntaxProbe",
    "JSONSyntaxProbe",
    "TOMLSyntaxProbe",
    "YAMLSyntaxProbe",
    "GoSyntaxProbe",
    "GDScriptSyntaxProbe",
    "HTMLSyntaxProbe",
    "JavaSyntaxProbe",
    "CSharpSyntaxProbe",
    "CSyntaxProbe",
    "CppSyntaxProbe",
    "CSSSyntaxProbe",
    "ShellSyntaxProbe",
    "SQLSyntaxProbe",
    "PowerShellSyntaxProbe",
]
