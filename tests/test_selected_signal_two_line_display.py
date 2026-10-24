import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import QPointF, QRect, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QListWidget, QListWidgetItem

from vet_data_modular.signal_panel import (
    COLOR_ROLE,
    KEY_ROLE,
    NAME_ROLE,
    VALUE_ROLE,
    SignalItemDelegate,
)
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


class SelectedSignalTwoLineDisplayTests(unittest.TestCase):
    def setUp(self):
        self.window = MDFPlotter()
        self.window.resize(1200, 800)
        self.window.signals = {
            "speed": {
                "name": "speed",
                "display_name": "VehiclePowertrainFrontAxleLongitudinalSpeed",
                "comment": "km/h",
                "bus_id": None,
            },
            "rpm": {
                "name": "rpm", "display_name": "Engine RPM",
                "comment": "rpm", "bus_id": None,
            },
            "empty": {
                "name": "empty", "display_name": "No Samples",
                "comment": "", "bus_id": None,
            },
        }
        self.window.can_parsed_data = {
            "speed": {
                "timestamps": np.array([0.0, 1.0]),
                "samples": np.array([0.0, 5.0]),
                "unit": "km/h",
            },
            "rpm": {
                "timestamps": np.array([0.0, 1.0, 2.0]),
                "samples": np.array([100.0, 200.0, 300.0]),
                "unit": "rpm",
            },
            "empty": {
                "timestamps": np.array([]),
                "samples": np.array([]),
                "unit": "",
            },
        }
        self.window.refresh_signal_list_ui()
        for row in range(self.window.signal_list_widget.count()):
            self.window.signal_list_widget.item(row).setCheckState(
                Qt.CheckState.Checked
            )
        self.window.left_tabs.setCurrentWidget(self.window.selected_signals_page)
        self.window.show()
        APP.processEvents()

    def tearDown(self):
        self.window.hide()
        self.window.deleteLater()
        APP.processEvents()

    def _selected_item(self, key):
        return next(
            self.window.selected_signal_list_widget.item(row)
            for row in range(self.window.selected_signal_list_widget.count())
            if self.window.selected_signal_list_widget.item(row).data(KEY_ROLE) == key
        )

    def _drag_single_cursor(self, plot, time_value):
        view_box = plot.getViewBox()
        y_min, y_max = view_box.viewRange()[1]
        scene_position = view_box.mapViewToScene(
            QPointF(float(time_value), (y_min + y_max) * 0.5)
        )
        self.window._dragging_cursor = {"plot": plot, "name": "single"}
        self.window._move_active_cursor(plot, "single", scene_position)
        QTest.qWait(80)
        APP.processEvents()

    def test_name_and_value_use_separate_fixed_rows_and_keep_selection_metadata(self):
        widget = QListWidget()
        widget.setObjectName("selectedSignalList")
        delegate = SignalItemDelegate(widget)
        widget.setItemDelegate(delegate)
        item = QListWidgetItem("")
        item.setData(NAME_ROLE, "VehiclePowertrainFrontAxleLongitudinalSpeed")
        item.setData(VALUE_ROLE, "125.600")
        item.setData(COLOR_ROLE, Qt.GlobalColor.blue)
        widget.addItem(item)

        name_rect, value_rect = delegate.selected_name_value_rects(
            QRect(0, 0, 320, 54)
        )

        self.assertLess(name_rect.bottom(), value_rect.top())
        self.assertEqual(name_rect.width(), value_rect.width())
        self.assertEqual(delegate.selected_value_text(None), "—")
        self.assertEqual(delegate.selected_value_text("125.600"), "125.600")
        selected = self._selected_item("speed")
        original_color = selected.data(COLOR_ROLE)
        self.assertIsNotNone(original_color)
        self.assertEqual(selected.sizeHint().height(), 54)
        self.assertEqual(selected.data(NAME_ROLE), self.window.signals["speed"]["display_name"])
        widget.deleteLater()

    def test_single_cursor_updates_values_clears_stale_values_and_keeps_text_rules(self):
        selected_widget = self.window.selected_signal_list_widget
        original_keys = [
            selected_widget.item(row).data(KEY_ROLE)
            for row in range(selected_widget.count())
        ]
        original_colors = {
            selected_widget.item(row).data(KEY_ROLE): selected_widget.item(row).data(COLOR_ROLE)
            for row in range(selected_widget.count())
        }
        original_width = selected_widget.viewport().width()
        self.window.plot_selected_signals()
        self.assertEqual(len(self.window.plot_widgets), 2)
        self.window.single_cursor_button.click()
        rpm_plot = next(
            plot for plot in self.window.plot_widgets
            if plot.signal_info["name"] == "rpm"
        )
        self._drag_single_cursor(rpm_plot, 1.0)
        self.assertEqual(self._selected_item("speed").data(VALUE_ROLE), "5.000")
        self.assertEqual(self._selected_item("rpm").data(VALUE_ROLE), "200.000")
        self.assertIsNone(self._selected_item("empty").data(VALUE_ROLE))

        self._drag_single_cursor(rpm_plot, 1.5)
        self.assertIsNone(self._selected_item("speed").data(VALUE_ROLE))
        self.assertEqual(self._selected_item("rpm").data(VALUE_ROLE), "300.000")
        self.assertEqual(selected_widget.viewport().width(), original_width)
        self.assertEqual(
            [selected_widget.item(row).data(KEY_ROLE) for row in range(selected_widget.count())],
            original_keys,
        )
        for key, color in original_colors.items():
            self.assertEqual(self._selected_item(key).data(COLOR_ROLE), color)

        self.window.signals["enum"] = {
            "name": "enum", "display_name": "Ignition State",
            "comment": "", "bus_id": None,
        }
        self.window.refresh_signal_list_ui()
        enum_item = next(
            self.window.signal_list_widget.item(row)
            for row in range(self.window.signal_list_widget.count())
            if self.window.signal_list_widget.item(row).data(KEY_ROLE) == "enum"
        )
        enum_item.setCheckState(Qt.CheckState.Checked)
        enum_signal = SimpleNamespace(
            timestamps=np.array([0.0, 1.0, 2.0]),
            samples=np.array([0, 1, 0]),
            is_text_signal=True,
            raw_text_values=("OFF", "ON", "OFF"),
        )
        self.window._current_cursor_time = 0.5
        with patch.object(self.window, "_get_signal", return_value=enum_signal):
            self.window._update_signal_list_values()
        self.assertEqual(self._selected_item("enum").data(VALUE_ROLE), "ON")

        self.window.single_cursor_button.click()
        self.window._update_signal_list_values()
        for row in range(self.window.selected_signal_list_widget.count()):
            self.assertIsNone(
                self.window.selected_signal_list_widget.item(row).data(VALUE_ROLE)
            )


if __name__ == "__main__":
    unittest.main()
