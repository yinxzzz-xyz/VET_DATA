import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings
from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtWidgets import QApplication

from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


class MainWindowVerticalScrollTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        settings = QSettings(
            os.path.join(self.temporary.name, "vertical-scroll.ini"),
            QSettings.Format.IniFormat,
        )
        self.window = MDFPlotter(gui_settings=settings)

    def tearDown(self):
        self.window.deleteLater()
        APP.processEvents()
        self.temporary.cleanup()

    def _show_at(self, width, height):
        self.window.resize(width, height)
        self.window.show()
        APP.processEvents()

    def test_fixed_shortcuts_stay_visible_above_the_main_splitter(self):
        self._show_at(1200, 800)
        shortcuts = (
            self.window.load_file_button,
            self.window.plot_button,
            self.window.math_channel_button,
            self.window.single_cursor_button,
            self.window.dual_cursor_button,
            self.window.plot_mode_combo,
        )
        self.assertEqual(self.window.quick_actions_layout.count(), 6)
        self.assertTrue(all(button.isVisible() and button.width() > 0 for button in shortcuts))
        self.assertTrue(all(button.height() == 36 for button in shortcuts))
        self.assertEqual(
            [self.window.ribbon_tabs.tabText(index) for index in range(self.window.ribbon_tabs.count())],
            ["文件", "信号管理", "曲线显示", "分析工具", "数据处理", "导出", "视图"],
        )
        for index in range(self.window.ribbon_tabs.count()):
            self.window.ribbon_tabs.setCurrentIndex(index)
            APP.processEvents()
            self.assertTrue(all(button.isVisible() for button in shortcuts))
        self.assertLess(self.window.ribbon_widget.geometry().bottom(), self.window.main_splitter.geometry().top())
        self.assertFalse(self.window.ribbon_widget._wrapped)
        self.assertEqual(
            self.window.ribbon_tabs.geometry().top(),
            self.window.quick_actions_widget.geometry().top(),
        )
        for button in shortcuts:
            top_left = button.mapTo(self.window.quick_actions_widget, QPoint(0, 0))
            self.assertTrue(
                self.window.quick_actions_widget.rect().contains(
                    QRect(top_left, button.size())
                )
            )

    def test_narrow_top_actions_do_not_overlap_and_remain_operable(self):
        self._show_at(850, 620)
        self.window.main_splitter.setSizes([320, 530])
        APP.processEvents()
        buttons = (
            self.window.load_file_button,
            self.window.plot_button,
            self.window.math_channel_button,
            self.window.single_cursor_button,
            self.window.dual_cursor_button,
            self.window.plot_mode_combo,
        )
        self.assertTrue(self.window.ribbon_widget._wrapped)
        self.assertTrue(all(button.isVisible() and button.width() >= 32 for button in buttons))
        self.assertTrue(all(button.height() == 36 for button in buttons))
        for left, right in zip(buttons, buttons[1:]):
            self.assertFalse(left.geometry().intersects(right.geometry()))
        self.assertLessEqual(
            buttons[-1].mapTo(self.window, buttons[-1].rect().bottomRight()).x(),
            self.window.width(),
        )

    def test_gps_controls_live_in_view_ribbon_without_using_left_panel_space(self):
        self._show_at(900, 430)
        self.assertNotIn("GPS轨迹图", self.window.collapsible_groups)
        self.assertFalse(self.window.show_map_button.isChecked())
        self.window.ribbon_tabs.setCurrentWidget(self.window.view_ribbon_page)
        APP.processEvents()
        self.assertTrue(self.window.lat_combo.isVisible())
        self.assertTrue(self.window.lon_combo.isVisible())
        self.assertTrue(self.window.show_map_button.isVisible())
        self.assertTrue(self.window.math_channel_button.isVisible())
        self.window.ribbon_tabs.setCurrentWidget(self.window.processing_ribbon_page)
        APP.processEvents()
        self.assertTrue(self.window.blf_slice_button.isVisible())


if __name__ == "__main__":
    unittest.main()
