from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QStatusBar

from aura.config import PROVIDERS, cost_usd, ThinkingMode

_THINKING_LABEL = {"off": "Off", "high": "High", "max": "Max"}

class AuraStatusBar(QStatusBar):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        
        # Left side: workspace, model, thinking
        self._status_left = QLabel("")
        self.addWidget(self._status_left, 1)
        
        # Right side: tokens, cost
        self._status_tokens = QLabel("0 hit · 0 miss · 0 out")
        self.addPermanentWidget(self._status_tokens)

        self._status_cost = QLabel("$0.0000")
        self._status_cost.setObjectName("statusCost")
        self.addPermanentWidget(self._status_cost)

        # Monospace for numbers
        mono_font = QFont("Geist Mono, JetBrains Mono, Consolas, monospace")
        mono_font.setStyleHint(QFont.StyleHint.Monospace)
        mono_font.setPointSize(11)
        self._status_tokens.setFont(mono_font)
        self._status_cost.setFont(mono_font)

    def refresh(
        self, 
        workspace_root: str, 
        model_id: str, 
        thinking: ThinkingMode,
        session_usage: dict[str, dict[str, int]]
    ) -> None:
        # Workspace path truncation
        ws = workspace_root
        if len(ws) > 64:
            ws = "…" + ws[-63:]
            
        # Model label lookup
        model_label = model_id
        for cfg in PROVIDERS.values():
            if model_id in cfg.models:
                model_label = cfg.models[model_id].label
                break
                
        thinking_label = _THINKING_LABEL.get(thinking, "Off")
        self._status_left.setText(f"{ws}    ·    {model_label}    ·    Thinking: {thinking_label}")

        # Usage and Cost
        total_hit = sum(u["hit"] for u in session_usage.values())
        total_miss = sum(u["miss"] for u in session_usage.values())
        total_out = sum(u["out"] for u in session_usage.values())
        
        known_cost = 0.0
        unknown_count = 0
        for m_id, u in session_usage.items():
            c = cost_usd(m_id, u["hit"], u["miss"], u["out"])
            if c is None:
                unknown_count += 1
            else:
                known_cost += c

        self._status_tokens.setText(
            f"{total_hit:,} hit · {total_miss:,} miss · {total_out:,} out"
        )

        total_models = len(session_usage)
        if total_models == 0:
            self._status_cost.setText("$0.0000")
            self._status_cost.setToolTip("")
        elif unknown_count == total_models:
            self._status_cost.setText("$?.????")
            self._status_cost.setToolTip("")
        elif unknown_count > 0:
            self._status_cost.setText(f"${known_cost:.4f}*")
            self._status_cost.setToolTip(
                "Some models have unknown pricing — actual cost may be higher."
            )
        else:
            self._status_cost.setText(f"${known_cost:.4f}")
            self._status_cost.setToolTip("")
