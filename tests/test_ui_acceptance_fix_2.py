import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest

from vet_data_modular.signal_panel import COLOR_ROLE, KEY_ROLE
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


class UiAcceptanceFix2Tests(unittest.TestCase):
    def setUp(self):
        self.window = MDFPlotter()
        self.window.resize(1200, 800)
        self.window.show()
        APP.processEvents()

    def tearDown(self):
        self.window.hide()
        self.window.deleteLater()
        APP.processEvents()

    def test_color_change_updates_metadata_both_lists_and_only_target_curve(self):
        keys = ("speed", "rpm")
        self.window.signals = {
            key: {"name": key, "display_name": key.title(), "comment": "", "bus_id": 0}
            for key in keys
        }
        self.window.can_parsed_data = {
            key: {
                "timestamps": np.array([0.0, 1.0, 2.0]),
                "samples": np.array([1.0, 2.0, 3.0]),
                "unit": "",
            }
            for key in keys
        }
        self.window.can_bus_data = {0: {"name": "Bus 0"}}
        self.window.refresh_signal_list_ui()
        for row in range(self.window.signal_list_widget.count()):
            self.window.signal_list_widget.item(row).setCheckState(Qt.CheckState.Checked)
        selected_before = self.window.get_selected_signals()
        self.window.plot_mode_combo.setCurrentIndex(
            self.window.plot_mode_combo.findData("overlay")
        )
        self.window.plot_selected_signals()
        self.assertEqual(len(self.window.plot_widgets), 1)
        records = self.window.plot_widgets[0].signal_records
        target, other = records
        other_color = other["data_curve"].opts["pen"].color()
        self.window.set_cursor_mode("dual")
        cursor_before = self.window.cursor_mode
        color_row = next(
            row for row in range(self.window.signal_list_widget.count())
            if self.window.signal_list_widget.item(row).data(KEY_ROLE) == target["signal_key"]
        )
        item_rect = self.window.signal_list_widget.visualItemRect(
            self.window.signal_list_widget.item(color_row)
        )
        color_point = self.window.signal_list_delegate.color_rect(item_rect).center()

        with patch(
            "vet_data_modular.signal_panel.QColorDialog.getColor",
            return_value=QColor("#123456"),
        ) as choose_color:
            QTest.mouseClick(
                self.window.signal_list_widget.viewport(),
                Qt.MouseButton.LeftButton,
                pos=color_point,
            )
            choose_color.assert_called_once()

        expected = QColor("#123456")
        self.assertEqual(self.window.signals[target["signal_key"]]["display_color"], "#123456")
        for widget in (self.window.signal_list_widget, self.window.selected_signal_list_widget):
            item = next(
                widget.item(row) for row in range(widget.count())
                if widget.item(row).data(KEY_ROLE) == target["signal_key"]
            )
            self.assertEqual(item.data(COLOR_ROLE), expected)
        self.assertEqual(target["data_curve"].opts["pen"].color(), expected)
        self.assertEqual(target["spot_item"].opts["brush"].color(), expected)
        self.assertEqual(other["data_curve"].opts["pen"].color(), other_color)
        self.assertEqual(self.window.get_selected_signals(), selected_before)
        self.assertEqual(self.window.cursor_mode, cursor_before)

    def test_cancelled_color_selection_keeps_existing_color(self):
        key = "speed"
        self.window.signals = {
            key: {"name": key, "display_name": "Speed", "comment": "", "bus_id": None}
        }
        self.window.refresh_signal_list_ui()
        old_color = self.window._signal_color(key)
        with patch(
            "vet_data_modular.signal_panel.QColorDialog.getColor",
            return_value=QColor(),
        ):
            self.window._choose_signal_color(key)
        self.assertEqual(self.window._signal_color(key), old_color)
        self.assertNotIn("display_color", self.window.signals[key])


if __name__ == "__main__":
    unittest.main()
