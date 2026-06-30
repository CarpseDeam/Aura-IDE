"""Adapters bridging the synchronous ConversationManager to Qt signals."""
from aura.bridge.dispatch import _DispatchProxy
from aura.bridge.dispatch_session import install_dispatch_session_bridge
from aura.bridge.lap_result import LapResult
from aura.bridge.qt_bridge import ConversationBridge

install_dispatch_session_bridge(_DispatchProxy)

__all__ = ["ConversationBridge", "LapResult"]
