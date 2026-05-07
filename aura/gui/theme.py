"""Dark theme — color palette and global stylesheet."""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


# ---- color tokens ---------------------------------------------------------
BG = "#1e1e1e"           # window background (was #181a20)
BG_ALT = "#25252d"       # panels / generic cards (was #1f222a)
BG_RAISED = "#2a2a33"    # input field, pressed buttons (was #262a33)
BORDER = "#2e333d"
BORDER_STRONG = "#3a4050"

# Message-card backgrounds — distinct so user/assistant turns separate at a glance.
BG_USER_CARD = "#1e2536"        # cool blue tint (user — feels like input)
BG_ASSISTANT_CARD = "#1f1f23"   # warm neutral (assistant — feels like output)
BG_TOOL_CARD = "#1c2026"        # supporting info, slightly recessed

FG = "#eaecef"           # primary text — bumped for chat readability
FG_BODY_USER = "#dde0e6"  # user message body
FG_DIM = "#a8aebb"        # secondary text (role labels, meta)
FG_MUTED = "#6e7382"      # tertiary / placeholder
FG_ITALIC = "#7e8494"     # reasoning text

ACCENT = "#7aa2f7"         # primary accent (links, selected, user-card edge)
ACCENT_HOVER = "#94b6ff"
SUCCESS = "#9ece6a"        # diff additions, ok
SUCCESS_DIM = "#82a35a"    # tool-card supporting state (desaturated)
DANGER = "#f7768e"         # diff removals, errors, rejection
WARN = "#e0af68"           # warning, read-only badge

# Terminal card background
TERMINAL_BG = "#0a0a0a"

# Diff hunk backgrounds (low-saturation)
DIFF_ADD_BG = "#1d2a1f"
DIFF_DEL_BG = "#2a1d20"


def apply_theme(app: QApplication) -> None:
    """Apply Fusion + dark palette + global stylesheet."""
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(FG))
    palette.setColor(QPalette.ColorRole.Base, QColor(BG_ALT))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(BG_RAISED))
    palette.setColor(QPalette.ColorRole.Text, QColor(FG))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(FG_MUTED))
    palette.setColor(QPalette.ColorRole.Button, QColor(BG_ALT))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(FG))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(BG))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(BG_RAISED))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(FG))
    palette.setColor(QPalette.ColorRole.Link, QColor(ACCENT))
    app.setPalette(palette)

    app.setStyleSheet(QSS)


QSS = f"""
* {{
    color: {FG};
    font-family: "Segoe UI", "Inter", system-ui, sans-serif;
    font-size: 13px;
}}

QMainWindow, QWidget {{
    background: {BG};
}}

QSplitter::handle {{
    background: {BORDER};
    width: 3px;
    height: 3px;
}}

QToolBar {{
    background: {BG_ALT};
    border-bottom: 1px solid {BORDER};
    padding: 4px 6px;
    spacing: 6px;
}}

QToolBar QToolButton {{
    background: transparent;
    color: {FG};
    border: 1px solid transparent;
    border-radius: 5px;
    padding: 4px 10px;
}}
QToolBar QToolButton:hover {{
    background: {BG_RAISED};
    border-color: {BORDER};
}}
QToolBar QToolButton:checked {{
    background: {BG_RAISED};
    border-color: {BORDER_STRONG};
    color: {FG};
}}
QToolBar QLabel {{
    color: {FG_DIM};
    padding: 0 4px;
}}

QPushButton {{
    background: {BG_RAISED};
    color: {FG};
    border: 1px solid {BORDER_STRONG};
    border-radius: 5px;
    padding: 5px 12px;
}}
QPushButton:hover {{
    background: {BORDER_STRONG};
}}
QPushButton:disabled {{
    color: {FG_MUTED};
    background: {BG_ALT};
}}
QPushButton#primary {{
    background: {ACCENT};
    color: {BG};
    border-color: {ACCENT};
    font-weight: 600;
}}
QPushButton#primary:hover {{
    background: {ACCENT_HOVER};
}}
QPushButton#danger {{
    background: transparent;
    color: {DANGER};
    border-color: {DANGER};
}}
QPushButton#danger:hover {{
    background: rgba(247, 118, 142, 0.10);
}}
QPushButton#success {{
    background: {SUCCESS};
    color: {BG};
    border-color: {SUCCESS};
    font-weight: 600;
}}
QPushButton#success:hover {{
    background: #b3e088;
}}

QComboBox, QLineEdit, QTextEdit, QPlainTextEdit {{
    background: {BG_RAISED};
    color: {FG};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
    selection-color: {BG};
}}
QTextEdit, QPlainTextEdit {{
    padding: 6px 8px;
}}
QTextEdit:focus, QPlainTextEdit:focus {{
    border: 1px solid {ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    width: 18px;
}}
QComboBox QAbstractItemView {{
    background: {BG_ALT};
    border: 1px solid {BORDER_STRONG};
    selection-background-color: {ACCENT};
    selection-color: {BG};
}}

QScrollArea {{
    background: {BG};
    border: none;
}}
QScrollBar:vertical {{
    background: {BG};
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER_STRONG};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: #4a5263;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: {BG};
    height: 10px;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER_STRONG};
    border-radius: 4px;
    min-width: 30px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

QCheckBox {{
    color: {FG};
    spacing: 6px;
}}
QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {BORDER_STRONG};
    border-radius: 3px;
    background: {BG_RAISED};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}

QLabel#paneTitle {{
    color: {FG_DIM};
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 4px 8px;
}}

QLabel#workspaceLabel {{
    color: {FG};
    padding: 4px 8px;
}}

QLabel#workspaceHint {{
    color: {FG_MUTED};
    padding: 4px 8px;
    font-size: 12px;
}}

QFrame#leftPane {{
    background: {BG_ALT};
    border-right: 1px solid {BORDER};
}}

QFrame#card {{
    background: {BG_ALT};
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 8px;
}}
QFrame#userCard {{
    background: {BG_USER_CARD};
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-left: 3px solid {ACCENT};
    border-radius: 8px;
}}
QFrame#assistantCard {{
    background: {BG_ASSISTANT_CARD};
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 8px;
}}
QFrame#toolCard {{
    background: {BG_TOOL_CARD};
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 6px;
}}
QFrame#toolCluster {{
    background: transparent;
    border: none;
    border-left: 1px solid {BORDER_STRONG};
}}
QFrame#errorCard {{
    background: rgba(247, 118, 142, 0.08);
    border: 1px solid {DANGER};
    border-radius: 8px;
}}

QLabel#cardHeader {{
    color: {FG_DIM};
    font-weight: 600;
    font-size: 12px;
    padding: 0 0 4px 0;
}}
QLabel#userHeader {{
    color: {ACCENT};
    font-weight: 700;
    font-size: 12px;
    padding: 0 0 4px 0;
    letter-spacing: 0.02em;
}}
QLabel#assistantHeader {{
    color: {FG_DIM};
    font-weight: 600;
    font-size: 12px;
    padding: 0 0 4px 0;
    letter-spacing: 0.02em;
}}

QLabel#reasoning {{
    color: {FG_ITALIC};
    font-style: italic;
    background: transparent;
}}

QToolButton#sectionToggle {{
    background: transparent;
    color: {FG_DIM};
    border: none;
    text-align: left;
    padding: 0;
}}
QToolButton#sectionToggle:hover {{
    color: {FG};
}}

QToolButton#reasoningToggle {{
    background: transparent;
    color: {FG_DIM};
    border: 1px solid transparent;
    border-radius: 4px;
    text-align: left;
    padding: 2px 6px;
    font-weight: 600;
    font-size: 12px;
}}
QToolButton#reasoningToggle:hover {{
    background: {BG_RAISED};
    border-color: {BORDER};
    color: {FG};
}}

QLabel#readOnlyBadge {{
    color: {WARN};
    font-weight: 600;
}}

QToolBar QFrame#toolbarSeparator {{
    background: {BORDER};
    max-width: 1px;
    min-width: 1px;
    margin: 4px 6px;
}}

QStatusBar {{
    background: {BG_ALT};
    color: {FG_DIM};
    border-top: 1px solid {BORDER};
}}
QStatusBar QLabel {{
    color: {FG_DIM};
    padding: 0 8px;
}}
QStatusBar QLabel#statusCost {{
    color: {FG_DIM};
    font-weight: 500;
}}
"""
