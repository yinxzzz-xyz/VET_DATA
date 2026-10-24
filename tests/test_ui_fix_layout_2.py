import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QApplication, QGroupBox

from vet_data_modular.gui_state import (
    CAN_PANEL_EXPANDED_KEY,
)
from vet_data_modular.theme import DEFAULT_THEME, build_main_window_stylesheet
from vet_data_modular.window import MDFPlotter
from vet_data_modular.workers import normalized_can_path


APP = QApplication.instance() or QApplication([])


class UiFixLayout2Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = QSettings(
            os.path.join(self.temporary.name, "ui-fix-layout-2.ini"),
            QSettings.Format.IniFormat,
        )
        self.settings.clear()
        self.window = MDFPlotter(gui_settings=self.settings)
        self.window.resize(1000, 620)
        self.window.show()
        APP.processEvents()

    def tearDown(self):
        self.window.deleteLater()
        APP.processEvents()
        self.temporary.cleanup()

    def _group(self, title):
        return next(
            group for group in self.window.findChildren(QGroupBox)
            if group.title() == title
        )

    def test_default_panels_and_saved_state_restore(self):
        self.assertFalse(self._group("CAN通道配置").isCheckable())
        self.assertFalse(self._group("可用信号").isCheckable())
        self.assertNotIn("GPS轨迹图", self.window.collapsible_groups)
        self.assertFalse(self.window.show_map_button.isChecked())

        self.settings.setValue(CAN_PANEL_EXPANDED_KEY, True)
        restored = MDFPlotter(gui_settings=self.settings)
        try:
            self.assertFalse(next(
                group for group in restored.findChildren(QGroupBox)
                if group.title() == "CAN通道配置"
            ).isCheckable())
            self.assertNotIn("GPS轨迹图", restored.collapsible_groups)
        finally:
            restored.deleteLater()

    def test_successful_blf_message_scan_keeps_can_page_available(self):
        worker = object()
        self.window._can_detection_generation = 7
        self.window._can_bus_detection_worker = worker

        class DetectionResult(dict):
            def __init__(self, path):
                super().__init__()
                self.mapping = object()
                self.snapshot = SimpleNamespace(
                    file_identity=SimpleNamespace(path=normalized_can_path(path))
                )

        with patch.object(self.window, "_print_bus_info"), patch.object(
            self.window, "create_bus_config_ui"
        ):
            self.window.mdf_path = os.path.join(self.temporary.name, "capture.BLF")
            self.window._finish_can_bus_detection(
                DetectionResult(self.window.mdf_path), 7, worker
            )
            self.assertFalse(self._group("CAN通道配置").isCheckable())

            self.window.mdf_path = os.path.join(self.temporary.name, "capture.asc")
            self.window._finish_can_bus_detection(
                DetectionResult(self.window.mdf_path), 7, worker
            )
            self.assertFalse(self._group("CAN通道配置").isCheckable())

    def test_signal_view_checked_text_is_white_and_unchecked_is_normal(self):
        for mode, active, inactive in (
            ("all", self.window.view_all_btn, self.window.view_selected_btn),
            ("selected", self.window.view_selected_btn, self.window.view_all_btn),
            ("all", self.window.view_all_btn, self.window.view_selected_btn),
        ):
            self.window._set_view_mode(mode)
            APP.processEvents()
            self.assertTrue(active.isChecked())
            self.assertFalse(inactive.isChecked())
            self.assertEqual(active.property("uiRole"), "signalView")
            self.assertEqual(inactive.property("uiRole"), "signalView")

        self.window.view_all_btn.setEnabled(False)
        APP.processEvents()
        self.assertFalse(self.window.view_all_btn.isEnabled())

        stylesheet = build_main_window_stylesheet(DEFAULT_THEME)
        checked_rule = stylesheet.split(
            'QPushButton[uiRole="signalView"]:checked {'
        )[1].split("}", 1)[0]
        normal_rule = stylesheet.split(
            'QPushButton[uiRole="signalView"] {'
        )[1].split("}", 1)[0]
        disabled_rule = stylesheet.split(
            'QPushButton[uiRole="signalView"]:disabled {'
        )[1].split("}", 1)[0]
        self.assertIn(f"color: {DEFAULT_THEME.colors.surface};", checked_rule)
        self.assertIn(f"background-color: {DEFAULT_THEME.colors.primary};", checked_rule)
        self.assertIn(f"color: {DEFAULT_THEME.colors.text_primary};", normal_rule)
        self.assertIn(f"color: {DEFAULT_THEME.colors.disabled_text};", disabled_rule)

    def test_actions_are_reused_in_requested_top_and_bottom_rows(self):
        shortcuts = (
            self.window.load_file_button, self.window.plot_button,
            self.window.math_channel_button, self.window.single_cursor_button,
            self.window.dual_cursor_button,
            self.window.plot_mode_combo,
        )
        self.assertEqual(self.window.quick_actions_layout.count(), 6)
        self.assertEqual(
            [self.window.quick_actions_layout.itemAt(index).widget() for index in range(6)],
            list(shortcuts),
        )
        self.assertEqual(
            [self.window.load_file_button.text(), self.window.plot_button.text(),
             self.window.math_channel_button.text()],
            ["", "", "公式"],
        )
        self.assertEqual(self.window.load_file_button.toolTip(), "打开文件")
        self.assertEqual(self.window.plot_button.toolTip(), "绘制曲线")
        self.assertTrue(self.window.math_channel_button.toolTip())
        self.assertEqual(self.window.plot_mode_combo.currentData(), "independent")
        self.assertEqual(self.window.plot_mode_combo.count(), 2)
        self.assertFalse(self.window.single_cursor_button.isChecked())
        self.assertFalse(self.window.dual_cursor_button.isChecked())
        self.assertIn(
            self.window.save_data_button,
            [self.window.processing_ribbon_layout.itemAt(index).widget()
             for index in range(self.window.processing_ribbon_layout.count())],
        )
        self.assertIn(
            self.window.export_button,
            [self.window.export_ribbon_layout.itemAt(index).widget()
             for index in range(self.window.export_ribbon_layout.count())],
        )
        self.assertIsNotNone(
            self.window._find_layout_containing(
                self.window.layout(), self.window.save_data_button
            )
        )
        self.assertIsNotNone(
            self.window._find_layout_containing(
                self.window.layout(), self.window.blf_slice_button
            )
        )

    def test_moved_buttons_keep_connections_and_enabled_state(self):
        buttons = (
            self.window.plot_button, self.window.export_button,
            self.window.save_data_button, self.window.math_channel_button,
        )
        self.assertTrue(all(not button.isEnabled() for button in buttons))
        self.assertGreater(self.window.plot_button.receivers(self.window.plot_button.clicked), 0)
        self.assertGreater(self.window.export_button.receivers(self.window.export_button.clicked), 0)
        self.assertGreater(self.window.save_data_button.receivers(self.window.save_data_button.clicked), 0)
        self.assertGreater(
            self.window.math_channel_button.receivers(self.window.math_channel_button.clicked), 0
        )
        self.assertGreater(
            self.window.blf_slice_button.receivers(self.window.blf_slice_button.clicked), 0
        )
        self.assertTrue(all(button.isVisible() for button in (
            self.window.load_file_button, self.window.plot_button,
            self.window.math_channel_button, self.window.single_cursor_button,
            self.window.dual_cursor_button,
            self.window.plot_mode_combo,
        )))

    def test_narrow_layout_keeps_shortcuts_visible_and_left_panel_scrolls(self):
        self.window.resize(850, 430)
        self.window.main_splitter.setSizes([320, 530])
        APP.processEvents()

        quick_buttons = (
            self.window.load_file_button, self.window.plot_button,
            self.window.math_channel_button, self.window.single_cursor_button,
            self.window.dual_cursor_button,
            self.window.plot_mode_combo,
        )
        self.assertTrue(all(button.isVisible() and button.width() >= 55 for button in quick_buttons))
        for left, right in zip(quick_buttons, quick_buttons[1:]):
            self.assertFalse(left.geometry().intersects(right.geometry()))

        self.window.ribbon_tabs.setCurrentWidget(self.window.view_ribbon_page)
        APP.processEvents()
        self.assertTrue(self.window.lat_combo.isVisible())
        self.assertTrue(self.window.show_map_button.isVisible())
        self.window.ribbon_tabs.setCurrentWidget(self.window.processing_ribbon_page)
        APP.processEvents()
        self.assertTrue(self.window.blf_slice_button.isVisible())


if __name__ == "__main__":
    unittest.main()
