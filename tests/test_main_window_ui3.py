import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QApplication, QGroupBox

from vet_data_modular.gui_state import (
    CAN_PANEL_EXPANDED_KEY,
    restore_collapsible_panel_state,
)
from vet_data_modular.theme import DEFAULT_THEME
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


class MainWindowUi3Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = QSettings(
            os.path.join(self.temporary.name, "ui3.ini"),
            QSettings.Format.IniFormat,
        )
        self.settings.clear()
        self.window = MDFPlotter(gui_settings=self.settings)
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

    def test_can_card_is_not_checkable_and_gps_controls_are_not_in_left_panel(self):
        signal = self._group("可用信号")
        can = self._group("CAN通道配置")
        self.assertFalse(signal.isCheckable())
        self.assertFalse(can.isCheckable())
        self.assertNotIn("GPS轨迹图", self.window.collapsible_groups)
        self.assertFalse(self.window.show_map_button.isChecked())
        self.assertGreaterEqual(
            self.window.view_ribbon_layout.indexOf(self.window.gps_controls_widget), 0
        )

    def test_left_tabs_show_three_panels_and_can_stays_available(self):
        can = self._group("CAN通道配置")
        self.assertEqual(self.window.left_tabs.count(), 3)
        self.assertEqual(
            [self.window.left_tabs.tabText(index) for index in range(3)],
            ["可用信号", "已选信号", "CAN 配置"],
        )
        self.window.left_tabs.setCurrentWidget(self.window.can_tab_page)
        APP.processEvents()
        self.assertTrue(can.isVisible())

    def test_legacy_can_collapse_state_is_ignored(self):
        self.settings.setValue(CAN_PANEL_EXPANDED_KEY, True)
        self.assertTrue(self.window.close())
        APP.processEvents()
        self.assertTrue(self.settings.value(CAN_PANEL_EXPANDED_KEY, type=bool))

        restored = MDFPlotter(gui_settings=self.settings)
        try:
            self.assertFalse(next(g for g in restored.findChildren(QGroupBox) if g.title() == "CAN通道配置").isCheckable())
            self.assertNotIn("GPS轨迹图", restored.collapsible_groups)
        finally:
            restored.deleteLater()

        self.settings.setValue(CAN_PANEL_EXPANDED_KEY, "damaged")
        restore_collapsible_panel_state(self.window.collapsible_groups, self.settings)
        self.assertFalse(self._group("CAN通道配置").isCheckable())

    def test_signal_and_plot_empty_states_appear_and_clear(self):
        self.assertTrue(self.window.signal_empty_label.isVisible())
        self.assertFalse(self.window.signal_list_widget.isVisible())
        self.assertTrue(self.window.plot_empty_label.isVisible())

        key = "CAN_SIGNAL"
        self.window.signals[key] = {
            "name": key, "display_name": "Speed", "comment": "",
            "bus_id": 0,
        }
        self.window.can_bus_data = {0: {"name": "Bus 0"}}
        self.window.can_parsed_data[key] = {
            "timestamps": np.array([0.0, 1.0, 2.0]),
            "samples": np.array([1.0, 2.0, 3.0]),
            "unit": "km/h",
        }
        self.window.refresh_signal_list_ui()
        self.assertFalse(self.window.signal_empty_label.isVisible())
        self.assertTrue(self.window.signal_list_widget.isVisible())
        self.window.signal_list_widget.item(0).setCheckState(Qt.CheckState.Checked)
        self.window.plot_selected_signals()
        APP.processEvents()
        self.assertEqual(len(self.window.plot_widgets), 1)
        self.assertFalse(self.window.plot_empty_label.isVisible())

    def test_title_and_token_driven_main_window_roles(self):
        self.assertEqual(self.window.windowTitle(), "VET_DATA")
        self.assertEqual(self.window.load_file_button.property("uiRole"), "primary")
        self.assertEqual(self.window.plot_button.property("uiRole"), "primary")
        self.assertEqual(self.window.math_channel_button.property("uiRole"), "secondary")
        self.assertEqual(self.window.search_box.property("uiControl"), "main")
        self.assertIn(DEFAULT_THEME.colors.primary, self.window.styleSheet())
        self.assertIn(DEFAULT_THEME.colors.border, self.window.styleSheet())
        self.assertEqual(self.window.math_channel_button.styleSheet(), "")

    def test_ui2_splitter_and_critical_state_survive_panel_toggles(self):
        original_sizes = self.window.main_splitter.sizes()
        self.window.signals.update({
            "speed": {
                "name": "speed", "display_name": "Speed",
                "comment": "vehicle", "bus_id": None,
            },
            "latitude": {
                "name": "latitude", "display_name": "Latitude",
                "comment": "gps", "bus_id": None,
            },
        })
        self.window.refresh_signal_list_ui()
        self.window.signal_widgets["speed"]["checkbox"].setChecked(True)
        self.window.lat_combo.setCurrentIndex(
            self.window.lat_combo.findData("latitude")
        )
        self.window._set_view_mode("selected")

        APP.processEvents()

        self.assertEqual(self.window.get_selected_signals(), ["speed"])
        self.assertEqual(self.window.lat_combo.currentData(), "latitude")
        self.assertEqual(self.window._view_mode, "selected")
        self.assertEqual(self.window.main_splitter.sizes(), original_sizes)
        self.assertTrue(self.window.blf_slice_button.isEnabled())
        self.assertIn(
            self.window.save_data_button,
            [self.window.processing_ribbon_layout.itemAt(index).widget()
             for index in range(self.window.processing_ribbon_layout.count())],
        )


if __name__ == "__main__":
    unittest.main()
