"""Reusable Aura scrollbar stylesheet fragments."""

from __future__ import annotations

from aura.gui.theme import ACCENT, BG_ALT, BORDER_STRONG


def aura_scrollbar_qss(scope: str = "") -> str:
    """Return Aura-themed QScrollBar rules, optionally scoped to a widget selector."""
    prefix = f"{scope} " if scope else ""
    return f"""
{prefix}QScrollBar:vertical {{
    background: rgba(255, 255, 255, 0.035);
    width: 12px;
    margin: 0;
    border: none;
    border-radius: 6px;
}}
{prefix}QScrollBar::handle:vertical {{
    background: {BORDER_STRONG};
    border-radius: 5px;
    min-height: 36px;
    margin: 2px;
}}
{prefix}QScrollBar::handle:vertical:hover {{
    background: #5f6b86;
}}
{prefix}QScrollBar::handle:vertical:pressed {{
    background: {ACCENT};
}}
{prefix}QScrollBar::add-line:vertical,
{prefix}QScrollBar::sub-line:vertical {{
    height: 0;
    background: transparent;
    border: none;
}}
{prefix}QScrollBar::add-page:vertical,
{prefix}QScrollBar::sub-page:vertical {{
    background: transparent;
}}
{prefix}QScrollBar:horizontal {{
    background: rgba(255, 255, 255, 0.035);
    height: 12px;
    margin: 0;
    border: none;
    border-radius: 6px;
}}
{prefix}QScrollBar::handle:horizontal {{
    background: {BORDER_STRONG};
    border-radius: 5px;
    min-width: 36px;
    margin: 2px;
}}
{prefix}QScrollBar::handle:horizontal:hover {{
    background: #5f6b86;
}}
{prefix}QScrollBar::handle:horizontal:pressed {{
    background: {ACCENT};
}}
{prefix}QScrollBar::add-line:horizontal,
{prefix}QScrollBar::sub-line:horizontal {{
    width: 0;
    background: transparent;
    border: none;
}}
{prefix}QScrollBar::add-page:horizontal,
{prefix}QScrollBar::sub-page:horizontal {{
    background: transparent;
}}
{prefix}QScrollBar:disabled {{
    background: rgba(255, 255, 255, 0.02);
}}
{prefix}QScrollBar::handle:disabled {{
    background: {BG_ALT};
}}
"""
