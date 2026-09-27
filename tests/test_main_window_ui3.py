import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QApplication, QGroupBox

from vet_data_modular.gui_state import (
    CAN_PANEL_EXPANDED_KEY,
    GPS_PANEL_EXPANDED_KEY,
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

    def test_can_and_gps_default_collapsed_while_signals_stay_expanded(self):
        signal = self._group("可用信号")
        can = self._group("CAN通道配置")
        gps = self._group("GPS轨迹图")
        self.assertFalse(signal.isCheckable())
        self.assertFalse(can.isChecked())
        self.assertFalse(gps.isChecked())
        self.assertLess(can.maximumHeight(), 100)
        self.assertLess(gps.maximumHeight(), 100)

    def test_collapse_releases_space_and_keeps_vertical_splitter(self):
        can = self._group("CAN通道配置")
        collapsed_height = can.height()
        signal_height_collapsed = self._group("可用信号").height()
        self.assertEqual(self.window.left_splitter.orientation(), Qt.Orientation.Vertical)
        self.assertFalse(self.window.left_splitter.childrenCollapsible())
        self.assertGreater(signal_height_collapsed, collapsed_height)

        can.setChecked(True)
        APP.processEvents()
        self.assertGreater(can.height(), collapsed_height)
        can.setChecked(False)
        APP.processEvents()
        self.assertLess(can.maximumHeight(), 100)
        self.assertGreater(
            self._group("可用信号").height(), can.height()
        )

    def test_collapse_state_round_trip_and_invalid_values_fall_back(self):
        self._group("CAN通道配置").setChecked(True)
        self._group("GPS轨迹图").setChecked(False)
        self.assertTrue(self.window.close())
        APP.processEvents()
        self.assertTrue(self.settings.value(CAN_PANEL_EXPANDED_KEY, type=bool))
        self.assertFalse(self.settings.value(GPS_PANEL_EXPANDED_KEY, type=bool))

        restored = MDFPlotter(gui_settings=self.settings)
        try:
            self.assertTrue(restored.collapsible_groups["CAN通道配置"].isChecked())
            self.assertFalse(restored.collapsible_groups["GPS轨迹图"].isChecked())
        finally:
            restored.deleteLater()

        self.settings.setValue(CAN_PANEL_EXPANDED_KEY, "damaged")
        self.settings.setValue(GPS_PANEL_EXPANDED_KEY, [True])
        restore_collapsible_panel_state(self.window.collapsible_groups, self.settings)
        self.assertFalse(self._group("CAN通道配置").isChecked())
        self.assertFalse(self._group("GPS轨迹图").isChecked())

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

        self._group("CAN通道配置").setChecked(True)
        self._group("GPS轨迹图").setChecked(True)
        self._group("CAN通道配置").setChecked(False)
        APP.processEvents()

        self.assertEqual(self.window.get_selected_signals(), ["speed"])
        self.assertEqual(self.window.lat_combo.currentData(), "latitude")
        self.assertEqual(self.window._view_mode, "selected")
        self.assertEqual(self.window.main_splitter.sizes(), original_sizes)
        self.assertTrue(self.window.blf_slice_button.isEnabled())
        self.assertIsNotNone(
            self.window._find_layout_containing(
                self.window.layout(), self.window.save_data_button
            )
        )


if __name__ == "__main__":
    unittest.main()
