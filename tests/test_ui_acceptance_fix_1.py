import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import QSettings, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QWidget

from vet_data_modular import baseline
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


class _SignalData:
    def __init__(self, timestamps, samples):
        self.timestamps = np.asarray(timestamps, dtype=float)
        self.samples = np.asarray(samples, dtype=float)

    def __len__(self):
        return len(self.timestamps)


class _FakeWebView(QWidget):
    loadFinished = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.html = ""

    def settings(self):
        return self

    def setAttribute(self, *_args):
        pass

    def setHtml(self, html):
        self.html = html


class UiAcceptanceFix1Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        settings = QSettings(
            os.path.join(self.temporary.name, "ui-acceptance-1.ini"),
            QSettings.Format.IniFormat,
        )
        self.window = MDFPlotter(gui_settings=settings)
        self.window.resize(1100, 760)
        self.window.show()
        APP.processEvents()

    def tearDown(self):
        self.window.deleteLater()
        APP.processEvents()
        self.temporary.cleanup()

    def test_file_actions_start_left_and_filename_uses_remaining_space(self):
        self.window.ribbon_tabs.setCurrentWidget(self.window.file_ribbon_page)
        APP.processEvents()
        open_mirror = self.window._ribbon_action_mirrors[
            self.window.load_file_button
        ][0]
        widgets = [
            self.window.file_ribbon_layout.itemAt(index).widget()
            for index in range(self.window.file_ribbon_layout.count())
            if self.window.file_ribbon_layout.itemAt(index).widget() is not None
        ]
        self.assertEqual(
            widgets[:4],
            [
                open_mirror,
                self.window.save_config_button,
                self.window.load_config_button,
                self.window.current_file_status,
            ],
        )

        self.window.set_current_file_status(r"D:\recordings\vehicle_run_2026.mf4")
        self.assertIn("vehicle_run_2026.mf4", self.window.current_file_label.fullText())
        self.assertEqual(
            self.window.current_file_label.toolTip(),
            r"D:\recordings\vehicle_run_2026.mf4",
        )
        for width in (850, 1200):
            self.window.resize(width, 760)
            APP.processEvents()
            status_rect = self.window.current_file_status.rect()
            status_rect.moveTopLeft(
                self.window.current_file_status.mapTo(self.window, status_rect.topLeft())
            )
            for button in (open_mirror, self.window.save_config_button, self.window.load_config_button):
                button_rect = button.rect()
                button_rect.moveTopLeft(button.mapTo(self.window, button_rect.topLeft()))
                self.assertFalse(status_rect.intersects(button_rect))

    def test_gps_toggle_reuses_dialog_and_web_view_without_clearing_data(self):
        self.window.signals = {
            "lat": {"name": "Latitude"},
            "lon": {"name": "Longitude"},
        }
        self.window.lat_combo.addItem("Latitude", "lat")
        self.window.lon_combo.addItem("Longitude", "lon")
        self.window.set_buttons_enabled(True)
        gps_signal = _SignalData([0, 1, 2], [35.0, 35.1, 35.2])
        longitude_signal = _SignalData([0, 1, 2], [139.0, 139.1, 139.2])
        with patch.object(
            self.window,
            "_get_signal",
            side_effect=[gps_signal, longitude_signal, gps_signal, longitude_signal],
        ), patch.object(baseline, "QWebEngineView", _FakeWebView), patch.object(
            baseline.QMessageBox, "warning"
        ) as warning:
            self.assertFalse(self.window.show_map_button.isChecked())
            self.window.show_map_button.setChecked(True)
            warning.assert_not_called()
            first_dialog = self.window.map_dialog
            first_web_view = self.window.web_view
            self.assertIsNotNone(first_dialog)
            self.assertIsInstance(first_web_view, _FakeWebView)
            self.assertTrue(first_dialog.isVisible())
            self.assertEqual(first_web_view.receivers(first_web_view.loadFinished), 1)
            gps_snapshot = self.window.gps_df.copy(deep=True)

            self.window.show_map_button.setChecked(False)
            self.assertFalse(first_dialog.isVisible())
            self.assertFalse(self.window.show_map_button.isChecked())
            self.assertTrue(self.window.gps_df.equals(gps_snapshot))

            self.window.show_map_button.setChecked(True)
            self.assertIs(self.window.map_dialog, first_dialog)
            self.assertIs(self.window.web_view, first_web_view)
            self.assertEqual(first_web_view.receivers(first_web_view.loadFinished), 1)
            self.window.show_map_button.setChecked(False)

    def test_can_channel_uses_separate_card_with_non_overlapping_actions(self):
        from vet_data_modular.baseline import BusConfigWidget

        channel = BusConfigWidget(3)
        try:
            self.assertEqual(channel.card_group.objectName(), "busConfigCard3")
            self.assertEqual(channel.card_group.title(), "CAN 通道")
            channel.set_protocol_display("a_very_long_powertrain_database_file_name.dbc")
            self.assertIn(
                "a_very_long_powertrain_database_file_name.dbc",
                channel.protocol_label.toolTip(),
            )
            channel.resize(360, channel.sizeHint().height())
            channel.show()
            APP.processEvents()
            buttons = (
                channel.select_protocol_btn,
                channel.parse_btn,
                channel.protocol_clear_btn,
            )
            self.assertTrue(all(button.width() > 0 for button in buttons))
            self.assertFalse(buttons[0].geometry().intersects(buttons[1].geometry()))
            self.assertFalse(buttons[1].geometry().intersects(buttons[2].geometry()))
        finally:
            channel.close()
            channel.deleteLater()
            APP.processEvents()


if __name__ == "__main__":
    unittest.main()
