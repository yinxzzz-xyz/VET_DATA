import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QGroupBox

from vet_data_modular.math_channel import SearchableMathChannelDialog
from vet_data_modular.window import MDFPlotter
from vet_data_modular.workers import load_data_file


APP = QApplication.instance() or QApplication([])


class ModularSmokeTests(unittest.TestCase):
    def setUp(self):
        self.window = MDFPlotter()

    def tearDown(self):
        # Avoid invoking the legacy console logger in this focused widget test.
        self.window.deleteLater()
        APP.processEvents()

    def test_controls_and_collapsible_groups(self):
        self.assertEqual(self.window.height_spin.minimum(), 80)
        self.assertEqual(self.window.height_spin.maximum(), 900)
        titles = {g.title(): g for g in self.window.findChildren(QGroupBox)}
        self.assertTrue(titles["CAN通道配置"].isCheckable())
        self.assertTrue(titles["GPS轨迹图"].isCheckable())

    def test_selection_search_and_order(self):
        self.window.signals.update({
            "MATH_z": {"name": "z", "display_name": "Calc Z", "comment": "computed", "bus_id": None},
            "raw_a": {"name": "a", "display_name": "Speed", "comment": "vehicle speed", "bus_id": None},
        })
        self.window.refresh_signal_list_ui()
        self.assertEqual(self.window.signal_list_widget.count(), 2)
        speed = next(self.window.signal_list_widget.item(i) for i in range(2) if self.window.signal_list_widget.item(i).data(Qt.ItemDataRole.UserRole) == "raw_a")
        speed.setCheckState(Qt.CheckState.Checked)
        self.assertEqual(self.window.get_selected_signals(), ["raw_a"])
        self.window.search_signals("vehicle")
        self.window._apply_signal_filter()
        self.assertFalse(speed.isHidden())
        self.window._set_view_mode("selected")
        self.assertEqual(sum(not self.window.signal_list_widget.item(i).isHidden() for i in range(2)), 1)

    def test_math_channel_completers(self):
        signals = {"a": {"display_name": "Engine Speed"}, "b": {"display_name": "Vehicle Speed"}}
        dialog = SearchableMathChannelDialog(signals)
        self.assertTrue(dialog.combo_a.isEditable())
        self.assertIsNotNone(dialog.combo_a.completer())
        dialog.deleteLater()

    def test_csv_loader(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "sample.csv")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("timestamp,speed,state\n0,1.5,0\n1,2.5,1\n")
            result = load_data_file(path)
            self.assertEqual(set(result.signals), {"speed", "state"})
            self.assertTrue(np.array_equal(result.data.index.to_numpy(), np.array([0, 1])))


if __name__ == "__main__":
    unittest.main()
