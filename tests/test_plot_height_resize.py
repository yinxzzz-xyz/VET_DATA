import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6 import sip
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QWidget

from vet_data_modular.plot_panel import PlotPanelMixin
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


class _HeightOnlyWidget:
    def __init__(self):
        self.values = []

    def setMinimumHeight(self, value):
        self.values.append(value)


class _HeightHarness(PlotPanelMixin):
    def __init__(self, count):
        self.plot_min_height = 150
        self.plot_widgets = [_HeightOnlyWidget() for _ in range(count)]
        self.plot_selected_signals = Mock()


class PlotHeightResizeUnitTests(unittest.TestCase):
    def test_zero_one_ten_and_fifty_widgets_resize_without_replot(self):
        for count in (0, 1, 10, 50):
            with self.subTest(count=count):
                harness = _HeightHarness(count)
                identities = tuple(map(id, harness.plot_widgets))

                for value in (240, 100, 360):
                    harness._plot_height_changed(value)

                self.assertEqual(harness.plot_min_height, 360)
                self.assertEqual(tuple(map(id, harness.plot_widgets)), identities)
                self.assertEqual(
                    [widget.values for widget in harness.plot_widgets],
                    [[240, 100, 360]] * count,
                )
                harness.plot_selected_signals.assert_not_called()

    def test_missing_or_destroyed_plot_is_safe_during_shutdown(self):
        destroyed = QWidget()
        sip.delete(destroyed)
        harness = _HeightHarness(0)
        harness.plot_widgets = [None, destroyed]

        harness._plot_height_changed(210)

        self.assertEqual(harness.plot_min_height, 210)
        harness.plot_selected_signals.assert_not_called()


class PlotHeightResizeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.window = MDFPlotter()
        self.window.resize(1200, 800)
        self.window.show()
        APP.processEvents()

    def tearDown(self):
        self.window.hide()
        self.window.deleteLater()
        APP.processEvents()

    def _populate(self, count, points=101):
        timestamps = np.linspace(0.0, 10.0, points)
        samples = np.sin(timestamps)
        self.window.signals = {}
        self.window.can_parsed_data = {}
        self.window.can_bus_data = {0: {"name": "Bus 0"}}
        for index in range(count):
            key = f"CAN_SIGNAL_{index:03d}"
            self.window.signals[key] = {
                "name": key,
                "display_name": f"Signal {index:03d}",
                "comment": f"comment {index}",
                "unit": "V",
                "bus_id": 0,
            }
            self.window.can_parsed_data[key] = {
                "timestamps": timestamps,
                "samples": samples,
                "unit": "V",
            }
        self.window.refresh_signal_list_ui()
        for row in range(self.window.signal_list_widget.count()):
            self.window.signal_list_widget.item(row).setCheckState(Qt.CheckState.Checked)
        return timestamps, samples

    @staticmethod
    def _data_curve(plot):
        return next(item for item in plot.listDataItems() if item is not plot.spot_item)

    def test_height_preserves_plot_curve_cursor_data_ranges_and_selection(self):
        self._populate(1)
        original_get_signal = self.window._get_signal
        self.window._get_signal = Mock(wraps=original_get_signal)
        self.window.plot_selected_signals()
        self.window._get_signal.reset_mock()

        plot = self.window.plot_widgets[0]
        curve = self._data_curve(plot)
        view_box = plot.getViewBox()
        view_box.setXRange(2.0, 4.0, padding=0)
        view_box.setYRange(-0.25, 0.25, padding=0)
        plot.vLine.setPos(3.0)
        plot.spot_item.setData([3.0], [0.125])
        plot.text_item.setPos(3.0, 0.2)
        plot.text_item.setText("cursor text")
        plot.stats_text_item.setText("stats text")
        APP.processEvents()

        signal = plot.signal_data
        timestamps = signal.timestamps
        samples = signal.samples
        before_range = np.asarray(view_box.viewRange(), dtype=float)
        before_spot = tuple(np.asarray(values).copy() for values in plot.spot_item.getData())
        identities = {
            "plot": id(plot),
            "curve": id(curve),
            "vline": id(plot.vLine),
            "spot": id(plot.spot_item),
            "text": id(plot.text_item),
            "stats": id(plot.stats_text_item),
            "signal": id(signal),
            "timestamps": id(timestamps),
            "samples": id(samples),
        }

        for value in (240, 100, 360):
            self.window._plot_height_changed(value)
        APP.processEvents()

        self.assertEqual(self.window._get_signal.call_count, 0)
        self.assertEqual(len(self.window.plot_widgets), 1)
        self.assertEqual(id(self.window.plot_widgets[0]), identities["plot"])
        self.assertEqual(id(self._data_curve(plot)), identities["curve"])
        self.assertEqual(id(plot.vLine), identities["vline"])
        self.assertEqual(id(plot.spot_item), identities["spot"])
        self.assertEqual(id(plot.text_item), identities["text"])
        self.assertEqual(id(plot.stats_text_item), identities["stats"])
        self.assertEqual(id(plot.signal_data), identities["signal"])
        self.assertEqual(id(plot.signal_data.timestamps), identities["timestamps"])
        self.assertEqual(id(plot.signal_data.samples), identities["samples"])
        np.testing.assert_allclose(view_box.viewRange(), before_range)
        self.assertEqual(plot.vLine.value(), 3.0)
        after_spot = plot.spot_item.getData()
        np.testing.assert_array_equal(after_spot[0], before_spot[0])
        np.testing.assert_array_equal(after_spot[1], before_spot[1])
        self.assertEqual(plot.text_item.textItem.toPlainText(), "cursor text")
        self.assertEqual(plot.stats_text_item.textItem.toPlainText(), "stats text")
        self.assertEqual(plot.minimumHeight(), 360)
        self.assertEqual(self.window.get_selected_signals(), ["CAN_SIGNAL_000"])
        self.assertEqual(plot.signal_info["display_name"], "Signal 000")
        self.assertEqual(plot.signal_info["comment"], "comment 0")
        self.assertEqual(plot.signal_data.unit, "V")

    def test_ten_and_fifty_real_plots_keep_all_object_identities(self):
        for count in (10, 50):
            with self.subTest(count=count):
                self._populate(count, points=11)
                original_get_signal = self.window._get_signal
                self.window._get_signal = Mock(wraps=original_get_signal)
                self.window.plot_selected_signals()
                self.window._get_signal.reset_mock()
                plots = tuple(self.window.plot_widgets)
                curves = tuple(self._data_curve(plot) for plot in plots)
                cursors = tuple(plot.vLine for plot in plots)

                self.window._plot_height_changed(275)

                self.assertEqual(self.window._get_signal.call_count, 0)
                self.assertEqual(tuple(self.window.plot_widgets), plots)
                self.assertEqual(tuple(self._data_curve(plot) for plot in plots), curves)
                self.assertEqual(tuple(plot.vLine for plot in plots), cursors)
                self.assertTrue(all(plot.minimumHeight() == 275 for plot in plots))

    def test_new_plot_uses_height_selected_before_any_plot_exists(self):
        self.assertFalse(self.window.plot_widgets)
        self.window._plot_height_changed(285)
        self._populate(1)

        self.window.plot_selected_signals()

        self.assertEqual(len(self.window.plot_widgets), 1)
        self.assertEqual(self.window.plot_widgets[0].minimumHeight(), 285)


if __name__ == "__main__":
    unittest.main()
