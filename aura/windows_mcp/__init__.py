"""Windows Computer Use — the sbroenne/mcp-windows server, on Aura's terms.

Three concerns, one per module, and no parallel registry or manager anywhere:

* :mod:`aura.windows_mcp.allowlist` decides which of the server's tools Aura is
  willing to expose at all, and what each one's effect is.
* :mod:`aura.windows_mcp.install` obtains the executable — a user's own command,
  a usable existing install, or a checksum-verified official release.
* :mod:`aura.windows_mcp.manager` owns the lifecycle and drives the *existing*
  :class:`~aura.conversation.tools.registry.ToolRegistry` MCP seam.
"""
from __future__ import annotations

from aura.windows_mcp.allowlist import (
    WINDOWS_MCP_ALLOWLIST,
    WINDOWS_MCP_CAPABILITY,
    WINDOWS_MCP_DENYLIST_REASONS,
    filter_windows_tool_defs,
)
from aura.windows_mcp.install import (
    WindowsMcpInstallError,
    installed_server_path,
    resolve_server_command,
)
from aura.windows_mcp.manager import (
    WindowsComputerUseManager,
    WindowsComputerUseStatus,
)

__all__ = [
    "WINDOWS_MCP_ALLOWLIST",
    "WINDOWS_MCP_CAPABILITY",
    "WINDOWS_MCP_DENYLIST_REASONS",
    "WindowsComputerUseManager",
    "WindowsComputerUseStatus",
    "WindowsMcpInstallError",
    "filter_windows_tool_defs",
    "installed_server_path",
    "resolve_server_command",
]
