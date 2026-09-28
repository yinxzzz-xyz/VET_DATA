import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QSizePolicy

from vet_data_modular import baseline
from vet_data_modular.blf_slice_models import (
    ConditionResult, ConditionSpec, ConditionStatus, InputMode, SliceTask,
    TaskResult, TaskStatus,
)
from vet_data_modular.blf_slice_progress import BlfSliceResultDialog
from vet_data_modular.formula_editor import FormulaEditorDialog
from vet_data_modular.theme import DEFAULT_THEME
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


class PlotStatisticsUi8Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        settings = QSettings(
            os.path.join(self.temporary.name, "ui8.ini"), QSettings.Format.IniFormat
        )
        self.window = MDFPlotter(gui_settings=settings)
        self.window.resize(1200, 800)
        timestamps = np.linspace(0.0, 10.0, 101)
        self.window.signals = {}
        self.window.can_parsed_data = {}
        for index in range(2):
            key = f"CAN_UI8_{index}"
            self.window.signals[key] = {
                "name": key, "display_name": f"UI8 signal {index}",
                "comment": "", "unit": "V", "bus_id": 0,
            }
            self.window.can_parsed_data[key] = {
                "timestamps": timestamps,
                "samples": np.sin(timestamps) + index,
                "unit": "V",
            }
        self.window.refresh_signal_list_ui()
        for row in range(self.window.signal_list_widget.count()):
            self.window.signal_list_widget.item(row).setCheckState(Qt.CheckState.Checked)
        self.window.show()
        self.window.plot_selected_signals()
        APP.processEvents()

    def tearDown(self):
        self.window.deleteLater()
        APP.processEvents()
        self.temporary.cleanup()

    def _set_width(self, width):
        with patch.object(self.window, "_plot_usable_width", return_value=width):
            return [
                self.window._plot_stats_visibility_changed(plot)
                for plot in self.window.plot_widgets
            ]

    def test_all_plots_share_hide_restore_rule_and_keep_statistics(self):
        self.assertEqual(len(self.window.plot_widgets), 2)
        self.assertTrue(all(width > 0 for width in (
            self.window._plot_usable_width(plot) for plot in self.window.plot_widgets
        )))
        self.assertEqual(self._set_width(400), [True, True])
        for plot in self.window.plot_widgets:
            self.window._update_stats(plot)
            text = plot.stats_text_item.textItem.toPlainText()
            self.assertTrue(all(label in text for label in ("最大", "最小", "平均", "标准差")))
        self.assertEqual(self._set_width(290), [False, False])
        self.assertTrue(all(not plot.stats_text_item.isVisible() for plot in self.window.plot_widgets))
        self.assertEqual(self._set_width(350), [True, True])
        self.assertTrue(all(plot.stats_text_item.isVisible() for plot in self.window.plot_widgets))

    def test_hysteresis_is_stable_between_thresholds(self):
        plot = self.window.plot_widgets[0]
        with patch.object(self.window, "_plot_usable_width", return_value=290):
            self.assertFalse(self.window._plot_stats_visibility_changed(plot))
        for width in (305, 320, 339, 320, 301):
            with patch.object(self.window, "_plot_usable_width", return_value=width):
                self.assertFalse(self.window._plot_stats_visibility_changed(plot))
        with patch.object(self.window, "_plot_usable_width", return_value=341):
            self.assertTrue(self.window._plot_stats_visibility_changed(plot))
        for width in (339, 320, 301):
            with patch.object(self.window, "_plot_usable_width", return_value=width):
                self.assertTrue(self.window._plot_stats_visibility_changed(plot))


class CanAndFormulaUi8Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temporary.cleanup()

    def test_can_actions_are_compact_inline_and_narrow_layout_stays_safe(self):
        settings = QSettings(
            os.path.join(self.temporary.name, "can.ini"), QSettings.Format.IniFormat
        )
        window = MDFPlotter(gui_settings=settings)
        try:
            window.can_bus_data = {1: {
                "name": "Powertrain", "actual_ids": [0x100], "id_count": 1,
                "msg_count": 1, "has_fd": False,
            }}
            with patch.object(baseline.QTimer, "singleShot"):
                window.create_bus_config_ui()
            row = window.bus_config_widgets[1]
            buttons = (row.select_protocol_btn, row.protocol_clear_btn, row.parse_btn)
            window.resize(1280, 800)
            window.main_splitter.setSizes([440, 840])
            window.collapsible_groups["CAN通道配置"].setChecked(True)
            window.show()
            APP.processEvents()
            visible_buttons = tuple(button for button in buttons if button.isVisible())
            self.assertEqual(
                {button.y() for button in visible_buttons}, {visible_buttons[0].y()}
            )
            self.assertLessEqual(row.sizeHint().height(), 56)
            self.assertEqual(window.bus_scroll.horizontalScrollBar().maximum(), 0)

            window.resize(850, 700)
            window.main_splitter.setSizes([320, 530])
            row.protocol_clear_btn.setVisible(True)
            APP.processEvents()
            self.assertTrue(all(button.width() > 0 for button in buttons))
            for left, right in zip(buttons, buttons[1:]):
                self.assertFalse(left.geometry().intersects(right.geometry()))
            self.assertEqual(
                window.bus_scroll.horizontalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAsNeeded,
            )
        finally:
            window.deleteLater()
            APP.processEvents()

    def test_formula_signal_area_has_priority_and_grows_more_than_editor(self):
        signals = {
            "speed": {"name": "speed", "display_name": "Vehicle Speed", "source": "MDF"}
        }
        dialog = FormulaEditorDialog(signals, background_calculation=False)
        try:
            dialog.show()
            dialog.resize(900, 760)
            APP.processEvents()
            initial_signal = dialog.signal_splitter.height()
            initial_formula = dialog.formula_edit.height()
            self.assertGreaterEqual(dialog.signal_splitter.minimumHeight(), 190)
            self.assertGreater(initial_signal, initial_formula)

            dialog.resize(900, 940)
            APP.processEvents()
            signal_growth = dialog.signal_splitter.height() - initial_signal
            formula_growth = dialog.formula_edit.height() - initial_formula
            self.assertGreater(signal_growth, 0)
            self.assertGreater(signal_growth, formula_growth)
            self.assertTrue(dialog.signal_list.isVisible())
            self.assertTrue(dialog.binding_list.isVisible())
        finally:
            dialog.deleteLater()
            APP.processEvents()


class FinalConsistencyUi8Tests(unittest.TestCase):
    def test_blf_fixed_ui_words_are_chinese_and_paths_remain_accessible(self):
        condition = ConditionSpec(1, "测试", "测试", datetime(2026, 1, 1))
        long_output = Path("C:/") / ("very-long-output-directory-" * 8)
        task = SliceTask(
            InputMode.FILE, Path("input.blf"), Path("table.csv"), long_output,
            (condition,), datetime(2026, 1, 1),
        )
        result = TaskResult(
            task, TaskStatus.COMPLETED,
            (ConditionResult(condition, ConditionStatus.NO_DATA),),
        )
        dialog = BlfSliceResultDialog(result)
        try:
            self.assertEqual(dialog.status_label.text(), "任务状态：已完成")
            self.assertEqual(dialog.table.item(0, 1).text(), "无数据")
            self.assertEqual(dialog.output_label.toolTip(), dialog.output_label.text())
            self.assertEqual(
                dialog.output_label.sizePolicy().horizontalPolicy(),
                QSizePolicy.Policy.Ignored,
            )
            close_button = dialog.findChild(QDialogButtonBox).button(
                QDialogButtonBox.StandardButton.Close
            )
            self.assertEqual(close_button.text(), "关闭")
            self.assertEqual(close_button.property("uiRole"), "secondary")
            self.assertIn(DEFAULT_THEME.colors.primary, dialog.styleSheet())
        finally:
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
