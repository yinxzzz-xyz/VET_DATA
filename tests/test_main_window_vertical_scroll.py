import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings
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

    def test_top_actions_use_four_plus_one_two_row_layout(self):
        self._show_at(1200, 800)
        top_buttons = (
            self.window.select_all_button,
            self.window.deselect_all_button,
            self.window.save_config_button,
            self.window.load_config_button,
        )

        self.assertEqual({button.y() for button in top_buttons}, {top_buttons[0].y()})
        self.assertEqual(self.window.top_action_layout.count(), 4)
        self.assertTrue(all(button.width() > 0 for button in top_buttons))
        self.assertLessEqual(max(button.width() for button in top_buttons) - min(
            button.width() for button in top_buttons
        ), 1)

        old_three_row_height = sum(
            (top_buttons[0].sizeHint().height(),
             self.window.save_config_button.sizeHint().height(),
             self.window.plot_button.sizeHint().height())
        ) + self.window.left_panel_content.layout().spacing() * 2
        new_two_row_height = (
            max(button.height() for button in top_buttons)
            + self.window.plot_button.height()
            + self.window.left_panel_content.layout().spacing()
        )
        self.assertLess(new_two_row_height, old_three_row_height)

    def test_narrow_top_actions_do_not_overlap_and_remain_operable(self):
        self._show_at(850, 620)
        self.window.main_splitter.setSizes([320, 530])
        APP.processEvents()
        buttons = (
            self.window.select_all_button,
            self.window.deselect_all_button,
            self.window.save_config_button,
            self.window.load_config_button,
        )
        self.assertTrue(all(button.isVisible() and button.width() >= 55 for button in buttons))
        for left, right in zip(buttons, buttons[1:]):
            self.assertFalse(left.geometry().intersects(right.geometry()))
        self.assertLessEqual(buttons[-1].geometry().right(), self.window.left_panel_content.width())

    def test_expanded_sections_scroll_to_access_bottom_actions(self):
        for title in ("CAN通道配置", "GPS轨迹图"):
            self.window.collapsible_groups[title].setChecked(True)
        self._show_at(900, 430)

        bar = self.window.left_panel_scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum())
        APP.processEvents()

        viewport_rect = self.window.left_panel_scroll.viewport().rect()
        for button in (
            self.window.math_channel_button,
            self.window.blf_slice_button,
        ):
            center = button.mapTo(self.window.left_panel_scroll.viewport(), button.rect().center())
            self.assertTrue(button.isVisible())
            self.assertTrue(viewport_rect.contains(center), button.text())
            self.assertTrue(button.isEnabled() or not self.window.signals)


if __name__ == "__main__":
    unittest.main()
