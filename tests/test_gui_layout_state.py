import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QApplication, QGroupBox, QSplitter

from vet_data_modular.gui_state import (
    DEFAULT_GEOMETRY,
    DEFAULT_SPLITTER_SIZES,
    GEOMETRY_KEY,
    HORIZONTAL_SPLITTER_SIZES_KEY,
    MIN_LEFT_WIDTH,
    MIN_RIGHT_WIDTH,
    restore_main_window_state,
    save_main_window_state,
)
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


class MainWindowLayoutStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = QSettings(
            os.path.join(self.temporary.name, "gui-state.ini"),
            QSettings.Format.IniFormat,
        )
        self.settings.clear()
        self.window = MDFPlotter(gui_settings=self.settings)
        self.window.show()
        APP.processEvents()

    def tearDown(self):
        self.window.deleteLater()
        APP.processEvents()
        self.temporary.cleanup()

    def test_horizontal_splitter_exists_with_hit_target_and_no_collapse(self):
        splitter = self.window.main_splitter
        self.assertIsInstance(splitter, QSplitter)
        self.assertEqual(splitter.orientation(), Qt.Orientation.Horizontal)
        self.assertFalse(splitter.childrenCollapsible())
        self.assertGreaterEqual(splitter.handleWidth(), 8)
        self.assertEqual(splitter.handle(1).cursor().shape(), Qt.CursorShape.SplitHCursor)
        self.assertIs(splitter.widget(0), self.window.left_panel_widget)
        self.assertEqual(splitter.widget(1).title(), "信号曲线")

    def test_default_sizes_and_minimums_keep_right_panel_dominant(self):
        left, right = self.window.main_splitter.sizes()
        self.assertGreaterEqual(left, MIN_LEFT_WIDTH)
        self.assertGreaterEqual(right, MIN_RIGHT_WIDTH)
        self.assertGreater(right, left)
        self.assertGreater(DEFAULT_SPLITTER_SIZES[1], DEFAULT_SPLITTER_SIZES[0] * 2)
        self.assertEqual(self.window.left_panel_widget.minimumWidth(), MIN_LEFT_WIDTH)
        self.assertEqual(
            self.window.main_splitter.widget(1).minimumWidth(), MIN_RIGHT_WIDTH
        )
        self.window.main_splitter.setSizes([0, 10_000])
        APP.processEvents()
        self.assertGreaterEqual(self.window.main_splitter.sizes()[0], MIN_LEFT_WIDTH)
        self.window.main_splitter.setSizes([10_000, 0])
        APP.processEvents()
        self.assertGreaterEqual(self.window.main_splitter.sizes()[1], MIN_RIGHT_WIDTH)

    def test_geometry_and_horizontal_sizes_round_trip(self):
        self.window.setGeometry(140, 160, 1200, 720)
        self.window.main_splitter.setSizes([400, 790])
        APP.processEvents()
        expected_sizes = self.window.main_splitter.sizes()
        save_main_window_state(self.window, self.window.main_splitter, self.settings)
        self.assertEqual(
            [int(value) for value in self.settings.value(HORIZONTAL_SPLITTER_SIZES_KEY)],
            expected_sizes,
        )
        self.assertTrue(self.settings.value(GEOMETRY_KEY))

        restored = MDFPlotter(gui_settings=self.settings)
        try:
            restored.show()
            APP.processEvents()
            # Qt clamps geometry to the current desktop.  The restored window
            # must remain visible and retain the requested left/right priority.
            self.assertGreaterEqual(restored.width(), 640)
            self.assertGreaterEqual(restored.height(), 500)
            restored_left, restored_right = restored.main_splitter.sizes()
            self.assertGreaterEqual(restored_left, MIN_LEFT_WIDTH)
            self.assertGreaterEqual(restored_right, MIN_RIGHT_WIDTH)
            self.assertGreater(restored_right, restored_left)
        finally:
            restored.deleteLater()
            APP.processEvents()

    def test_window_close_persists_only_ui2_keys(self):
        self.window.setGeometry(120, 140, 1100, 700)
        self.window.main_splitter.setSizes([360, 730])
        APP.processEvents()
        self.assertTrue(self.window.close())
        APP.processEvents()
        self.assertEqual(
            set(self.settings.allKeys()),
            {GEOMETRY_KEY, HORIZONTAL_SPLITTER_SIZES_KEY},
        )

    def test_missing_and_invalid_settings_fall_back_safely(self):
        self.assertGreaterEqual(self.window.width(), DEFAULT_GEOMETRY[2])
        left, right = self.window.main_splitter.sizes()
        self.assertGreaterEqual(left, MIN_LEFT_WIDTH)
        self.assertGreaterEqual(right, MIN_RIGHT_WIDTH)

        self.settings.setValue(GEOMETRY_KEY, b"not-a-qt-geometry")
        self.settings.setValue(HORIZONTAL_SPLITTER_SIZES_KEY, [0, -1, 99])
        restore_main_window_state(
            self.window, self.window.main_splitter, self.settings
        )
        geometry = self.window.geometry()
        self.assertEqual(
            (geometry.x(), geometry.y(), geometry.width(), geometry.height()),
            DEFAULT_GEOMETRY,
        )
        left, right = self.window.main_splitter.sizes()
        self.assertGreaterEqual(left, MIN_LEFT_WIDTH)
        self.assertGreaterEqual(right, MIN_RIGHT_WIDTH)

    def test_existing_vertical_splitter_is_preserved(self):
        splitter = self.window.left_splitter
        self.assertEqual(splitter.orientation(), Qt.Orientation.Vertical)
        self.assertFalse(splitter.childrenCollapsible())
        self.assertEqual(
            [splitter.widget(index).title() for index in range(splitter.count())],
            ["可用信号", "CAN通道配置"],
        )

    def test_resize_and_splitter_move_preserve_critical_gui_state(self):
        self.window.signals.update({
            "raw_a": {
                "name": "a", "display_name": "Speed", "comment": "vehicle",
                "bus_id": None,
            },
            "raw_b": {
                "name": "b", "display_name": "Latitude", "comment": "gps",
                "bus_id": None,
            },
        })
        self.window.refresh_signal_list_ui()
        checked = self.window.signal_widgets["raw_a"]["checkbox"]
        checked.setChecked(True)
        self.window.lat_combo.setCurrentIndex(self.window.lat_combo.findData("raw_b"))
        self.window._set_view_mode("selected")
        groups_before = {
            group.title() for group in self.window.findChildren(QGroupBox)
        }

        self.window.resize(1700, 980)
        self.window.main_splitter.setSizes([450, 1240])
        APP.processEvents()

        self.assertEqual(self.window.get_selected_signals(), ["raw_a"])
        self.assertTrue(checked.isChecked())
        self.assertEqual(self.window.lat_combo.currentData(), "raw_b")
        self.assertEqual(self.window._view_mode, "selected")
        self.assertEqual(
            {group.title() for group in self.window.findChildren(QGroupBox)},
            groups_before,
        )
        located = self.window._find_layout_containing(
            self.window.layout(), self.window.save_data_button
        )
        self.assertIsNotNone(located)


if __name__ == "__main__":
    unittest.main()
