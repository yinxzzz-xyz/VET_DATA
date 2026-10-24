import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QApplication, QGroupBox

from vet_data_modular.signal_panel import COLOR_ROLE, KEY_ROLE, VALUE_ROLE
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


class LeftManagementTabsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        settings = QSettings(
            os.path.join(self.temporary.name, "ui2-tabs.ini"),
            QSettings.Format.IniFormat,
        )
        self.window = MDFPlotter(gui_settings=settings)
        timestamps = np.array([0.0, 1.0, 2.0])
        self.window.signals = {
            "speed": {
                "name": "speed", "display_name": "Vehicle Speed",
                "comment": "km/h", "bus_id": None,
            },
            "rpm": {
                "name": "rpm", "display_name": "Engine RPM",
                "comment": "rpm", "bus_id": None,
            },
        }
        self.window.can_parsed_data = {
            "speed": {"timestamps": timestamps, "samples": np.array([0.0, 5.0, 10.0])},
            "rpm": {"timestamps": timestamps, "samples": np.array([100.0, 200.0, 300.0])},
        }
        self.window.refresh_signal_list_ui()

    def tearDown(self):
        self.window.deleteLater()
        APP.processEvents()
        self.temporary.cleanup()

    def test_selected_tab_tracks_selection_values_colors_and_removal(self):
        self.assertEqual(self.window.left_tabs.count(), 3)
        for row in range(self.window.signal_list_widget.count()):
            self.window.signal_list_widget.item(row).setCheckState(Qt.CheckState.Checked)

        selected = self.window.selected_signal_list_widget
        self.assertEqual(selected.count(), 2)
        first = selected.item(0)
        key = first.data(KEY_ROLE)
        self.assertIsNotNone(first.data(COLOR_ROLE))

        self.window.left_tabs.setCurrentWidget(self.window.selected_signals_page)
        self.window._current_cursor_time = 1.0
        self.window._mouse_in_plot = True
        self.window._update_signal_list_values()
        self.assertEqual(first.data(VALUE_ROLE), "200.000" if key == "rpm" else "5.000")

        first.setCheckState(Qt.CheckState.Unchecked)
        self.assertNotIn(key, self.window.get_selected_signals())
        self.assertEqual(selected.count(), 1)

    def test_switching_tabs_keeps_signal_and_can_configuration_state(self):
        key = self.window.signal_list_widget.item(0).data(KEY_ROLE)
        self.window.signal_widgets[key]["checkbox"].setChecked(True)
        self.window.left_tabs.setCurrentWidget(self.window.selected_signals_page)
        self.assertEqual(self.window.selected_signal_list_widget.count(), 1)

        self.window.can_bus_data[0] = {
            "name": "Powertrain", "actual_ids": [0x100],
            "id_count": 1, "msg_count": 1, "has_fd": False,
        }
        self.window.create_bus_config_ui()
        self.window.left_tabs.setCurrentWidget(self.window.can_tab_page)

        self.assertEqual(self.window.get_selected_signals(), [key])
        self.assertEqual(len(self.window.bus_config_widgets), 1)
        can_group = next(
            group for group in self.window.findChildren(QGroupBox)
            if group.title() == "CAN通道配置"
        )
        self.assertFalse(can_group.isCheckable())

    def test_selected_order_is_reflected_in_available_signal_order(self):
        for row in range(self.window.signal_list_widget.count()):
            self.window.signal_list_widget.item(row).setCheckState(Qt.CheckState.Checked)
        selected = self.window.selected_signal_list_widget
        first = selected.takeItem(0)
        selected.insertItem(1, first)
        self.window._selected_signal_rows_moved()

        selected_keys = [selected.item(row).data(KEY_ROLE) for row in range(selected.count())]
        source_keys = [
            self.window.signal_list_widget.item(row).data(KEY_ROLE)
            for row in range(self.window.signal_list_widget.count())
        ]
        self.assertEqual(source_keys[:len(selected_keys)], selected_keys)

    def test_long_signal_name_remains_available_in_tooltip(self):
        long_name = "VehiclePowertrainFrontAxleLongitudinalAccelerationSensor"
        self.window.signals["speed"]["display_name"] = long_name
        self.window.refresh_signal_list_ui()
        item = next(
            self.window.signal_list_widget.item(row)
            for row in range(self.window.signal_list_widget.count())
            if self.window.signal_list_widget.item(row).data(KEY_ROLE) == "speed"
        )
        self.assertIn(long_name, item.toolTip())


if __name__ == "__main__":
    unittest.main()
