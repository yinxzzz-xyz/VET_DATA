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


def build_main_window_stylesheet(theme: ThemeTokens = DEFAULT_THEME) -> str:
    """Return compact, token-driven rules scoped by main-window properties."""
    c, f = theme.colors, theme.fonts
    controls, spacing, padding = theme.controls, theme.spacing, theme.padding
    return f"""
QWidget[uiSurface="main"] {{
    background-color: {c.background};
}}
QGroupBox[uiPanel="main"] {{
    color: {c.text_primary};
    font-size: {f.body}px;
    font-weight: 600;
}}
QGroupBox[uiPanel="main"]::title {{
    subcontrol-origin: margin;
    left: {spacing.medium}px;
    padding: 0 {padding.compact}px;
}}
QLineEdit[uiControl="main"] {{
    min-height: {controls.standard_height}px;
    padding: 0 {padding.standard}px;
    color: {c.text_primary};
    background-color: {c.surface};
    border: 1px solid {c.border};
    border-radius: {controls.corner_radius}px;
}}
QPushButton[uiRole="primary"] {{
    min-height: {controls.standard_height}px;
    padding: 0 {padding.standard}px;
    color: {c.surface};
    background-color: {c.primary};
    border: 1px solid {c.primary};
    border-radius: {controls.corner_radius}px;
    font-weight: 600;
}}
QPushButton[uiRole="secondary"] {{
    min-height: {controls.standard_height}px;
    padding: 0 {padding.standard}px;
    color: {c.secondary};
    background-color: {c.surface};
    border: 1px solid {c.secondary};
    border-radius: {controls.corner_radius}px;
}}
QPushButton[uiRole="primary"]:disabled,
QPushButton[uiRole="secondary"]:disabled {{
    color: {c.disabled_text};
    background-color: {c.disabled_background};
    border-color: {c.border};
}}
QLabel[uiTextRole="secondary"], QLabel[uiEmptyState="true"] {{
    color: {c.text_secondary};
}}
QLabel[uiEmptyState="true"] {{
    padding: {padding.comfortable}px;
    font-size: {f.body}px;
}}
QSplitter[uiSplitter="main"]::handle {{
    background-color: {c.border};
}}
""".strip()


def build_can_config_stylesheet(theme: ThemeTokens = DEFAULT_THEME) -> str:
    """Return compact token-driven rules for CAN configuration rows."""
    c, f = theme.colors, theme.fonts
    controls, padding = theme.controls, theme.padding
    return f"""
QWidget[uiCanRow="true"] {{
    background-color: {c.surface};
    border-bottom: 1px solid {c.border};
}}
QWidget[uiCanHeader="true"] {{
    background-color: {c.background};
    border-bottom: 1px solid {c.border};
}}
QLabel[uiCanHeaderCell="true"] {{
    color: {c.text_secondary};
    font-size: {f.small}px;
    font-weight: 600;
}}
QLabel[uiCanCell="true"] {{
    color: {c.text_primary};
    font-size: {f.small}px;
}}
QLabel[uiCanCell="protocol"][statusKind="missing"] {{
    color: {c.text_secondary};
}}
QLabel[uiCanCell="protocol"][statusKind="configured"] {{
    color: {c.success};
}}
QLabel[uiCanCell="status"][statusKind="idle"] {{ color: {c.text_secondary}; }}
QLabel[uiCanCell="status"][statusKind="loading"] {{ color: {c.warning}; }}
QLabel[uiCanCell="status"][statusKind="success"] {{ color: {c.success}; }}
QLabel[uiCanCell="status"][statusKind="error"] {{ color: {c.danger}; }}
QLineEdit[uiCanCell="bus"] {{
    min-height: {controls.compact_height}px;
    padding: 0 {padding.compact}px;
    color: {c.text_primary};
    background-color: {c.surface};
    border: 1px solid {c.border};
    border-radius: {controls.corner_radius}px;
}}
QPushButton[uiRole="primary"] {{
    min-height: {controls.compact_height}px;
    padding: 0 {padding.compact}px;
    color: {c.surface};
    background-color: {c.primary};
    border: 1px solid {c.primary};
    border-radius: {controls.corner_radius}px;
    font-size: {f.small}px;
    font-weight: 600;
}}
QPushButton[uiRole="secondary"] {{
    min-height: {controls.compact_height}px;
    padding: 0 {padding.compact}px;
    color: {c.secondary};
    background-color: {c.surface};
    border: 1px solid {c.border};
    border-radius: {controls.corner_radius}px;
    font-size: {f.small}px;
}}
QPushButton:disabled {{
    color: {c.disabled_text};
    background-color: {c.disabled_background};
    border-color: {c.border};
}}
""".strip()


def build_signal_filter_stylesheet(theme: ThemeTokens = DEFAULT_THEME) -> str:
    """Return dense, neutral styling for the signal filter dialog."""
    c, f = theme.colors, theme.fonts
    controls, padding = theme.controls, theme.padding
    return f"""
QDialog {{ background-color: {c.background}; }}
QLabel[uiTextRole="summary"] {{
    color: {c.text_primary};
    font-size: {f.body}px;
    font-weight: 600;
}}
QLabel[uiTextRole="selection"] {{
    color: {c.text_secondary};
    font-size: {f.body}px;
    font-weight: 600;
}}
QLabel[uiTextRole="selection"][summaryState="selected"] {{ color: {c.success}; }}
QLineEdit {{
    min-height: {controls.standard_height}px;
    padding: 0 {padding.standard}px;
    background-color: {c.surface};
    border: 1px solid {c.border};
    border-radius: {controls.corner_radius}px;
}}
QPushButton[uiRole="primary"] {{
    min-height: {controls.standard_height}px;
    padding: 0 {padding.standard}px;
    color: {c.surface};
    background-color: {c.primary};
    border: 1px solid {c.primary};
    border-radius: {controls.corner_radius}px;
    font-weight: 600;
}}
QPushButton[uiRole="secondary"] {{
    min-height: {controls.standard_height}px;
    padding: 0 {padding.standard}px;
    color: {c.secondary};
    background-color: {c.surface};
    border: 1px solid {c.border};
    border-radius: {controls.corner_radius}px;
}}
QTreeWidget {{
    color: {c.text_primary};
    background-color: {c.surface};
    alternate-background-color: {c.background};
    border: 1px solid {c.border};
    font-size: {f.small}px;
    selection-background-color: {c.selection};
    selection-color: {c.selection_text};
}}
QTreeWidget::item {{ min-height: 20px; padding: 1px 2px; }}
QHeaderView::section {{
    color: {c.text_primary};
    background-color: {c.disabled_background};
    padding: {padding.compact}px;
    border: none;
    border-right: 1px solid {c.border};
    border-bottom: 1px solid {c.border};
    font-size: {f.small}px;
    font-weight: 600;
}}
""".strip()


def build_formula_editor_stylesheet(theme: ThemeTokens = DEFAULT_THEME) -> str:
    """Return token-driven styling for the formula definition workflow."""
    c, f = theme.colors, theme.fonts
    controls, padding = theme.controls, theme.padding
    return f"""
QDialog[uiDialog="formulaEditor"] {{ background-color: {c.background}; }}
QWidget[uiFormulaSection="true"] {{
    background-color: {c.surface};
    border: 1px solid {c.border};
    border-radius: {controls.corner_radius}px;
}}
QLabel[uiFormulaHeading="true"] {{
    color: {c.text_primary};
    font-size: {f.subtitle}px;
    font-weight: 600;
}}
QLabel[uiFormulaCaption="true"] {{
    color: {c.text_secondary};
    font-size: {f.small}px;
    font-weight: 600;
}}
QLineEdit, QTextEdit[uiFormulaCore="true"], QPlainTextEdit, QListWidget {{
    color: {c.text_primary};
    background-color: {c.surface};
    border: 1px solid {c.border};
    border-radius: {controls.corner_radius}px;
    selection-background-color: {c.selection};
    selection-color: {c.selection_text};
}}
QLineEdit {{
    min-height: {controls.standard_height}px;
    padding: 0 {padding.standard}px;
}}
QTextEdit[uiFormulaCore="true"] {{
    padding: {padding.standard}px;
    font-size: {f.body}px;
}}
QPlainTextEdit {{
    padding: {padding.compact}px;
    font-size: {f.small}px;
}}
QListWidget {{ font-size: {f.small}px; }}
QListWidget::item {{ min-height: 22px; padding: 1px {padding.compact}px; }}
QPushButton[uiRole="primary"] {{
    min-height: {controls.standard_height}px;
    padding: 0 {padding.standard}px;
    color: {c.surface};
    background-color: {c.primary};
    border: 1px solid {c.primary};
    border-radius: {controls.corner_radius}px;
    font-weight: 600;
}}
QPushButton[uiRole="secondary"], QPushButton[uiRole="formulaFunction"] {{
    min-height: {controls.compact_height}px;
    padding: 0 {padding.compact}px;
    color: {c.secondary};
    background-color: {c.surface};
    border: 1px solid {c.border};
    border-radius: {controls.corner_radius}px;
}}
QPushButton[uiRole="formulaFunction"] {{ font-size: {f.small}px; }}
QPushButton:disabled {{
    color: {c.disabled_text};
    background-color: {c.disabled_background};
    border-color: {c.border};
}}
QSplitter[uiFormulaSplitter="true"]::handle {{
    background-color: {c.border};
    width: {theme.spacing.xsmall}px;
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
    "build_can_config_stylesheet",
    "build_formula_editor_stylesheet",
    "build_main_window_stylesheet",
    "build_signal_filter_stylesheet",
    "semantic_button_stylesheet",
]
