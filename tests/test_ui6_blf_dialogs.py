import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QGroupBox

from vet_data_modular.blf_slice_dialog import (
    OTHER_OFFSET, BlfSliceDialog, DuplicateSelectionDialog, selected_offset,
)
from vet_data_modular.blf_slice_models import (
    ConditionResult, ConditionSpec, ConditionStatus, InputMode, SliceTask,
    TaskResult, TaskStatus,
)
from vet_data_modular.blf_slice_progress import (
    BlfSliceProgressDialog, BlfSliceResultDialog,
)
from vet_data_modular.blf_slice_service import DuplicateFileGroup
from vet_data_modular.theme import DEFAULT_THEME


APP = QApplication.instance() or QApplication([])


class FakeWorker(QObject):
    phase_changed = pyqtSignal(str, str)
    progress_changed = pyqtSignal(int, int, str)

    def __init__(self):
        super().__init__()
        self.running = True
        self.cancel_calls = 0

    def cancel(self):
        self.cancel_calls += 1

    def isRunning(self):
        return self.running


class BlfSliceMainDialogUi6Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.blf = self.root / "input.blf"
        self.blf.touch()
        self.table = self.root / "conditions.csv"
        self.table.write_text(
            "记录时间,记录内容,向前秒数,向后秒数\n"
            "2026/9/17 10:30,高速巡航工况名称用于验证完整提示,30,60\n"
            "2026/9/17 10:31,低速工况,45,90\n",
            encoding="utf-8-sig",
        )
        self.dialog = BlfSliceDialog()

    def tearDown(self):
        self.dialog.deleteLater()
        APP.processEvents()
        self.temporary.cleanup()

    def _load_table(self):
        self.dialog.table_edit.setText(str(self.table))
        self.dialog.load_condition_table(self.table)

    def test_four_regions_and_primary_secondary_hierarchy(self):
        sections = (
            self.dialog.input_box,
            self.dialog.settings_box,
            self.dialog.condition_box,
            self.dialog.execution_box,
        )
        self.assertEqual(
            [section.title() for section in sections],
            ["输入", "任务级设置", "工况表", "执行"],
        )
        self.assertTrue(all(section.property("uiBlfSection") == "true" for section in sections))
        self.assertEqual(self.dialog.start_button.property("uiRole"), "primary")
        for button in (
            self.dialog.cancel_button,
            self.dialog.select_all_button,
            self.dialog.clear_all_button,
            self.dialog.apply_batch_button,
            self.dialog.input_path_row.browse_button,
            self.dialog.table_path_row.browse_button,
            self.dialog.output_path_row.browse_button,
        ):
            self.assertEqual(button.property("uiRole"), "secondary")
        self.assertIn(DEFAULT_THEME.colors.primary, self.dialog.styleSheet())

    def test_path_selection_and_actual_output_hint(self):
        with patch(
            "vet_data_modular.blf_slice_dialog.QFileDialog.getOpenFileName",
            return_value=(str(self.blf), ""),
        ):
            self.dialog.input_path_row.browse_button.click()
        self.assertTrue(self.dialog.input_path_row.browse_button.isEnabled())
        self.assertEqual(self.dialog.input_edit.text(), str(self.blf))
        self.assertIn("实际输出目录", self.dialog.output_hint.text())
        self.assertEqual(self.dialog.output_hint.toolTip(), self.dialog.output_hint.text())

        output = self.root / "out"
        with patch(
            "vet_data_modular.blf_slice_dialog.QFileDialog.getExistingDirectory",
            return_value=str(output),
        ):
            self.dialog.output_path_row.browse_button.click()
        self.assertEqual(self.dialog.output_edit.text(), str(output))
        self.assertIn(str(output), self.dialog.output_hint.text())
        self.assertEqual(self.dialog.output_edit.toolTip(), str(output))

    def test_timezone_batch_table_selection_edit_status_and_tooltips(self):
        self._load_table()
        self.dialog.blf_offset_combo.setCurrentIndex(
            self.dialog.blf_offset_combo.findData(OTHER_OFFSET)
        )
        self.dialog.blf_special_offset_combo.setCurrentIndex(
            self.dialog.blf_special_offset_combo.findData(345)
        )
        self.assertEqual(
            selected_offset(
                self.dialog.blf_offset_combo,
                self.dialog.blf_special_offset_combo,
            ),
            timedelta(hours=5, minutes=45),
        )
        self.dialog.batch_before_spin.setValue(12)
        self.dialog.batch_after_spin.setValue(34)
        self.dialog.apply_batch_button.click()
        self.assertEqual(
            [
                (
                    self.dialog.condition_table.cellWidget(row, 4).value(),
                    self.dialog.condition_table.cellWidget(row, 5).value(),
                )
                for row in range(2)
            ],
            [(12, 34), (12, 34)],
        )
        self.dialog.clear_all_button.click()
        self.assertTrue(all(
            self.dialog.condition_table.item(row, 0).checkState() == Qt.CheckState.Unchecked
            for row in range(2)
        ))
        self.dialog.select_all_button.click()
        self.dialog.condition_table.item(0, 2).setText("编辑后的长工况名称")
        self.dialog._mark_invalid(0, 3, "时间格式应为 yyyy-MM-dd HH:mm:ss")
        APP.processEvents()
        self.assertEqual(
            self.dialog.condition_table.item(0, 2).toolTip(), "编辑后的长工况名称"
        )
        self.assertEqual(
            self.dialog.condition_table.item(0, 6).toolTip(),
            "时间格式应为 yyyy-MM-dd HH:mm:ss",
        )
        self.assertEqual(self.dialog.condition_table.verticalHeader().defaultSectionSize(), 30)

    def test_resize_gives_additional_height_to_condition_table(self):
        self._load_table()
        self.dialog.show()
        self.dialog.resize(900, 620)
        APP.processEvents()
        initial_table_height = self.dialog.condition_table.height()
        initial_input_height = self.dialog.input_box.height()
        self.dialog.resize(1200, 900)
        APP.processEvents()
        self.assertGreater(self.dialog.condition_table.height(), initial_table_height)
        self.assertEqual(self.dialog.input_box.height(), initial_input_height)
        self.assertTrue(self.dialog.start_button.isVisible())


class BlfRelatedDialogsUi6Tests(unittest.TestCase):
    def test_progress_theme_updates_and_close_preserves_cancel_lifecycle(self):
        worker = FakeWorker()
        dialog = BlfSliceProgressDialog(worker)
        try:
            self.assertEqual(dialog.cancel_button.property("uiRole"), "secondary")
            worker.phase_changed.emit("scan", "正在扫描")
            worker.progress_changed.emit(2, 5, "C:/very/long/path/input.blf")
            APP.processEvents()
            self.assertEqual(dialog.phase_label.text(), "正在扫描")
            self.assertEqual(dialog.progress.maximum(), 5)
            self.assertEqual(dialog.progress.value(), 2)
            self.assertEqual(dialog.file_label.toolTip(), "C:/very/long/path/input.blf")
            dialog.show()
            dialog.close()
            APP.processEvents()
            self.assertTrue(dialog.isVisible())
            self.assertEqual(worker.cancel_calls, 1)
            self.assertFalse(dialog.cancel_button.isEnabled())
            worker.running = False
            dialog.close()
            self.assertFalse(dialog.isVisible())
        finally:
            worker.running = False
            dialog.deleteLater()

    def test_result_dialog_preserves_evidence_and_exposes_full_values(self):
        condition = ConditionSpec(2, "长工况名称", "长工况名称", datetime(2026, 1, 1))
        task = SliceTask(
            InputMode.FILE, Path("input.blf"), Path("table.csv"), Path("output"),
            (condition,), datetime(2026, 1, 1),
        )
        condition_result = ConditionResult(
            condition,
            ConditionStatus.COMPLETE,
            output_files=(Path("output/long-result-file-name.blf"),),
            message_count=42,
            warnings=("warning detail",),
        )
        result = TaskResult(
            task, TaskStatus.COMPLETED, (condition_result,),
            report_path=Path("output/report.md"),
        )
        dialog = BlfSliceResultDialog(result)
        try:
            self.assertEqual(dialog.table.item(0, 1).text(), ConditionStatus.COMPLETE.value)
            self.assertEqual(dialog.table.item(0, 2).text(), "42")
            self.assertEqual(
                dialog.table.item(0, 3).toolTip(), "output\\long-result-file-name.blf"
            )
            close_button = dialog.findChild(QDialogButtonBox).button(
                QDialogButtonBox.StandardButton.Close
            )
            self.assertEqual(close_button.property("uiRole"), "secondary")
        finally:
            dialog.deleteLater()

    def test_duplicate_dialog_actions_and_explicit_choice_are_preserved(self):
        paths = (
            Path("C:/logs/a-very-long-original-file-name.blf"),
            Path("C:/logs/a-very-long-copy-file-name.blf"),
        )
        dialog = DuplicateSelectionDialog(
            (DuplicateFileGroup(10, "a" * 64, paths),)
        )
        try:
            self.assertEqual(dialog.continue_button.property("uiRole"), "primary")
            self.assertEqual(dialog.cancel_button.property("uiRole"), "secondary")
            self.assertEqual(dialog.choices(), {})
            button = dialog._groups[0].buttons()[1]
            self.assertEqual(button.toolTip(), str(paths[1]))
            button.setChecked(True)
            dialog.continue_button.click()
            self.assertEqual(dialog.choices(), {0: paths[1]})
            self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
        finally:
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
