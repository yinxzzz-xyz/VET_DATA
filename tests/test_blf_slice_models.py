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
        self.assertEqual(models.MIN_UTC_OFFSET, timedelta(hours=-12))
        self.assertEqual(models.MAX_UTC_OFFSET, timedelta(hours=14))
        self.assertEqual(models.UTC_OFFSET_STEP, timedelta(minutes=15))
        self.assertEqual((models.HEADER_MIN_YEAR, models.HEADER_MAX_YEAR), (2000, 2100))
        self.assertEqual(models.MAX_OUTPUT_BLF_FILENAME_LENGTH, 180)
        self.assertEqual(models.WINDOWS_MAX_PATH_CHARACTERS, 259)

    def test_status_enums_are_complete(self):
        self.assertEqual({item.value for item in models.InputMode}, {"file", "folder"})
        self.assertEqual(
            {item.value for item in models.ConditionStatus},
            {"complete", "partial", "no_data", "failed", "cancelled", "not_processed", "not_selected"},
        )
        self.assertEqual(
            {item.value for item in models.BlfHeaderStatus},
            {"pending", "valid", "untrusted", "read_failed"},
        )
        self.assertEqual(
            {item.value for item in models.BlfTimeRangeStatus},
            {"unconfirmed", "confirmed", "empty", "failed", "cancelled"},
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
        self.assertEqual(index.header_status, models.BlfHeaderStatus.PENDING)
        self.assertIsNone(index.header_start_timestamp_raw)
        self.assertIsNone(index.header_start_timestamp)
        self.assertEqual(index.header_untrusted_reasons, ())
        self.assertEqual(index.time_range_status, models.BlfTimeRangeStatus.UNCONFIRMED)
        self.assertIsNone(index.effective_start_timestamp)
        self.assertIsNone(index.effective_stop_timestamp)

        condition_result = models.ConditionResult(condition=self.condition)
        self.assertEqual(condition_result.status, models.ConditionStatus.NOT_PROCESSED)
        self.assertEqual(condition_result.message_count, 0)
        self.assertEqual(condition_result.output_files, ())

        task_result = models.TaskResult(task=self.task)
        self.assertEqual(task_result.status, models.TaskStatus.PENDING)
        self.assertEqual(task_result.condition_results, ())
        self.assertIsNone(task_result.finished_at)

    def test_header_evidence_and_confirmed_effective_range_are_independent(self):
        index = models.BlfIndexEntry(
            path="source.blf",
            header_status=models.BlfHeaderStatus.VALID,
            header_start_timestamp_raw=1000.0,
            header_stop_timestamp_raw=2000.0,
            header_start_timestamp=900.0,
            header_stop_timestamp=1900.0,
            time_range_status=models.BlfTimeRangeStatus.CONFIRMED,
            effective_start_timestamp=1200.0,
            effective_stop_timestamp=1800.0,
        )

        self.assertEqual(
            (index.header_start_timestamp, index.header_stop_timestamp),
            (900.0, 1900.0),
        )
        self.assertEqual(
            (index.effective_start_timestamp, index.effective_stop_timestamp),
            (1200.0, 1800.0),
        )

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
