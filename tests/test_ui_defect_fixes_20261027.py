import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import QPointF, QSettings, Qt
from PyQt6.QtWidgets import QApplication

from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


class UiDefectFixes20261027Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        settings = QSettings(
            os.path.join(self.temporary.name, "ui-defect-fixes.ini"),
            QSettings.Format.IniFormat,
        )
        self.window = MDFPlotter(gui_settings=settings)
        self.window.resize(1200, 800)
        self.window.signals = {
            f"signal_{index}": {
                "name": f"signal_{index}",
                "display_name": f"Signal {index}",
                "comment": f"comment {index}",
                "bus_id": None,
            }
            for index in range(3)
        }
        timestamps = np.linspace(0.0, 20.0, 201)
        self.window.can_parsed_data = {
            key: {
                "timestamps": timestamps,
                "samples": np.sin(timestamps + index),
                "unit": "V",
            }
            for index, key in enumerate(self.window.signals)
        }
        self.window.refresh_signal_list_ui()
        for row in range(self.window.signal_list_widget.count()):
            self.window.signal_list_widget.item(row).setCheckState(
                Qt.CheckState.Checked
            )
        self.window.show()
        APP.processEvents()

    def tearDown(self):
        self.window.hide()
        self.window.deleteLater()
        APP.processEvents()
        self.temporary.cleanup()

    def _selected_keys(self):
        return self.window.get_selected_signals()

    def test_left_file_label_tracks_current_file_at_top_of_left_panel(self):
        self.window.set_current_file_status(r"D:\recordings\vehicle_run_2026.mf4")
        APP.processEvents()

        self.assertEqual(
            self.window.left_current_file_label.fullText(),
            "当前文件: vehicle_run_2026.mf4",
        )
        self.assertEqual(
            self.window.left_current_file_label.toolTip(),
            r"D:\recordings\vehicle_run_2026.mf4",
        )
        label_bottom = self.window.left_current_file_label.mapTo(
            self.window, self.window.left_current_file_label.rect().bottomLeft()
        ).y()
        tabs_top = self.window.left_tabs.mapTo(
            self.window, self.window.left_tabs.rect().topLeft()
        ).y()
        self.assertLess(label_bottom, tabs_top)

    def test_height_control_changes_actual_plot_height_both_directions(self):
        self.window.plot_selected_signals()
        APP.processEvents()

        self.window._plot_height_changed(240)
        APP.processEvents()
        self.assertTrue(all(plot.height() == 240 for plot in self.window.plot_widgets))

        self.window._plot_height_changed(110)
        APP.processEvents()
        self.assertTrue(all(plot.height() == 110 for plot in self.window.plot_widgets))

    def test_overlay_hidden_signal_leaves_legend_and_title(self):
        self.window.plot_mode_combo.setCurrentIndex(
            self.window.plot_mode_combo.findData("overlay")
        )
        self.window.plot_selected_signals()
        APP.processEvents()
        plot = self.window.plot_widgets[0]
        hidden_key = self._selected_keys()[0]
        self.assertEqual(len(plot.legend_item.items), 3)
        self.assertEqual(
            plot.getPlotItem().titleLabel.item.toPlainText(),
            "叠加显示 · 3 个信号",
        )

        self.window._set_signal_curve_visibility(hidden_key, False)
        APP.processEvents()
        self.assertEqual(len(plot.legend_item.items), 2)
        self.assertEqual(
            plot.getPlotItem().titleLabel.item.toPlainText(),
            "叠加显示 · 2 个信号（已隐藏 1）",
        )

        self.window.plot_selected_signals()
        APP.processEvents()
        plot = self.window.plot_widgets[0]
        self.assertEqual(len(plot.legend_item.items), 2)
        self.assertEqual(
            plot.getPlotItem().titleLabel.item.toPlainText(),
            "叠加显示 · 2 个信号（已隐藏 1）",
        )

        self.window._set_signal_curve_visibility(hidden_key, True)
        APP.processEvents()
        self.assertEqual(len(plot.legend_item.items), 3)
        self.assertEqual(
            plot.getPlotItem().titleLabel.item.toPlainText(),
            "叠加显示 · 3 个信号",
        )

    def test_hidden_signal_stays_hidden_after_plot_button_redraw(self):
        self.window.plot_selected_signals()
        APP.processEvents()
        hidden_key = self._selected_keys()[0]

        self.window._set_signal_curve_visibility(hidden_key, False)
        self.window.plot_selected_signals()
        APP.processEvents()

        hidden_plots = [
            plot for plot in self.window.plot_widgets
            if any(
                record["signal_key"] == hidden_key
                for record in plot.signal_records
            )
        ]
        self.assertEqual(len(hidden_plots), 1)
        self.assertFalse(hidden_plots[0].isVisible())
        self.assertEqual(
            sum(1 for plot in self.window.plot_widgets if plot.isVisible()),
            2,
        )

        self.window._set_signal_curve_visibility(hidden_key, True)
        APP.processEvents()
        self.assertTrue(hidden_plots[0].isVisible())

    def test_curve_tools_are_usable_and_dual_cursor_delta_moves_to_selected_panel(self):
        self.window.plot_selected_signals()
        self.window.left_tabs.setCurrentWidget(self.window.selected_signals_page)
        self.window.dual_cursor_button.click()
        self.window._cursor_a_time = 3.0
        self.window._cursor_b_time = 7.0
        self.window._update_measurement_display()
        APP.processEvents()

        self.assertGreaterEqual(self.window.box_zoom_button.width(), 92)
        self.assertGreaterEqual(self.window.box_zoom_button.height(), 32)
        self.assertGreaterEqual(self.window.reset_zoom_button.width(), 92)
        self.assertGreaterEqual(self.window.reset_zoom_button.height(), 32)

        summary = self.window.measurement_bar
        self.assertIs(summary.parentWidget(), self.window.selected_signals_page)
        self.assertEqual(
            self.window.selected_signals_layout.indexOf(summary),
            0,
        )
        self.assertTrue(summary.isVisible())
        self.assertGreaterEqual(summary.width(), 240)
        self.assertIn("A: 3.000000 s", summary.text())
        self.assertIn("B: 7.000000 s", summary.text())
        self.assertIn("Δt: 4.000000 s", summary.text())

        value_font_height = self.window.selected_signal_list_widget.fontMetrics().height()
        summary_font_height = summary.fontMetrics().height()
        self.assertGreater(summary_font_height, value_font_height)
        self.assertLessEqual(summary_font_height - value_font_height, 2)

    def test_cursor_readout_switches_left_in_right_thirty_percent(self):
        self.window.single_cursor_button.click()
        self.window.plot_selected_signals()
        APP.processEvents()
        plot = self.window.plot_widgets[0]
        view_box = plot.getViewBox()
        view_box.setXRange(0.0, 20.0, padding=0)
        sample = plot.signal_records[0]["signal_data"]
        label = plot.cursor_labels["single"][0]

        for cursor_time, expect_left in ((5.0, False), (18.0, True)):
            self.window._single_cursor_time = cursor_time
            self.window._render_single_cursor_at(cursor_time)
            APP.processEvents()
            index = int(np.searchsorted(sample.timestamps, cursor_time))
            anchor = view_box.mapViewToScene(
                QPointF(cursor_time, float(sample.samples[index]))
            )
            label_rect = label.sceneBoundingRect()
            if expect_left:
                self.assertLess(label_rect.right(), anchor.x())
            else:
                self.assertGreater(label_rect.left(), anchor.x())


if __name__ == "__main__":
    unittest.main()
