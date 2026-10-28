import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pyqtgraph as pg
from PyQt6 import sip
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtWidgets import QApplication, QScrollBar

from vet_data_modular.multi_y_axis import MultiYAxisManager


APP = QApplication.instance() or QApplication([])


class MultiYAxisManagerTests(unittest.TestCase):
    def setUp(self):
        self.plot = pg.PlotWidget()
        self.plot.resize(900, 500)
        self.plot.show()
        APP.processEvents()
        self.reference = self.plot.getViewBox()
        self.manager = MultiYAxisManager(self.reference, self.plot.scene())

    def tearDown(self):
        self.manager.close(preserve_state=False)
        APP.processEvents()

    def _register_three(self):
        ranges = {
            "vehicle_speed": (-2.0, 2.0),
            "engine_rpm": (-200.0, 200.0),
            "battery_current": (-20_000.0, 20_000.0),
        }
        timestamps = np.linspace(0.0, 10.0, 101)
        for index, (signal_key, y_range) in enumerate(ranges.items()):
            view_box = self.manager.register_signal(signal_key, y_range)
            view_box.addItem(pg.PlotDataItem(
                timestamps,
                np.sin(timestamps) * (10 ** (index * 2)),
            ))
        self.manager.set_current_signal("vehicle_speed")
        return ranges

    def test_signals_keep_independent_ranges_and_y_changes_are_isolated(self):
        self._register_three()
        self.assertEqual(
            self.manager.y_range("vehicle_speed"), (-2.0, 2.0)
        )
        self.assertEqual(self.manager.y_range("engine_rpm"), (-200.0, 200.0))
        self.assertEqual(
            self.manager.y_range("battery_current"), (-20_000.0, 20_000.0)
        )

        self.manager.set_y_range("engine_rpm", (-100.0, 300.0))

        self.assertEqual(self.manager.y_range("engine_rpm"), (-100.0, 300.0))
        self.assertEqual(self.manager.y_range("vehicle_speed"), (-2.0, 2.0))
        self.assertEqual(
            self.manager.y_range("battery_current"), (-20_000.0, 20_000.0)
        )

    def test_x_range_is_shared_while_y_ranges_remain_independent(self):
        self._register_three()

        self.reference.setXRange(2.0, 7.0, padding=0)
        APP.processEvents()

        for signal_key in self.manager.signal_keys:
            np.testing.assert_allclose(
                self.manager.view_box(signal_key).viewRange()[0], [2.0, 7.0]
            )
        self.assertEqual(self.manager.y_range("vehicle_speed"), (-2.0, 2.0))
        self.assertEqual(self.manager.y_range("engine_rpm"), (-200.0, 200.0))
        self.assertTrue(self.manager.axis_visible("vehicle_speed"))

        self.plot.resize(1100, 650)
        APP.processEvents()
        self.assertGreater(self.manager._geometry_sync_count, 0)
        for signal_key in self.manager.signal_keys:
            np.testing.assert_allclose(
                self.manager.view_box(signal_key).viewRange()[0],
                self.reference.viewRange()[0],
            )

    def test_remove_reorder_and_reregister_keep_ranges_attached_to_signal_keys(self):
        ranges = self._register_three()
        self.manager.set_y_range("engine_rpm", (-80.0, 120.0))
        self.manager.remove_signal("engine_rpm")

        self.assertEqual(
            self.manager.signal_keys, ("vehicle_speed", "battery_current")
        )
        self.manager.remove_signal("vehicle_speed")
        self.manager.remove_signal("battery_current")
        self.manager.register_signal("battery_current", ranges["battery_current"])
        self.manager.register_signal("engine_rpm", ranges["engine_rpm"])
        self.manager.register_signal("vehicle_speed", ranges["vehicle_speed"])

        self.assertEqual(
            self.manager.signal_keys,
            ("battery_current", "engine_rpm", "vehicle_speed"),
        )
        self.assertEqual(self.manager.y_range("engine_rpm"), (-80.0, 120.0))
        self.assertEqual(self.manager.y_range("vehicle_speed"), ranges["vehicle_speed"])
        self.assertEqual(
            self.manager.y_range("battery_current"), ranges["battery_current"]
        )

    def test_hiding_and_showing_axis_preserves_range_and_curve_view(self):
        axis = pg.AxisItem("left")
        view_box = self.manager.register_signal(
            "signal-a", (-10.0, 30.0), axis_item=axis
        )
        curve = pg.PlotDataItem([0, 1], [0, 10])
        view_box.addItem(curve)
        self.manager.set_y_range("signal-a", (-5.0, 15.0))

        self.manager.set_axis_visible("signal-a", False)
        self.assertFalse(axis.isVisible())
        self.assertIs(curve.getViewBox(), view_box)
        self.assertEqual(self.manager.y_range("signal-a"), (-5.0, 15.0))

        self.manager.set_axis_visible("signal-a", True)
        self.assertTrue(axis.isVisible())
        self.assertEqual(self.manager.y_range("signal-a"), (-5.0, 15.0))
        self.assertIs(axis.linkedView(), view_box)

    def test_current_signal_and_visible_axis_set_are_independent(self):
        self.manager.register_signal("signal-a", (-1.0, 1.0))
        self.manager.register_signal("signal-b", (-100.0, 100.0))

        self.manager.set_current_signal("signal-a")
        self.manager.set_axis_visible("signal-b", True)
        self.manager.set_current_signal("signal-b")
        self.manager.set_axis_visible("signal-b", False)

        self.assertEqual(self.manager.current_signal_key, "signal-b")
        self.assertTrue(self.manager.axis_visible("signal-a"))
        self.assertFalse(self.manager.axis_visible("signal-b"))

    def test_close_and_rebuild_release_old_views_and_restore_keyed_state(self):
        self._register_three()
        self.manager.set_axis_visible("engine_rpm", False)
        old_views = tuple(
            self.manager.view_box(key) for key in self.manager.signal_keys
        )
        old_manager = self.manager
        old_sync_count = self.manager._geometry_sync_count

        saved_state = self.manager.close()
        APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
        self.assertTrue(all(sip.isdeleted(view_box) for view_box in old_views))

        self.plot.resize(1000, 600)
        APP.processEvents()
        self.assertEqual(old_manager._geometry_sync_count, old_sync_count)

        self.manager = MultiYAxisManager(
            self.reference, self.plot.scene(), initial_state=saved_state
        )
        self._register_three()
        self.assertEqual(self.manager.y_range("engine_rpm"), (-200.0, 200.0))
        self.assertFalse(self.manager.axis_visible("engine_rpm"))

    def test_plot_item_axis_strip_links_axes_and_caps_visible_width(self):
        self.manager.close(preserve_state=False)
        APP.processEvents()

        plot = pg.PlotWidget()
        plot.resize(260, 420)
        plot.show()
        APP.processEvents()
        reference = plot.getViewBox()
        scroll_bar = QScrollBar(Qt.Orientation.Horizontal, plot)
        manager = MultiYAxisManager(
            reference, plot.scene(), plot_item=plot.getPlotItem(), plot_widget=plot,
            scroll_bar=scroll_bar,
        )
        try:
            axis_objects = {}
            curve_objects = {}
            for index in range(12):
                key = f"signal-{index}"
                axis = pg.AxisItem(orientation="left")
                axis_objects[key] = axis
                axis.setTextPen(pg.mkColor(index * 15, 80, 180))
                view_box = manager.register_signal(
                    key, (-10.0 ** index, 10.0 ** index), axis_item=axis
                )
                curve = pg.PlotDataItem([0.0, 1.0], [0.0, float(index + 1)])
                view_box.addItem(curve)
                curve_objects[key] = curve
            manager.set_current_signal("signal-11")
            manager.install_axis_layout()
            APP.processEvents()

            self.assertEqual(manager.displayed_axis_keys, ("signal-11",))
            self.assertFalse(scroll_bar.isVisible())
            for key in manager.signal_keys:
                manager.set_axis_visible(key, True)
            APP.processEvents()
            self.assertTrue(scroll_bar.isVisible())
            self.assertEqual(scroll_bar.pageStep(), manager.axis_capacity)
            self.assertEqual(
                scroll_bar.maximum(), len(manager.visible_axis_keys) - manager.axis_capacity
            )
            self.assertEqual(manager.current_signal_key, "signal-11")
            self.assertIn("signal-11", manager.displayed_axis_keys)
            self.assertEqual(
                manager.displayed_axis_keys,
                manager.visible_axis_keys[-manager.axis_capacity:],
            )
            self.assertLessEqual(
                sum(manager.axis_item(key).width()
                    for key in manager.displayed_axis_keys),
                plot.width() * 0.5 + 1,
            )
            self.assertIn("signal-11", manager.displayed_axis_keys)
            self.assertEqual(len(manager.visible_axis_keys), 12)
            stored_ranges = {
                key: manager.y_range(key) for key in manager.signal_keys
            }
            x_ranges = {
                key: tuple(manager.view_box(key).viewRange()[0])
                for key in manager.signal_keys
            }
            mapped_points = {
                key: manager.view_box(key).mapViewToScene(QPointF(0.5, 0.5))
                for key in manager.signal_keys
            }
            plot_geometry = reference.sceneBoundingRect()
            scroll_bar.setValue(0)
            APP.processEvents()
            self.assertEqual(
                manager.displayed_axis_keys,
                manager.visible_axis_keys[:manager.axis_capacity],
            )
            self.assertEqual(len(manager.visible_axis_keys), 12)
            self.assertEqual(
                {key: manager.y_range(key) for key in manager.signal_keys}, stored_ranges
            )
            self.assertEqual(
                {key: tuple(manager.view_box(key).viewRange()[0])
                 for key in manager.signal_keys}, x_ranges
            )
            self.assertEqual(
                {key: manager.view_box(key).mapViewToScene(QPointF(0.5, 0.5))
                 for key in manager.signal_keys}, mapped_points
            )
            self.assertTrue(all(
                curve_objects[key].getViewBox() is manager.view_box(key)
                for key in manager.signal_keys
            ))
            self.assertEqual(reference.sceneBoundingRect(), plot_geometry)
            manager.set_current_signal("signal-5")
            self.assertIn("signal-5", manager.displayed_axis_keys)
            self.assertEqual(len(manager.visible_axis_keys), 12)

            plot.resize(520, 420)
            APP.processEvents()
            self.assertEqual(scroll_bar.pageStep(), manager.axis_capacity)
            self.assertEqual(
                scroll_bar.maximum(), len(manager.visible_axis_keys) - manager.axis_capacity
            )
            manager.remove_signal("signal-0")
            APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            APP.processEvents()
            self.assertNotIn("signal-0", manager.signal_keys)
            self.assertTrue(sip.isdeleted(axis_objects["signal-0"]))
            self.assertEqual(
                scroll_bar.maximum(), len(manager.visible_axis_keys) - manager.axis_capacity
            )
            for key in manager.displayed_axis_keys:
                axis = manager.axis_item(key)
                self.assertIs(axis.linkedView(), manager.view_box(key))
                self.assertAlmostEqual(
                    axis.geometry().height(),
                    manager.view_box(key).sceneBoundingRect().height(),
                    delta=1.0,
                )
            manager.set_y_range("signal-11", (-12_000_000_000.0, 12_000_000_000.0))
            ticks = manager.axis_item("signal-11").tickValues(
                -12_000_000_000.0, 12_000_000_000.0, 400
            )
            self.assertTrue(ticks)
            self.assertEqual(
                manager.y_range("signal-0"), (-1.0, 1.0)
            )
        finally:
            manager.close(preserve_state=False)
            plot.close()
            plot.deleteLater()
            APP.processEvents()
    def test_remove_can_discard_state_and_duplicate_registration_is_rejected(self):
        self.manager.register_signal("temporary", (-1.0, 1.0))
        with self.assertRaises(ValueError):
            self.manager.register_signal("temporary", (-2.0, 2.0))

        self.manager.remove_signal("temporary", preserve_state=False)

        self.assertNotIn("temporary", self.manager.snapshot_state())
        self.assertEqual(self.manager.signal_keys, ())


if __name__ == "__main__":
    unittest.main()
