import ast
import dataclasses
import inspect
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from vet_data_modular import blf_slice_models as models


class BlfSliceModelTests(unittest.TestCase):
    def setUp(self):
        self.condition = models.ConditionSpec(
            original_row_number=2,
            original_name="原始工况",
            name="动力不足",
            recorded_at=datetime(2026, 6, 29, 14, 30),
        )
        self.task = models.SliceTask(
            input_mode=models.InputMode.FOLDER,
            input_path=Path("input"),
            table_path=Path("conditions.xlsx"),
            output_dir=Path("output"),
            conditions=(self.condition,),
            started_at=datetime(2026, 9, 15, 10, 0),
        )

    def test_v13_constants(self):
        self.assertEqual(models.DEFAULT_BEFORE_SECONDS, 60)
        self.assertEqual(models.DEFAULT_AFTER_SECONDS, 60)
        self.assertEqual((models.MIN_SLICE_SECONDS, models.MAX_SLICE_SECONDS), (0, 300))
        self.assertEqual(models.BLF_VOLUME_GAP_THRESHOLD_SECONDS, 5)
        self.assertEqual(models.DEFAULT_UTC_OFFSET, timedelta(hours=8))
        self.assertEqual((models.HEADER_MIN_YEAR, models.HEADER_MAX_YEAR), (2000, 2100))
        self.assertEqual(models.MAX_OUTPUT_BLF_FILENAME_LENGTH, 180)

    def test_status_enums_are_complete(self):
        self.assertEqual({item.value for item in models.InputMode}, {"file", "folder"})
        self.assertEqual(
            {item.value for item in models.ConditionStatus},
            {"complete", "partial", "no_data", "failed", "cancelled", "not_processed", "not_selected"},
        )
        self.assertEqual(
            {item.value for item in models.BlfFileStatus},
            {"pending", "header_valid", "scan_recovered", "empty", "corrupt", "read_failed", "cancelled"},
        )
        self.assertEqual(
            {item.value for item in models.TaskStatus},
            {"pending", "running", "completed", "partial_completed", "cancelled", "failed"},
        )

    def test_models_can_be_created_with_expected_defaults(self):
        self.assertEqual(self.condition.before_seconds, 60)
        self.assertEqual(self.condition.after_seconds, 60)
        self.assertTrue(self.condition.selected)
        self.assertEqual(self.task.blf_utc_offset, timedelta(hours=8))
        self.assertEqual(self.task.table_utc_offset, timedelta(hours=8))

        index = models.BlfIndexEntry(path="source.blf")
        self.assertEqual(index.status, models.BlfFileStatus.PENDING)
        self.assertIsNone(index.start_timestamp)

        condition_result = models.ConditionResult(condition=self.condition)
        self.assertEqual(condition_result.status, models.ConditionStatus.NOT_PROCESSED)
        self.assertEqual(condition_result.message_count, 0)
        self.assertEqual(condition_result.output_files, ())

        task_result = models.TaskResult(task=self.task)
        self.assertEqual(task_result.status, models.TaskStatus.PENDING)
        self.assertEqual(task_result.condition_results, ())
        self.assertIsNone(task_result.finished_at)

    def test_slice_task_is_an_immutable_snapshot(self):
        source_conditions = [self.condition]
        task = dataclasses.replace(self.task, conditions=source_conditions)
        source_conditions.clear()
        self.assertEqual(task.conditions, (self.condition,))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            task.output_dir = Path("changed")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            task.conditions[0].name = "changed"

    def test_result_collections_are_snapshots(self):
        output_files = [Path("one.blf")]
        result = models.ConditionResult(condition=self.condition, output_files=output_files)
        output_files.append(Path("two.blf"))
        self.assertEqual(result.output_files, (Path("one.blf"),))

    def test_model_module_has_no_pyqt_import(self):
        tree = ast.parse(inspect.getsource(models))
        imported_roots = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_roots.update(
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertNotIn("PyQt6", imported_roots)


if __name__ == "__main__":
    unittest.main()
