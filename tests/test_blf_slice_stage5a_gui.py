import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication

from vet_data_modular.blf_slice_dialog import (
    BlfSliceDialog, DuplicateSelectionDialog, retain_duplicate_choices,
)
from vet_data_modular.blf_slice_models import (
    BlfIndexEntry, ConditionResult, ConditionSpec, ConditionStatus, InputMode,
    SliceTask, TaskResult, TaskStatus,
)
from vet_data_modular.blf_slice_service import (
    CandidateSelectionResult, DuplicateDetectionResult, DuplicateFileGroup,
    SourceChainBuildResult, TaskValidationResult,
)
from vet_data_modular.blf_slice_table import TableParseResult
from vet_data_modular.blf_slice_progress import BlfSliceWorker
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


class Stage5aGuiTests(unittest.TestCase):
    def test_dialog_builds_immutable_snapshot_with_edited_values_and_offsets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            blf, table = root / "input.blf", root / "conditions.csv"
            blf.touch()
            table.write_text("记录时间,记录内容\n2026/9/17 10:30,原名称\n", encoding="utf-8-sig")
            dialog = BlfSliceDialog()
            dialog.input_edit.setText(str(blf))
            dialog.table_edit.setText(str(table))
            dialog.load_condition_table(table)
            dialog.condition_table.item(0, 2).setText("编辑后")
            dialog.condition_table.cellWidget(0, 4).setValue(12)
            dialog.condition_table.cellWidget(0, 5).setValue(34)
            dialog.blf_offset_combo.setCurrentIndex(dialog.blf_offset_combo.findData(-5 * 60))
            snapshot = dialog.build_task_snapshot(datetime(2026, 9, 17, 11, 0))
            self.assertEqual(snapshot.input_mode, InputMode.FILE)
            self.assertEqual(snapshot.output_dir, root)
            self.assertEqual(snapshot.blf_utc_offset, timedelta(hours=-5))
            self.assertEqual(snapshot.table_utc_offset, timedelta(hours=8))
            self.assertEqual(snapshot.conditions[0].name, "编辑后")
            self.assertEqual((snapshot.conditions[0].before_seconds, snapshot.conditions[0].after_seconds), (12, 34))
            dialog.deleteLater()

    def test_duplicate_choices_cover_none_one_and_multiple_groups(self):
        files = tuple(Path(name) for name in ("a.blf", "a-copy.blf", "b.blf", "b-copy.blf", "unique.blf"))
        first = DuplicateFileGroup(1, "a" * 64, files[:2])
        second = DuplicateFileGroup(1, "b" * 64, files[2:4])
        self.assertEqual(retain_duplicate_choices(files, (), {}), files)
        self.assertEqual(retain_duplicate_choices(files, (first,), {0: files[1]}), files[1:])
        self.assertEqual(retain_duplicate_choices(files, (first, second), {0: files[0], 1: files[3]}), (files[0], files[3], files[4]))
        with self.assertRaisesRegex(ValueError, "必须且只能保留一个"):
            retain_duplicate_choices(files, (first,), {})

    def test_duplicate_dialog_requires_explicit_single_choice_per_group(self):
        group = DuplicateFileGroup(1, "c" * 64, (Path("a.blf"), Path("b.blf")))
        dialog = DuplicateSelectionDialog((group,))
        self.assertEqual(dialog.choices(), {})
        dialog._groups[0].buttons()[1].setChecked(True)
        self.assertEqual(dialog.choices(), {0: Path("b.blf")})
        self.assertEqual(sum(button.isChecked() for button in dialog._groups[0].buttons()), 1)
        dialog.deleteLater()

    def test_worker_orchestrates_existing_services_and_emits_completed(self):
        condition = ConditionSpec(2, "工况", "工况", datetime(2026, 9, 17, 10, 0))
        task = SliceTask(InputMode.FILE, Path("input.blf"), Path("table.csv"), Path("out"), (condition,), datetime(2026, 9, 17, 10, 1))
        table = TableParseResult(task.table_path, "utf-8-sig", (condition,))
        entry = BlfIndexEntry(Path("input.blf"))
        duplicate_result = DuplicateDetectionResult(())
        candidate_result = CandidateSelectionResult((entry,), ())
        executed = TaskResult(task, TaskStatus.COMPLETED, (ConditionResult(condition, ConditionStatus.NO_DATA),), (entry,), datetime.now())
        received = []
        worker = BlfSliceWorker(task, table)
        self.assertIsInstance(worker, QThread)
        worker.completed.connect(received.append)
        with patch("vet_data_modular.blf_slice_progress.validate_task", return_value=TaskValidationResult((), (Path("input.blf"),))), \
             patch("vet_data_modular.blf_slice_progress.detect_duplicate_blf_files", return_value=duplicate_result), \
             patch("vet_data_modular.blf_slice_progress.build_blf_header_index", return_value=(entry,)), \
             patch("vet_data_modular.blf_slice_progress.select_blf_candidates", return_value=candidate_result), \
             patch("vet_data_modular.blf_slice_progress.build_blf_source_chains", return_value=SourceChainBuildResult()), \
             patch("vet_data_modular.blf_slice_progress.build_slice_execution_plan", return_value=object()), \
             patch("vet_data_modular.blf_slice_progress.execute_slice", return_value=executed), \
             patch("vet_data_modular.blf_slice_progress.write_report", return_value=Path("report.md")):
            worker.start()
            self.assertTrue(worker.wait(3000))
            APP.processEvents()
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].report_path, Path("report.md"))
        worker.deleteLater()

    def test_main_window_entry_is_independent_of_data_loading_buttons(self):
        window = MDFPlotter()
        self.assertEqual(window.blf_slice_button.text(), "BLF 工况切片")
        window.set_buttons_enabled(False)
        self.assertTrue(window.blf_slice_button.isEnabled())
        window.deleteLater()


if __name__ == "__main__":
    unittest.main()
