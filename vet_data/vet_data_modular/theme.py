"""Central GUI design tokens and lightweight Qt stylesheet helpers.

This module is intentionally independent from VET_DATA widgets.  Later UI
stages can consume the tokens or the small semantic helpers without creating a
second widget hierarchy or coupling theme code to business behavior.
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtWidgets import QApplication


@dataclass(frozen=True, slots=True)
class ColorTokens:
    primary: str = "#2563EB"
    secondary: str = "#64748B"
    danger: str = "#DC2626"
    success: str = "#16A34A"
    warning: str = "#D97706"
    text_primary: str = "#1F2937"
    text_secondary: str = "#64748B"
    background: str = "#F8FAFC"
    surface: str = "#FFFFFF"
    border: str = "#CBD5E1"
    selection: str = "#DBEAFE"
    selection_text: str = "#1E3A5F"
    disabled_background: str = "#E5E7EB"
    disabled_text: str = "#94A3B8"


@dataclass(frozen=True, slots=True)
class FontSizeTokens:
    small: int = 10
    body: int = 12
    subtitle: int = 14
    title: int = 16


@dataclass(frozen=True, slots=True)
class ControlSizeTokens:
    compact_height: int = 26
    standard_height: int = 30
    prominent_height: int = 36
    corner_radius: int = 4


@dataclass(frozen=True, slots=True)
class SpacingTokens:
    xsmall: int = 4
    small: int = 8
    medium: int = 12
    large: int = 16
    xlarge: int = 24


@dataclass(frozen=True, slots=True)
class PaddingTokens:
    compact: int = 4
    standard: int = 8
    comfortable: int = 12


@dataclass(frozen=True, slots=True)
class ThemeTokens:
    colors: ColorTokens = ColorTokens()
    fonts: FontSizeTokens = FontSizeTokens()
    controls: ControlSizeTokens = ControlSizeTokens()
    spacing: SpacingTokens = SpacingTokens()
    padding: PaddingTokens = PaddingTokens()


DEFAULT_THEME = ThemeTokens()
THEME_PROPERTY = "vetDataTheme"
THEME_NAME = "default-light"


def build_application_stylesheet(theme: ThemeTokens = DEFAULT_THEME) -> str:
    """Return conservative application defaults; local legacy QSS still wins."""
    c, f = theme.colors, theme.fonts
    return f"""
QWidget {{
    color: {c.text_primary};
    font-size: {f.body}px;
}}
QDialog {{
    background-color: {c.background};
}}
QWidget:disabled {{
    color: {c.disabled_text};
}}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox,
QListWidget, QTreeWidget, QTableWidget {{
    selection-background-color: {c.selection};
    selection-color: {c.selection_text};
}}
QToolTip {{
    color: {c.text_primary};
    background-color: {c.surface};
    border: 1px solid {c.border};
}}
""".strip()


def semantic_button_stylesheet(
    role: str = "primary", theme: ThemeTokens = DEFAULT_THEME
) -> str:
    """Return reusable QSS for a semantic action button."""
    colors = theme.colors
    role_colors = {
        "primary": colors.primary,
        "secondary": colors.secondary,
        "danger": colors.danger,
        "success": colors.success,
        "warning": colors.warning,
    }
    try:
        background = role_colors[role]
    except KeyError as exc:
        raise ValueError(f"unknown semantic button role: {role}") from exc
    controls, padding = theme.controls, theme.padding
    return f"""
QPushButton {{
    min-height: {controls.standard_height}px;
    padding: {padding.compact}px {padding.standard}px;
    border: 1px solid {background};
    border-radius: {controls.corner_radius}px;
    color: {colors.surface};
    background-color: {background};
}}
QPushButton:disabled {{
    color: {colors.disabled_text};
    background-color: {colors.disabled_background};
    border-color: {colors.border};
}}
""".strip()


def apply_application_theme(
    application: QApplication, theme: ThemeTokens = DEFAULT_THEME
) -> None:
    """Install the shared base stylesheet without touching widget structure."""
    application.setProperty(THEME_PROPERTY, THEME_NAME)
    application.setStyleSheet(build_application_stylesheet(theme))


__all__ = [
    "ColorTokens",
    "ControlSizeTokens",
    "DEFAULT_THEME",
    "FontSizeTokens",
    "PaddingTokens",
    "SpacingTokens",
    "THEME_NAME",
    "THEME_PROPERTY",
    "ThemeTokens",
    "apply_application_theme",
    "build_application_stylesheet",
    "semantic_button_stylesheet",
]
