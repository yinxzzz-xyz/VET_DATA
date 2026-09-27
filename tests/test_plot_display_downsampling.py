import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtWidgets import QApplication

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

    @staticmethod
    def _render(plot):
        APP.processEvents()
        plot.grab()
        APP.processEvents()
        return plot.data_curve._getDisplayDataset()

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
        samples[target_index] = 37.125
        plot = self._populate(timestamps, samples)
        plot.setXRange(0.0, float(timestamps[-1]), padding=0)
        self._render(plot)
        self.assertLess(len(plot.data_curve._getDisplayDataset().x), len(timestamps))

        scene_point = self.window.master_viewbox.mapViewToScene(
            QPointF(float(timestamps[target_index]), 0.0)
        )
        self.window.update_cursor_positions(scene_point)
        cursor_x, cursor_y = plot.spot_item.getData()

        self.assertEqual(float(cursor_x[0]), float(timestamps[target_index]))
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


if __name__ == "__main__":
    unittest.main()
