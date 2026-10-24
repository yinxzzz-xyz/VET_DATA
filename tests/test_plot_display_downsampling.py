import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest

from vet_data_modular.signal_panel import INTERVAL_ROLE, KEY_ROLE, MEASUREMENT_ROLE
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


class PlotDisplayDownsamplingTests(unittest.TestCase):
    def setUp(self):
        self.window = MDFPlotter()
        self.window.resize(1200, 800)
        self.window.show()
        APP.processEvents()

    def tearDown(self):
        self.window.hide()
        self.window.deleteLater()
        APP.processEvents()

    def _populate(self, timestamps, samples, *, key="CAN_SIGNAL", display_name="Signal"):
        self.window.signals = {
            key: {
                "name": key,
                "display_name": display_name,
                "comment": "display test",
                "bus_id": 0,
            }
        }
        self.window.can_parsed_data = {
            key: {
                "timestamps": timestamps,
                "samples": samples,
                "unit": "V",
            }
        }
        self.window.can_bus_data = {0: {"name": "Bus 0"}}
        self.window.refresh_signal_list_ui()
        self.window.signal_list_widget.item(0).setCheckState(Qt.CheckState.Checked)
        self.window.plot_selected_signals()
        self.assertEqual(len(self.window.plot_widgets), 1)
        plot = self.window.plot_widgets[0]
        plot.resize(1000, 400)
        APP.processEvents()
        return plot

    def _drag_cursor(self, plot, name, target_time):
        if name == "single":
            line = plot.vLine
        else:
            line = plot.measurement_lines[name]
        view_box = plot.getViewBox()
        y_min, y_max = view_box.viewRange()[1]
        center_y = (y_min + y_max) * 0.5
        start = plot.mapFromScene(
            view_box.mapViewToScene(QPointF(line.value(), center_y))
        )
        end = plot.mapFromScene(
            view_box.mapViewToScene(QPointF(float(target_time), center_y))
        )
        QTest.mousePress(plot.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(plot.viewport(), end, 10)
        QTest.mouseRelease(plot.viewport(), Qt.MouseButton.LeftButton, pos=end)
        APP.processEvents()

    @staticmethod
    def _global_x_for_time(plot, time_value):
        view_box = plot.getViewBox()
        y_min, y_max = view_box.viewRange()[1]
        scene_point = view_box.mapViewToScene(
            QPointF(float(time_value), (y_min + y_max) * 0.5)
        )
        return plot.mapToGlobal(plot.mapFromScene(scene_point)).x()

    @staticmethod
    def _render(plot):
        APP.processEvents()
        plot.grab()
        APP.processEvents()
        return plot.data_curve._getDisplayDataset()

    def test_signal_title_font_is_smaller_only_in_independent_mode(self):
        plot = self._populate(np.linspace(0.0, 10.0, 11), np.arange(11.0))
        title = plot.getPlotItem().titleLabel

        self.assertEqual(title.opts["size"], "10pt")
        self.assertEqual(title.item.toPlainText(), "信号: Signal [Bus 0]")

        overlay_index = self.window.plot_mode_combo.findData("overlay")
        self.window.plot_mode_combo.setCurrentIndex(overlay_index)
        APP.processEvents()

        overlay_title = self.window.plot_widgets[0].getPlotItem().titleLabel
        self.assertEqual(overlay_title.opts["size"], "11pt")
        self.assertEqual(overlay_title.item.toPlainText(), "信号: Signal [Bus 0]")

    def test_numeric_curve_uses_peak_auto_downsampling_and_clip_without_mutating_signal(self):
        timestamps = np.linspace(0.0, 1000.0, 200_000, dtype=np.float64)
        samples = np.sin(timestamps).astype(np.float32)
        plot = self._populate(timestamps, samples)
        signal = plot.signal_data
        signal_timestamps = signal.timestamps
        signal_samples = signal.samples
        timestamp_snapshot = signal_timestamps.copy()
        sample_snapshot = signal_samples.copy()

        plot.setXRange(0.0, 1000.0, padding=0)
        display = self._render(plot)

        self.assertTrue(plot.data_curve.opts["autoDownsample"])
        self.assertTrue(plot.data_curve.opts["clipToView"])
        self.assertEqual(plot.data_curve.opts["downsampleMethod"], "peak")
        self.assertLess(len(display.x), len(signal_timestamps))
        self.assertEqual(len(signal_timestamps), 200_000)
        self.assertEqual(len(signal_samples), 200_000)
        self.assertEqual(signal_samples.dtype, np.float64)
        self.assertIs(plot.signal_data.timestamps, signal_timestamps)
        self.assertIs(plot.signal_data.samples, signal_samples)
        np.testing.assert_array_equal(signal_timestamps, timestamp_snapshot)
        np.testing.assert_array_equal(signal_samples, sample_snapshot)

    def test_large_y_values_expand_axis_and_use_slightly_smaller_tick_font(self):
        samples = np.linspace(1412.0, 2721.5, 101)
        plot = self._populate(np.linspace(0.0, 10.0, len(samples)), samples)
        axis = plot.getPlotItem().getAxis("left")
        APP.processEvents()
        plot.grab()
        APP.processEvents()

        self.assertGreater(axis.width(), 30)
        self.assertGreaterEqual(axis.width(), axis.textWidth)
        self.assertIsNotNone(axis.style["tickFont"])
        self.assertAlmostEqual(axis.style["tickFont"].pointSizeF(), 8.0)
        self.assertTrue(axis.style["autoExpandTextSpace"])
        for time_value in (2.0, 5.0, 8.0):
            self.assertLessEqual(
                abs(
                    self._global_x_for_time(plot, time_value)
                    - self._global_x_for_time(
                        self.window.shared_time_axis_widget, time_value
                    )
                ),
                1,
            )

    def test_peak_pulse_step_oscillation_and_nan_gap_visual_semantics(self):
        count = 240_000
        timestamps = np.linspace(0.0, 240.0, count, dtype=np.float64)
        samples = np.zeros(count, dtype=np.float64)
        samples[30_000] = 50.0
        samples[60_000] = -40.0
        samples[90_000:90_003] = 30.0
        samples[120_000:] = 10.0
        samples[160_000:180_000] = np.where(np.arange(20_000) % 2, 8.0, 12.0)
        samples[200_000:202_000] = np.nan
        plot = self._populate(timestamps, samples)
        plot.setXRange(0.0, 240.0, padding=0)

        display = self._render(plot)

        self.assertEqual(float(np.nanmax(display.y)), 50.0)
        self.assertEqual(float(np.nanmin(display.y)), -40.0)
        pulse_positions = display.x[display.y == 30.0]
        self.assertGreater(len(pulse_positions), 0)
        self.assertLess(
            float(np.min(np.abs(pulse_positions - timestamps[90_001]))),
            0.1,
        )
        self.assertTrue(np.any(display.y == 0.0))
        self.assertTrue(np.any(display.y == 10.0))
        self.assertTrue(np.any(display.y == 8.0))
        self.assertTrue(np.any(display.y == 12.0))
        gap = (display.x >= timestamps[200_000]) & (display.x <= timestamps[201_999])
        self.assertTrue(np.any(np.isnan(display.y[gap])))
        self.assertFalse(plot.data_curve.opts["skipFiniteCheck"])
        np.testing.assert_array_equal(plot.signal_data.samples, samples)

    def test_zoom_in_recovers_local_detail_and_zoom_out_reduces_display_points(self):
        timestamps = np.linspace(0.0, 1000.0, 1_000_000, dtype=np.float64)
        samples = np.sin(timestamps * 5.0)
        plot = self._populate(timestamps, samples)

        plot.setXRange(0.0, 1000.0, padding=0)
        global_data = self._render(plot)
        global_count = len(global_data.x)
        global_ratio = global_count / len(timestamps)

        plot.setXRange(490.0, 510.0, padding=0)
        zoom_data = self._render(plot)
        raw_local_count = int(np.count_nonzero((timestamps >= 490.0) & (timestamps <= 510.0)))
        zoom_ratio = len(zoom_data.x) / raw_local_count

        plot.setXRange(0.0, 1000.0, padding=0)
        zoomed_out = self._render(plot)

        self.assertGreater(zoom_ratio, global_ratio)
        self.assertGreater(len(zoom_data.x), 1000)
        self.assertLess(len(zoomed_out.x), len(timestamps))
        self.assertLess(len(zoomed_out.x), raw_local_count)
        self.assertLessEqual(abs(len(zoomed_out.x) - global_count), max(10, global_count // 4))
        self.assertEqual(len(plot.signal_data.timestamps), 1_000_000)

    def test_cursor_and_statistics_continue_to_use_full_resolution_signal(self):
        timestamps = np.arange(200_000, dtype=np.float64)
        samples = np.zeros(200_000, dtype=np.float64)
        target_index = 123_456
        samples[target_index:target_index + 2] = 37.125
        plot = self._populate(timestamps, samples)
        plot.setXRange(0.0, float(timestamps[-1]), padding=0)
        self._render(plot)
        self.assertLess(len(plot.data_curve._getDisplayDataset().x), len(timestamps))
        plot.setXRange(target_index - 5, target_index + 5, padding=0)
        APP.processEvents()
        self.window.single_cursor_button.click()
        self._drag_cursor(plot, "single", float(timestamps[target_index]))
        cursor_x, cursor_y = plot.spot_item.getData()

        self.assertAlmostEqual(float(cursor_x[0]), float(timestamps[target_index]), delta=1.0)
        self.assertEqual(float(cursor_y[0]), float(samples[target_index]))
        self.assertIn("37.125", plot.text_item.textItem.toPlainText())

        plot.setXRange(float(target_index - 2), float(target_index + 2), padding=0)
        self.window._update_stats(plot)
        self.assertIn("37.125", plot.stats_text_item.textItem.toPlainText())
        self.assertIs(plot.signal_data.timestamps, plot.signal_data.timestamps)
        self.assertEqual(len(plot.signal_data.samples), 200_000)

    def test_text_enum_curve_keeps_full_resolution_behavior(self):
        timestamps = np.arange(10_000, dtype=np.float64)
        samples = np.where(np.arange(10_000) % 2, "ON", "OFF")
        plot = self._populate(timestamps, samples, key="STATE", display_name="State")

        self.assertTrue(plot.is_text_signal)
        self.assertFalse(plot.data_curve.opts["autoDownsample"])
        self.assertFalse(plot.data_curve.opts["clipToView"])
        self.assertEqual(plot.signal_data.text_mapping, ["OFF", "ON"])
        self.assertEqual(len(plot.signal_data.raw_text_values), 10_000)
        self.assertEqual(len(plot.data_curve._getDisplayDataset().x), 10_000)

    def test_overlay_mode_uses_one_plot_for_all_curves_and_preserves_interactions(self):
        timestamps = np.linspace(0.0, 10.0, 101)
        self.window.signals = {
            "speed": {
                "name": "speed", "display_name": "Vehicle Speed",
                "comment": "", "bus_id": None,
            },
            "rpm": {
                "name": "rpm", "display_name": "Engine RPM",
                "comment": "", "bus_id": None,
            },
        }
        self.window.can_parsed_data = {
            "speed": {"timestamps": timestamps, "samples": timestamps, "unit": "km/h"},
            "rpm": {"timestamps": timestamps, "samples": timestamps * 100, "unit": "rpm"},
        }
        self.window.refresh_signal_list_ui()
        for row in range(self.window.signal_list_widget.count()):
            self.window.signal_list_widget.item(row).setCheckState(Qt.CheckState.Checked)
        selected_before = self.window.get_selected_signals()
        self.window.plot_selected_signals()

        self.window.plot_mode_combo.setCurrentIndex(
            self.window.plot_mode_combo.findData("overlay")
        )
        APP.processEvents()
        self.assertEqual(self.window.plot_mode_combo.count(), 2)
        self.assertEqual(
            [self.window.plot_mode_combo.itemData(index) for index in range(2)],
            ["independent", "overlay"],
        )
        self.assertEqual(len(self.window.plot_widgets), 1)
        plot = self.window.plot_widgets[0]
        records = plot.signal_records
        self.assertEqual([record["signal_key"] for record in records], selected_before)
        self.assertEqual(len(records), 2)
        self.assertEqual(len({id(record["data_curve"]) for record in records}), 2)
        plot_items = plot.listDataItems()
        self.assertTrue(all(record["data_curve"] in plot_items for record in records))
        self.assertTrue(all(
            record["data_curve"].getViewBox() is plot.getViewBox()
            for record in records
        ))
        self.assertEqual(
            {record["signal_key"]: record["data_curve"].opts["pen"].color()
             for record in records},
            {key: self.window._signal_color(key) for key in selected_before},
        )
        self.assertIsNotNone(plot.legend_item)
        self.assertEqual(
            {label.toolTip() for _, label in plot.legend_item.items},
            {"Vehicle Speed", "Engine RPM"},
        )
        legend_colors = {
            label.toolTip(): sample.item.opts["pen"].color()
            for sample, label in plot.legend_item.items
        }
        self.assertEqual(
            legend_colors,
            {
                record["signal_info"]["display_name"]:
                    record["data_curve"].opts["pen"].color()
                for record in records
            },
        )
        self.assertEqual(
            plot.getPlotItem().titleLabel.item.toPlainText(),
            "叠加显示 · 2 个信号",
        )
        self.assertEqual(self.window.get_selected_signals(), selected_before)
        self.assertEqual(plot.stats_text_item.isVisible(), True)

        selected_list = self.window.selected_signal_list_widget
        moved_item = selected_list.takeItem(1)
        selected_list.insertItem(0, moved_item)
        self.window._selected_signal_rows_moved()
        self.window.plot_selected_signals()
        plot = self.window.plot_widgets[0]
        records = plot.signal_records
        self.assertEqual(
            [record["signal_key"] for record in records],
            self.window.get_selected_signals(),
        )
        self.assertEqual(self.window.get_selected_signals(), list(reversed(selected_before)))
        selected_before = self.window.get_selected_signals()

        self.window.master_viewbox.setXRange(3.0, 7.0, padding=0)
        APP.processEvents()
        np.testing.assert_allclose(
            self.window.shared_time_axis_widget.getViewBox().viewRange()[0],
            self.window.master_viewbox.viewRange()[0],
        )

        self.window.single_cursor_button.click()
        self._drag_cursor(plot, "single", 5.0)
        self.assertTrue(all(
            len(record["spot_item"].getData()[0]) == 1 for record in records
        ))
        self.assertAlmostEqual(plot.vLine.value(), 5.0, delta=0.02)
        shown_values = []
        for label in plot.cursor_labels["single"]:
            shown_time, shown_value = label.textItem.toPlainText().split(", ")
            self.assertAlmostEqual(float(shown_time), 5.0, delta=0.02)
            shown_values.append(float(shown_value))
        self.assertCountEqual(shown_values, (5.1, 510.0))
        self.window.single_cursor_button.click()
        self.assertFalse(plot.vLine.isVisible())
        self.assertTrue(all(not record["spot_item"].isVisible() for record in records))

        self.window.dual_cursor_button.click()
        self.window._cursor_a_time = 3.0
        self.window._cursor_b_time = 7.0
        self.window._update_measurement_display()
        self.assertFalse(self.window.measurement_bar.isHidden())
        for cursor_name, expected in (("A", {"3.000", "300.000"}),
                                      ("B", {"7.000", "700.000"})):
            values = {
                label.textItem.toPlainText().split(", ")[1]
                for label in plot.cursor_labels[cursor_name]
            }
            self.assertEqual(values, expected)
        self.window.dual_cursor_button.click()

        # Removing one selection replots one remaining curve in the same overlay plot.
        self.window.signal_list_widget.item(0).setCheckState(Qt.CheckState.Unchecked)
        self.window.plot_selected_signals()
        self.assertEqual(len(self.window.plot_widgets), 1)
        self.assertEqual(len(self.window.plot_widgets[0].signal_records), 1)
        self.assertEqual(
            self.window.plot_widgets[0].signal_records[0]["signal_key"],
            self.window.get_selected_signals()[0],
        )

        # Independent mode remains one plot per signal; switching preserves selection.
        self.window.signal_list_widget.item(0).setCheckState(Qt.CheckState.Checked)
        self.window.plot_mode_combo.setCurrentIndex(
            self.window.plot_mode_combo.findData("independent")
        )
        self.assertEqual(len(self.window.plot_widgets), 2)
        self.assertTrue(all(len(widget.signal_records) == 1
                            for widget in self.window.plot_widgets))
        self.assertEqual(self.window.get_selected_signals(), selected_before)
        self.window.plot_mode_combo.setCurrentIndex(
            self.window.plot_mode_combo.findData("overlay")
        )
        self.assertEqual(len(self.window.plot_widgets), 1)
        self.assertEqual(len(self.window.plot_widgets[0].signal_records), 2)
        self.window._clear_current_can_state()
        self.assertFalse(self.window.plot_widgets)
        self.assertTrue(self.window.plot_empty_label.isVisible())

    def test_height_change_and_normal_replot_keep_expected_curve_configuration(self):
        timestamps = np.linspace(0.0, 100.0, 100_000)
        samples = np.sin(timestamps)
        plot = self._populate(timestamps, samples)
        curve = plot.data_curve

        self.window._plot_height_changed(275)

        self.assertIs(self.window.plot_widgets[0], plot)
        self.assertIs(plot.data_curve, curve)
        self.assertTrue(curve.opts["autoDownsample"])
        self.assertTrue(curve.opts["clipToView"])
        self.assertEqual(plot.minimumHeight(), 275)

        self.window.plot_selected_signals()
        replacement = self.window.plot_widgets[0]
        self.assertIsNot(replacement, plot)
        self.assertTrue(replacement.data_curve.opts["autoDownsample"])
        self.assertTrue(replacement.data_curve.opts["clipToView"])
        self.assertEqual(replacement.data_curve.opts["downsampleMethod"], "peak")

    def test_cursor_modes_default_off_and_only_drag_moves_cursor(self):
        timestamps = np.arange(0.0, 21.0)
        plot = self._populate(timestamps, timestamps * 2.0)
        self.assertIsNone(self.window.cursor_mode)
        self.assertFalse(self.window.single_cursor_button.isChecked())
        self.assertFalse(self.window.dual_cursor_button.isChecked())
        self.assertEqual(self.window.single_cursor_button.toolTip(), "单游标")
        self.assertEqual(self.window.dual_cursor_button.toolTip(), "双游标")
        self.assertEqual(self.window.single_cursor_button.height(), 36)
        self.assertEqual(self.window.dual_cursor_button.height(), 36)
        self.assertFalse(self.window.single_cursor_button.icon().isNull())
        self.assertFalse(self.window.dual_cursor_button.icon().isNull())
        inactive_single_icon = self.window.single_cursor_button.icon().cacheKey()
        self.assertFalse(plot.vLine.isVisible())
        self.assertTrue(all(not label.isVisible() for label in plot.cursor_labels["single"]))
        self.assertTrue(all(not label.isVisible() for name in ("A", "B") for label in plot.cursor_labels[name]))
        self.assertTrue(all(not line.isVisible() for line in plot.measurement_lines.values()))
        self.assertFalse(self.window.measurement_bar.isVisible())

        start_value = plot.vLine.value()
        y_min, y_max = plot.getViewBox().viewRange()[1]
        no_button_move = plot.mapFromScene(
            plot.getViewBox().mapViewToScene(QPointF(15.0, (y_min + y_max) * 0.5))
        )
        QTest.mouseMove(plot.viewport(), no_button_move, 10)
        APP.processEvents()
        self.assertEqual(plot.vLine.value(), start_value)
        self.assertFalse(plot.vLine.isVisible())

        self.window.single_cursor_button.click()
        self.assertEqual(self.window.cursor_mode, "single")
        self.assertTrue(self.window.single_cursor_button.isChecked())
        self.assertNotEqual(
            self.window.single_cursor_button.icon().cacheKey(), inactive_single_icon
        )
        self.assertFalse(self.window.dual_cursor_button.isChecked())
        self.assertTrue(plot.vLine.isVisible())
        single_pen = plot.vLine.pen
        self.assertEqual(single_pen.color().name(), "#16a34a")
        self.assertEqual(single_pen.style(), Qt.PenStyle.SolidLine)
        initial_time = plot.vLine.value()
        x_min, x_max = plot.getViewBox().viewRange()[0]
        self.assertAlmostEqual(self.window._single_cursor_time, (x_min + x_max) * 0.5, delta=0.05)
        self.assertAlmostEqual(initial_time, self.window._single_cursor_time, delta=1.0)
        QTest.mouseMove(plot.viewport(), no_button_move, 10)
        APP.processEvents()
        self.assertEqual(plot.vLine.value(), initial_time)
        blank_click = plot.mapFromScene(
            plot.getViewBox().mapViewToScene(QPointF(3.0, (y_min + y_max) * 0.5))
        )
        QTest.mouseClick(plot.viewport(), Qt.MouseButton.LeftButton, pos=blank_click)
        self.assertEqual(plot.vLine.value(), initial_time)
        line_point = plot.mapFromScene(
            plot.getViewBox().mapViewToScene(
                QPointF(initial_time, (y_min + y_max) * 0.5)
            )
        )
        QTest.mouseClick(
            plot.viewport(), Qt.MouseButton.LeftButton,
            pos=line_point + QPoint(5, 0),
        )
        self.assertEqual(plot.vLine.value(), initial_time)

        self._drag_cursor(plot, "single", 15.0)
        self.assertAlmostEqual(plot.vLine.value(), 15.0, delta=0.2)
        self.assertRegex(plot.text_item.textItem.toPlainText(), r"^15\.000000, 30\.000000$")
        self.assertIsNone(self.window._dragging_cursor)
        self.window.single_cursor_button.click()
        self.assertIsNone(self.window.cursor_mode)
        self.assertFalse(plot.vLine.isVisible())
        self.assertTrue(all(not spot.isVisible() for spot in plot.cursor_spots))

    def test_cursor_modes_are_mutually_exclusive_and_clear_stale_ab_values(self):
        plot = self._populate(np.arange(0.0, 21.0), np.arange(0.0, 21.0))
        self.window.dual_cursor_button.click()
        self.assertEqual(self.window.cursor_mode, "dual")
        self.assertFalse(self.window.single_cursor_button.isChecked())
        self.assertTrue(self.window.dual_cursor_button.isChecked())
        self.assertTrue(all(line.isVisible() for line in plot.measurement_lines.values()))
        self.assertEqual(set(plot.measurement_lines), {"A", "B"})
        self.assertEqual(plot.measurement_lines["A"].pen.color().name(), "#16a34a")
        self.assertEqual(plot.measurement_lines["B"].pen.color().name(), "#2563eb")
        self.assertTrue(all(
            line.pen.style() == Qt.PenStyle.SolidLine
            for line in plot.measurement_lines.values()
        ))
        x_min, x_max = plot.getViewBox().viewRange()[0]
        initial_a = x_min + (x_max - x_min) / 3.0
        initial_b = x_min + (x_max - x_min) * 2.0 / 3.0
        self.assertAlmostEqual(plot.measurement_lines["A"].value(), initial_a, delta=0.05)
        self.assertAlmostEqual(plot.measurement_lines["B"].value(), initial_b, delta=0.05)
        before_a = plot.measurement_lines["A"].value()
        before_b = plot.measurement_lines["B"].value()
        y_min, y_max = plot.getViewBox().viewRange()[1]
        mouse_point = plot.mapFromScene(
            plot.getViewBox().mapViewToScene(QPointF(18.0, (y_min + y_max) * 0.5))
        )
        QTest.mouseMove(plot.viewport(), mouse_point, 10)
        APP.processEvents()
        self.assertEqual(plot.measurement_lines["A"].value(), before_a)
        self.assertEqual(plot.measurement_lines["B"].value(), before_b)
        QTest.mouseClick(plot.viewport(), Qt.MouseButton.LeftButton, pos=mouse_point)
        self.assertEqual(plot.measurement_lines["A"].value(), before_a)
        self.assertEqual(plot.measurement_lines["B"].value(), before_b)
        self._drag_cursor(plot, "A", 5.0)
        self._drag_cursor(plot, "B", 12.0)
        self.assertAlmostEqual(plot.measurement_lines["A"].value(), 5.0, delta=0.2)
        self.assertAlmostEqual(plot.measurement_lines["B"].value(), 12.0, delta=0.2)
        self.assertIn("A:", self.window.measurement_bar.text())
        for name, expected_time, expected_value in (("A", 5.0, 5.0), ("B", 12.0, 12.0)):
            label_text = plot.cursor_labels[name][0].textItem.toPlainText()
            self.assertEqual(len(label_text.split(", ")), 2)
            shown_time, shown_value = label_text.split(", ")
            self.assertAlmostEqual(float(shown_time), expected_time, delta=0.02)
            self.assertEqual(float(shown_value), expected_value)

        self.window.single_cursor_button.click()
        self.assertEqual(self.window.cursor_mode, "single")
        self.assertTrue(self.window.single_cursor_button.isChecked())
        self.assertFalse(self.window.dual_cursor_button.isChecked())
        self.assertTrue(plot.vLine.isVisible())
        self.assertTrue(all(not line.isVisible() for line in plot.measurement_lines.values()))
        self.assertFalse(self.window.measurement_bar.isVisible())
        selected_item = self.window.selected_signal_list_widget.item(0)
        self.assertIsNone(selected_item.data(MEASUREMENT_ROLE))
        self.assertIsNone(selected_item.data(INTERVAL_ROLE))

        self.window.single_cursor_button.click()
        self.assertIsNone(self.window.cursor_mode)
        self.assertFalse(plot.vLine.isVisible())
        self.assertFalse(self.window.single_cursor_button.isChecked())

        self.window.dual_cursor_button.click()
        self.assertEqual(self.window.cursor_mode, "dual")
        self.window.dual_cursor_button.click()
        self.assertIsNone(self.window.cursor_mode)
        self.assertFalse(self.window.dual_cursor_button.isChecked())

    def test_plot_mode_rebuild_clears_active_drag_without_losing_cursor_mode(self):
        self._populate(np.arange(0.0, 21.0), np.arange(0.0, 21.0))
        self.window.dual_cursor_button.click()
        plot = self.window.plot_widgets[0]
        line = plot.measurement_lines["A"]
        y_min, y_max = plot.getViewBox().viewRange()[1]
        line_point = plot.mapFromScene(
            plot.getViewBox().mapViewToScene(
                QPointF(line.value(), (y_min + y_max) * 0.5)
            )
        )
        QTest.mousePress(plot.viewport(), Qt.MouseButton.LeftButton, pos=line_point)
        self.assertIsNotNone(self.window._dragging_cursor)

        self.window.plot_mode_combo.setCurrentIndex(
            self.window.plot_mode_combo.findData("overlay")
        )
        QTest.mouseRelease(plot.viewport(), Qt.MouseButton.LeftButton, pos=line_point)
        APP.processEvents()

        self.assertIsNone(self.window._dragging_cursor)
        self.assertEqual(self.window.cursor_mode, "dual")
        self.assertTrue(self.window.dual_cursor_button.isChecked())
        self.assertTrue(all(
            line.isVisible()
            for line in self.window.plot_widgets[0].measurement_lines.values()
        ))

    def test_box_zoom_mode_and_reset_are_available(self):
        plot = self._populate(np.linspace(0.0, 20.0, 201), np.sin(np.linspace(0.0, 20.0, 201)))
        view_box = plot.getViewBox()
        full_range = tuple(view_box.viewRange()[0])

        self.window.box_zoom_button.setChecked(True)
        self.assertEqual(view_box.state["mouseMode"], view_box.RectMode)
        self.assertFalse(view_box.state["mouseEnabled"][1])

        view_box.setXRange(5.0, 8.0, padding=0)
        self.window.reset_plot_view()
        APP.processEvents()

        self.assertFalse(self.window.box_zoom_button.isChecked())
        self.assertEqual(view_box.state["mouseMode"], view_box.PanMode)
        self.assertTrue(view_box.state["mouseEnabled"][1])
        np.testing.assert_allclose(view_box.viewRange()[0], full_range, atol=0.1)
        np.testing.assert_allclose(
            self.window.shared_time_axis_widget.getViewBox().viewRange()[0],
            self.window.master_viewbox.viewRange()[0],
        )

    def test_ab_measurement_cursor_updates_times_and_per_signal_delta(self):
        timestamps = np.arange(0.0, 21.0)
        plot = self._populate(timestamps, timestamps * 2.0)
        self.window.left_tabs.setCurrentWidget(self.window.selected_signals_page)
        self.window.dual_cursor_button.click()
        self._drag_cursor(plot, "A", 5.0)
        self._drag_cursor(plot, "B", 12.0)

        self.assertAlmostEqual(self.window._cursor_a_time, 5.0, delta=0.02)
        self.assertAlmostEqual(self.window._cursor_b_time, 12.0, delta=0.02)
        self.assertAlmostEqual(
            abs(self.window._cursor_b_time - self.window._cursor_a_time), 7.0, delta=0.02
        )
        self.assertAlmostEqual(plot.measurement_lines["A"].value(), 5.0, delta=0.02)
        self.assertAlmostEqual(plot.measurement_lines["B"].value(), 12.0, delta=0.02)
        item = next(
            self.window.selected_signal_list_widget.item(row)
            for row in range(self.window.selected_signal_list_widget.count())
            if self.window.selected_signal_list_widget.item(row).data(KEY_ROLE) == "CAN_SIGNAL"
        )
        self.assertEqual(item.data(MEASUREMENT_ROLE), "A 10.000  B 24.000  Δ +14.000")
        self.assertEqual(
            item.data(INTERVAL_ROLE),
            f"区间 min 10.000  max 24.000  平均 17.000  RMS {np.sqrt(np.mean(np.square(np.arange(10.0, 25.0, 2.0)))):.3f}",
        )

    def test_dual_cursor_labels_avoid_overlap_and_stay_inside_plot_edges(self):
        plot = self._populate(np.arange(0.0, 21.0), np.arange(0.0, 21.0))
        self.window.dual_cursor_button.click()
        self.window._cursor_a_time = 0.0
        self.window._cursor_b_time = 0.0
        self.window._update_measurement_display()
        APP.processEvents()

        a_rect = plot.cursor_labels["A"][0].sceneBoundingRect()
        b_rect = plot.cursor_labels["B"][0].sceneBoundingRect()
        plot_bounds = plot.getViewBox().sceneBoundingRect()
        self.assertFalse(a_rect.intersects(b_rect))
        self.assertTrue(plot_bounds.contains(a_rect))
        self.assertTrue(plot_bounds.contains(b_rect))

    def test_plot_stack_uses_available_height_and_shared_axis_stays_at_panel_bottom(self):
        timestamps = np.linspace(0.0, 20.0, 201)
        self.window.signals = {
            f"signal_{index}": {
                "name": f"signal_{index}",
                "display_name": f"Signal {index}",
                "comment": "",
                "bus_id": None,
            }
            for index in range(4)
        }
        self.window.can_parsed_data = {
            key: {"timestamps": timestamps, "samples": timestamps * (i + 1), "unit": "V"}
            for i, key in enumerate(self.window.signals)
        }
        self.window.refresh_signal_list_ui()
        for row in range(self.window.signal_list_widget.count()):
            self.window.signal_list_widget.item(row).setCheckState(Qt.CheckState.Checked)

        def assert_axis_bottom():
            APP.processEvents()
            right_panel = self.window.main_splitter.widget(1)
            axis_row = self.window.shared_time_axis_row
            axis = self.window.shared_time_axis_widget
            self.assertLessEqual(axis_row.height(), axis.height() + 2)
            self.assertEqual(axis_row.height(), axis.height())
            self.assertGreater(axis_row.geometry().top(), self.window.plot_scroll_area.geometry().top())
            bottom_gap = right_panel.height() - axis_row.geometry().bottom() - 1
            self.assertLessEqual(bottom_gap, 16)
            self.assertGreaterEqual(bottom_gap, 0)

        self.window.plot_selected_signals()
        assert_axis_bottom()
        viewport_height = self.window.plot_scroll_area.viewport().height()
        self.assertEqual(len(self.window.plot_widgets), 4)
        self.assertTrue(self.window.plot_scroll_area.verticalScrollBar().isVisible())
        self.assertTrue(all(plot.height() >= 150 for plot in self.window.plot_widgets))
        self.assertGreater(self.window.plot_container.height(), viewport_height)
        for plot in self.window.plot_widgets:
            for time_value in (5.0, 15.0):
                self.assertLessEqual(
                    abs(
                        self._global_x_for_time(plot, time_value)
                        - self._global_x_for_time(
                            self.window.shared_time_axis_widget, time_value
                        )
                    ),
                    1,
                )

        for key in tuple(self.window.signals)[2:]:
            self.window.signal_widgets[key]["checkbox"].setChecked(False)
        self.window.plot_selected_signals()
        assert_axis_bottom()
        self.assertEqual(len(self.window.plot_widgets), 2)
        self.assertFalse(self.window.plot_scroll_area.verticalScrollBar().isVisible())
        self.assertGreaterEqual(
            sum(plot.height() for plot in self.window.plot_widgets),
            self.window.plot_scroll_area.viewport().height() - 30,
        )

        self.window.signal_widgets["signal_1"]["checkbox"].setChecked(False)
        self.window.plot_selected_signals()
        assert_axis_bottom()
        self.assertEqual(len(self.window.plot_widgets), 1)
        self.assertGreaterEqual(
            self.window.plot_widgets[0].height(),
            self.window.plot_scroll_area.viewport().height() - 30,
        )

        self.window.plot_mode_combo.setCurrentIndex(
            self.window.plot_mode_combo.findData("overlay")
        )
        assert_axis_bottom()
        self.assertEqual(len(self.window.plot_widgets), 1)
        self.assertGreaterEqual(
            self.window.plot_widgets[0].height(),
            self.window.plot_scroll_area.viewport().height() - 30,
        )

        self.window.resize(1200, 500)
        APP.processEvents()
        assert_axis_bottom()
        self.assertGreaterEqual(
            self.window.plot_widgets[0].height(),
            self.window.plot_scroll_area.viewport().height() - 30,
        )

    def test_interval_statistics_ignore_nan_and_handle_empty_and_text_data(self):
        class Signal:
            timestamps = np.arange(5.0)
            samples = np.array([1.0, np.nan, 3.0, 5.0, np.inf])

        record = {"signal_data": Signal(), "is_text_signal": False}
        result = self.window._interval_statistics(record, 0.0, 4.0)
        self.assertEqual(result, "区间 min 1.000  max 5.000  平均 3.000  RMS 3.416")

        Signal.samples = np.full(5, np.nan)
        self.assertEqual(
            self.window._interval_statistics(record, 0.0, 4.0),
            "区间统计：无有效样本",
        )
        record["is_text_signal"] = True
        self.assertEqual(
            self.window._interval_statistics(record, 0.0, 4.0),
            "区间统计：文本信号不适用",
        )


if __name__ == "__main__":
    unittest.main()
