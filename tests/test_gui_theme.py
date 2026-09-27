import os
import unittest
from dataclasses import FrozenInstanceError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QTextCursor
from PyQt6.QtWidgets import QApplication, QGroupBox

from vet_data_modular.formula_editor import AtomicFormulaEdit
from vet_data_modular.theme import (
    DEFAULT_THEME,
    THEME_NAME,
    THEME_PROPERTY,
    apply_application_theme,
    build_application_stylesheet,
    semantic_button_stylesheet,
)
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


class ThemeTokenTests(unittest.TestCase):
    def test_required_colors_are_valid_and_tokens_are_immutable(self):
        colors = DEFAULT_THEME.colors
        required = (
            colors.primary, colors.secondary, colors.danger, colors.success,
            colors.warning, colors.text_primary, colors.text_secondary,
            colors.background, colors.surface, colors.border, colors.selection,
            colors.selection_text, colors.disabled_background,
            colors.disabled_text,
        )
        self.assertTrue(all(QColor(value).isValid() for value in required))
        with self.assertRaises(FrozenInstanceError):
            colors.primary = "#000000"

    def test_typography_controls_spacing_and_padding_have_ordered_scales(self):
        fonts, controls = DEFAULT_THEME.fonts, DEFAULT_THEME.controls
        spacing, padding = DEFAULT_THEME.spacing, DEFAULT_THEME.padding
        self.assertEqual(
            sorted((fonts.small, fonts.body, fonts.subtitle, fonts.title)),
            [fonts.small, fonts.body, fonts.subtitle, fonts.title],
        )
        self.assertLess(controls.compact_height, controls.standard_height)
        self.assertLess(controls.standard_height, controls.prominent_height)
        self.assertEqual(
            sorted((spacing.xsmall, spacing.small, spacing.medium, spacing.large, spacing.xlarge)),
            [spacing.xsmall, spacing.small, spacing.medium, spacing.large, spacing.xlarge],
        )
        self.assertLess(padding.compact, padding.standard)
        self.assertLess(padding.standard, padding.comfortable)

    def test_application_stylesheet_exposes_shared_selection_and_disabled_tokens(self):
        stylesheet = build_application_stylesheet()
        for value in (
            DEFAULT_THEME.colors.text_primary,
            DEFAULT_THEME.colors.background,
            DEFAULT_THEME.colors.border,
            DEFAULT_THEME.colors.selection,
            DEFAULT_THEME.colors.selection_text,
            DEFAULT_THEME.colors.disabled_text,
        ):
            self.assertIn(value, stylesheet)

    def test_semantic_button_styles_cover_every_action_role(self):
        colors = DEFAULT_THEME.colors
        for role in ("primary", "secondary", "danger", "success", "warning"):
            stylesheet = semantic_button_stylesheet(role)
            self.assertIn(getattr(colors, role), stylesheet)
            self.assertIn(str(DEFAULT_THEME.controls.standard_height), stylesheet)
        with self.assertRaisesRegex(ValueError, "unknown semantic button role"):
            semantic_button_stylesheet("unsupported")

    def test_theme_installation_is_repeatable(self):
        apply_application_theme(APP)
        first = APP.styleSheet()
        apply_application_theme(APP)
        self.assertEqual(APP.property(THEME_PROPERTY), THEME_NAME)
        self.assertEqual(APP.styleSheet(), first)


class ThemeGuiRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        apply_application_theme(APP)

    def test_main_window_titles_controls_and_signal_selection_are_unchanged(self):
        window = MDFPlotter()
        try:
            titles = {group.title() for group in window.findChildren(QGroupBox)}
            self.assertTrue({"可用信号", "CAN通道配置", "GPS轨迹图", "信号曲线"} <= titles)
            self.assertEqual(window.blf_slice_button.text(), "BLF 工况切片")
            window.signals["raw"] = {
                "name": "raw", "display_name": "Raw", "comment": "",
                "bus_id": None,
            }
            window.refresh_signal_list_ui()
            item = window.signal_list_widget.item(0)
            item.setCheckState(Qt.CheckState.Checked)
            self.assertEqual(window.get_selected_signals(), ["raw"])
        finally:
            window.deleteLater()
            APP.processEvents()

    def test_formula_atom_semantic_colors_are_not_replaced_by_theme(self):
        editor = AtomicFormulaEdit()
        try:
            editor.insert_signal("VehicleSpeed", "S001", "speed")
            cursor = QTextCursor(editor.document())
            cursor.setPosition(0)
            cursor.movePosition(QTextCursor.MoveOperation.Right)
            char_format = cursor.charFormat()
            self.assertEqual(char_format.background().color(), QColor("#d9ecff"))
            self.assertEqual(char_format.foreground().color(), QColor("#0b4f87"))
        finally:
            editor.deleteLater()


if __name__ == "__main__":
    unittest.main()
