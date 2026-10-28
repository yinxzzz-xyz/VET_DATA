"""Cut-out dialog states, destination validation and remembered mode."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from vet_data_modular.cutout_dialog import CutoutDialog, CutoutProgressDialog, LAST_MODE_KEY
from vet_data_modular.data_cutout import (
    CutoutContext,
    CutoutWindow,
    SourceKind,
)


APP = QApplication.instance() or QApplication([])


class FakeSettings:
    def __init__(self):
        self.values = {}
        self.synced = 0

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        self.synced += 1


def blf_context(source_dir, **kwargs):
    defaults = dict(
        source_path=str(Path(source_dir, "run01.blf")),
        kind=SourceKind.BLF,
        window=CutoutWindow(1.0, 25.5),
        selected_total=3,
        selected_available=2,
        parsed_signal_count=40,
    )
    defaults.update(kwargs)
    return CutoutContext(**defaults)


class CutoutDialogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = self.temp.name
        self.settings = FakeSettings()

    def tearDown(self):
        self.temp.cleanup()

    def dialog(self, context, **kwargs):
        kwargs.setdefault("initial_dir", self.folder)
        kwargs.setdefault("settings", self.settings)
        return CutoutDialog(context, **kwargs)

    def test_describes_window_and_source(self):
        dialog = self.dialog(blf_context(self.folder))
        try:
            self.assertEqual(dialog.windowTitle(), "数据截取")
            self.assertIn("当前曲线视图", dialog.intro_label.text())
            self.assertIn("不会修改源文件", dialog.intro_label.text())
            self.assertEqual(dialog.range_label.text(), "截取范围：1.00 s – 25.50 s（共 24.50 s）")
            self.assertTrue(dialog.source_label.text().endswith("run01.blf"))
        finally:
            dialog.deleteLater()
            APP.processEvents()

    def test_blf_options_default_to_raw_messages(self):
        dialog = self.dialog(blf_context(self.folder))
        try:
            self.assertEqual(len(dialog.options), 2)
            self.assertEqual([option.key for option in dialog.options], ["blf:all", "mdf:selected"])
            self.assertIs(dialog.selected_option(), dialog.options[0])
            self.assertEqual(dialog.extension_label.text(), ".blf")
            self.assertIn("_cut_1.00s-25.50s", dialog.name_edit.text())
            self.assertTrue(dialog.path_preview.text().startswith("将保存为："))
            self.assertTrue(dialog.accept_button.isEnabled())
            self.assertEqual(dialog.directory_edit.text(), self.folder)
        finally:
            dialog.deleteLater()
            APP.processEvents()

    def test_switching_option_updates_extension_and_name(self):
        dialog = self.dialog(blf_context(self.folder))
        try:
            dialog._radios["mdf:selected"].setChecked(True)
            self.assertEqual(dialog.selected_option().key, "mdf:selected")
            self.assertEqual(dialog.extension_label.text(), ".mf4")
            self.assertIn("_signals", dialog.name_edit.text())
        finally:
            dialog.deleteLater()
            APP.processEvents()

    def test_edited_name_survives_option_switch(self):
        dialog = self.dialog(blf_context(self.folder))
        try:
            dialog._on_name_edited("my_cut")
            dialog.name_edit.setText("my_cut")
            dialog._radios["mdf:selected"].setChecked(True)
            self.assertEqual(dialog.name_edit.text(), "my_cut")
            self.assertTrue(dialog.output_path().name.endswith(".mf4"))
        finally:
            dialog.deleteLater()
            APP.processEvents()

    def test_disabled_option_explains_why(self):
        dialog = self.dialog(blf_context(self.folder, selected_total=2, selected_available=0))
        try:
            self.assertFalse(dialog._radios["mdf:selected"].isEnabled())
            self.assertIn("没有可导出的数据", dialog._descriptions["mdf:selected"].text())
            self.assertIs(dialog.selected_option(), dialog.options[0])
        finally:
            dialog.deleteLater()
            APP.processEvents()

    def test_remembered_mode_is_restored(self):
        dialog = self.dialog(blf_context(self.folder), remembered_mode="mdf:selected")
        try:
            self.assertEqual(dialog.selected_option().key, "mdf:selected")
        finally:
            dialog.deleteLater()
            APP.processEvents()

    def test_remembered_mode_is_ignored_when_disabled(self):
        dialog = self.dialog(
            blf_context(self.folder, selected_available=0), remembered_mode="mdf:selected",
        )
        try:
            self.assertEqual(dialog.selected_option().key, "blf:all")
        finally:
            dialog.deleteLater()
            APP.processEvents()

    def test_accept_saves_mode_and_output_path(self):
        dialog = self.dialog(blf_context(self.folder))
        try:
            dialog.accept()
        finally:
            dialog.deleteLater()
            APP.processEvents()
        self.assertEqual(self.settings.values[LAST_MODE_KEY.format(kind="blf")], "blf:all")
        self.assertEqual(self.settings.synced, 1)

    def test_output_path_uses_selected_directory(self):
        dialog = self.dialog(blf_context(self.folder))
        try:
            dialog.directory_edit.setText(self.folder)
            dialog._on_name_edited("abc")
            dialog.name_edit.setText("abc")
            path = dialog.output_path()
        finally:
            dialog.deleteLater()
            APP.processEvents()
        self.assertEqual(path, Path(self.folder, "abc.blf"))

    def test_source_file_cannot_be_overwritten(self):
        context = blf_context(self.folder)
        dialog = self.dialog(context)
        try:
            dialog.directory_edit.setText(self.folder)
            dialog._on_name_edited("run01")
            dialog.name_edit.setText("run01")
            with patch.object(QMessageBox, "warning") as warning:
                dialog.accept()
            warning.assert_called_once()
            self.assertIn("源文件", warning.call_args[0][2])
            self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        finally:
            dialog.deleteLater()
            APP.processEvents()

    def test_overwrite_confirmation(self):
        target = Path(self.folder, "run01_cut_1.00s-25.50s.blf")
        target.write_bytes(b"old")
        dialog = self.dialog(blf_context(self.folder))
        try:
            with patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.No,
            ):
                dialog.accept()
            self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
            with patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes,
            ):
                dialog.accept()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        finally:
            dialog.deleteLater()
            APP.processEvents()

    def test_mdf_and_csv_contexts_use_their_formats(self):
        mdf_context = CutoutContext(
            source_path=str(Path(self.folder, "run01.mf4")),
            kind=SourceKind.MDF,
            window=CutoutWindow(0.0, 5.0),
            selected_total=2,
            selected_available=1,
            file_signal_count=2640,
        )
        dialog = self.dialog(mdf_context)
        try:
            self.assertEqual(dialog.selected_option().key, "mdf:all")
            self.assertEqual(dialog.extension_label.text(), ".mf4")
            self.assertIn("2640", dialog._radios["mdf:all"].text())
        finally:
            dialog.deleteLater()
            APP.processEvents()

        csv_context = CutoutContext(
            source_path=str(Path(self.folder, "run01.csv")),
            kind=SourceKind.CSV,
            window=CutoutWindow(0.0, 5.0),
            selected_total=1,
            selected_available=1,
            file_signal_count=6,
        )
        dialog = self.dialog(csv_context)
        try:
            self.assertEqual(dialog.selected_option().key, "csv:all")
            self.assertEqual(dialog.extension_label.text(), ".csv")
        finally:
            dialog.deleteLater()
            APP.processEvents()


class CutoutProgressDialogTests(unittest.TestCase):
    def test_progress_switches_between_determinate_and_indeterminate(self):
        dialog = CutoutProgressDialog()
        try:
            dialog.set_progress("正在读取报文…", 5, 10)
            self.assertEqual(dialog.progress_bar.maximum(), 10)
            self.assertEqual(dialog.progress_bar.value(), 5)
            self.assertEqual(dialog.detail_label.text(), "5 / 10")
            dialog.set_progress("正在写入文件…", 0, 0)
            self.assertEqual(dialog.progress_bar.maximum(), 0)
            self.assertEqual(dialog.status_label.text(), "正在写入文件…")
            self.assertEqual(dialog.detail_label.text(), "")
        finally:
            dialog.deleteLater()
            APP.processEvents()

    def test_cancel_is_requested_only_once(self):
        dialog = CutoutProgressDialog()
        try:
            seen = []
            dialog.cancel_requested.connect(lambda: seen.append(True))
            dialog.request_cancel()
            dialog.request_cancel()
            self.assertEqual(seen, [True])
            self.assertFalse(dialog.cancel_button.isEnabled())
            self.assertIn("正在安全停止截取", dialog.status_label.text())
            dialog.reject()
            self.assertEqual(seen, [True])
        finally:
            dialog.deleteLater()
            APP.processEvents()


if __name__ == "__main__":
    unittest.main()
