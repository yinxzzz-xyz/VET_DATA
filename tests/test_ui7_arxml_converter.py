import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from vet_data_modular import baseline
from vet_data_modular.theme import DEFAULT_THEME
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


class ArxmlConverterUi7Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.arxml = self.root / "vehicle-network-description-with-a-long-name.arxml"
        self.arxml.write_text("<AUTOSAR/>", encoding="utf-8")
        self.dialog = baseline.ARXMLConverterDialog()

    def tearDown(self):
        self.dialog.deleteLater()
        APP.processEvents()
        self.temporary.cleanup()

    def _select_file(self):
        with patch.object(
            baseline.QFileDialog,
            "getOpenFileName",
            return_value=(str(self.arxml), ""),
        ):
            self.dialog.arxml_select_btn.click()

    def test_theme_sections_and_primary_secondary_hierarchy(self):
        self.assertEqual(self.dialog.property("uiDialog"), "arxmlConverter")
        self.assertEqual(self.dialog.path_group.property("uiArxmlSection"), "true")
        self.assertEqual(self.dialog.conversion_group.property("uiArxmlSection"), "true")
        self.assertEqual(self.dialog.log_group.property("uiArxmlSection"), "true")
        self.assertEqual(self.dialog.convert_btn.property("uiRole"), "primary")
        for button in (
            self.dialog.arxml_select_btn,
            self.dialog.clear_log_btn,
            self.dialog.open_folder_btn,
            self.dialog.close_btn,
        ):
            self.assertEqual(button.property("uiRole"), "secondary")
        self.assertIn(DEFAULT_THEME.colors.primary, self.dialog.styleSheet())
        self.assertNotIn("#9b59b6", self.dialog.styleSheet().lower())

    def test_file_selection_generates_read_only_output_and_full_path_tooltips(self):
        self.assertFalse(self.dialog.convert_btn.isEnabled())
        self.assertTrue(self.dialog.arxml_path_edit.isReadOnly())
        self.assertTrue(self.dialog.dbc_path_edit.isReadOnly())
        self._select_file()
        expected_output = self.root / "dbc_output" / f"{self.arxml.stem}.dbc"
        self.assertEqual(Path(self.dialog.arxml_path), self.arxml.resolve())
        self.assertEqual(Path(self.dialog.dbc_path), expected_output)
        self.assertEqual(self.dialog.arxml_path_edit.toolTip(), str(self.arxml.resolve()))
        self.assertEqual(self.dialog.dbc_path_edit.toolTip(), str(expected_output))
        self.assertTrue(expected_output.parent.is_dir())
        self.assertTrue(self.dialog.convert_btn.isEnabled())
        self.assertFalse(self.dialog.open_folder_btn.isEnabled())

    def test_busy_failure_success_progress_log_and_button_states(self):
        self._select_file()
        with patch.object(baseline.threading, "Thread") as thread_type:
            self.dialog.convert_btn.click()
        thread_type.assert_called_once()
        thread_type.return_value.start.assert_called_once()
        self.assertTrue(self.dialog.is_running)
        self.assertFalse(self.dialog.convert_btn.isEnabled())
        self.assertFalse(self.dialog.open_folder_btn.isEnabled())
        self.assertTrue(self.dialog.progress_bar.isVisibleTo(self.dialog))
        self.assertEqual(self.dialog.status_label.text(), "转换中...")
        self.assertEqual(self.dialog.status_label.property("statusKind"), "running")
        self.assertIn("开始转换", self.dialog.log_text.toPlainText())

        with patch.object(baseline.QMessageBox, "critical"):
            self.dialog._on_error("failed")
        self.assertFalse(self.dialog.is_running)
        self.assertTrue(self.dialog.convert_btn.isEnabled())
        self.assertFalse(self.dialog.progress_bar.isVisible())
        self.assertEqual(self.dialog.status_label.text(), "转换失败")
        self.assertEqual(self.dialog.status_label.property("statusKind"), "error")
        self.assertIn("转换失败: failed", self.dialog.log_text.toPlainText())

        Path(self.dialog.dbc_path).write_text("BO_ 1 Message: 8 Vector__XXX\n", encoding="utf-8")
        with patch.object(baseline.QMessageBox, "information"):
            self.dialog._on_success()
        self.assertEqual(self.dialog.status_label.text(), "转换成功！")
        self.assertEqual(self.dialog.status_label.property("statusKind"), "success")
        self.assertTrue(self.dialog.open_folder_btn.isEnabled())
        self.dialog.clear_log_btn.click()
        self.assertEqual(self.dialog.log_text.toPlainText(), "")

    def test_converter_keeps_existing_api_arguments_and_success_signal(self):
        self._select_file()
        emitted = []
        self.dialog.conversion_success.connect(lambda: emitted.append(True))

        def write_valid_dbc(_source, target, **_options):
            Path(target).write_text("BO_ 1 Message: 8 Vector__XXX\n", encoding="utf-8")

        with patch("canmatrix.convert.convert", side_effect=write_valid_dbc) as convert, \
             patch.object(baseline.QMessageBox, "information"):
            self.dialog._run_conversion()
        convert.assert_called_once_with(
            str(self.arxml.resolve()),
            str(self.root / "dbc_output" / f"{self.arxml.stem}.dbc"),
            dbcExportEncoding="ascii",
            ignoreEncodingErrors="ignore",
        )
        self.assertEqual(emitted, [True])
        self.assertIn("转换成功", self.dialog.log_text.toPlainText())

    def test_open_folder_condition_and_close_behavior(self):
        self._select_file()
        self.assertFalse(self.dialog.open_folder_btn.isEnabled())
        self.dialog.open_folder_btn.setEnabled(True)
        if hasattr(baseline.os, "startfile"):
            with patch.object(baseline.os, "startfile") as startfile:
                self.dialog.open_folder_btn.click()
            startfile.assert_called_once_with(str(self.root / "dbc_output"))
        self.dialog.show()
        self.dialog.close_btn.click()
        APP.processEvents()
        self.assertFalse(self.dialog.isVisible())

    def test_resize_prioritizes_log_area_and_keeps_paths_usable(self):
        self._select_file()
        self.dialog.show()
        self.dialog.resize(640, 520)
        APP.processEvents()
        initial_log_height = self.dialog.log_text.height()
        initial_path_height = self.dialog.path_group.height()
        self.dialog.resize(900, 820)
        APP.processEvents()
        self.assertGreater(self.dialog.log_text.height(), initial_log_height)
        self.assertEqual(self.dialog.path_group.height(), initial_path_height)
        self.assertTrue(self.dialog.arxml_path_edit.isVisible())
        self.assertTrue(self.dialog.dbc_path_edit.isVisible())
        self.assertTrue(self.dialog.convert_btn.isVisible())


class ArxmlMainWindowEntryUi7Tests(unittest.TestCase):
    def test_entry_stays_secondary_independent_and_opens_same_dialog(self):
        window = MDFPlotter()
        try:
            self.assertEqual(window.arxml2dbc_btn.property("uiRole"), "secondary")
            self.assertEqual(window.arxml2dbc_btn.property("uiToolEntry"), "true")
            self.assertEqual(window.arxml2dbc_btn.styleSheet(), "")
            window.collapsible_groups["CAN通道配置"].setChecked(True)
            window.set_buttons_enabled(False)
            self.assertTrue(window.arxml2dbc_btn.isEnabled())
            window.arxml2dbc_btn.click()
            APP.processEvents()
            self.assertIsInstance(window._arxml_converter, baseline.ARXMLConverterDialog)
            self.assertTrue(window._arxml_converter.isVisible())
            window._arxml_converter.close()
        finally:
            window.deleteLater()
            APP.processEvents()


if __name__ == "__main__":
    unittest.main()
