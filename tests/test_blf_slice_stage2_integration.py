import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vet_data_modular.blf_slice_models import ConditionSpec, InputMode, SliceTask
from vet_data_modular.blf_slice_report import (
    OutputNameAllocator,
    OwnedTemporaryFiles,
    build_output_paths,
    commit_temporary_output,
)
from vet_data_modular.blf_slice_service import resolve_output_directory, validate_task
from vet_data_modular.blf_slice_time import (
    blf_timestamp_to_utc_timestamp,
    build_condition_time_window,
    table_datetime_to_utc_timestamp,
    timestamp_in_window,
)


class Stage2IntegrationTests(unittest.TestCase):
    def test_valid_task_flows_from_time_normalization_to_safe_commit(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            source = folder / "source.blf"
            source.write_bytes(b"source must remain unchanged")
            table = folder / "conditions.csv"
            table.write_text("记录时间,记录内容\n", encoding="utf-8")
            condition = ConditionSpec(
                original_row_number=2,
                original_name="原始名称",
                name="  测<试>. ",
                recorded_at=datetime(2026, 6, 29, 8, 0),
                before_seconds=10,
                after_seconds=20,
            )
            output_dir = resolve_output_directory(InputMode.FILE, source)
            task = SliceTask(
                input_mode=InputMode.FILE,
                input_path=source,
                table_path=table,
                output_dir=output_dir,
                conditions=(condition,),
                started_at=datetime(2026, 9, 16, 10, 0),
                blf_utc_offset=timedelta(hours=8),
                table_utc_offset=timedelta(hours=8),
            )

            validation = validate_task(task)
            self.assertTrue(validation.is_valid)
            window = build_condition_time_window(condition, task.table_utc_offset)
            table_timestamp = table_datetime_to_utc_timestamp(
                condition.recorded_at,
                task.table_utc_offset,
            )
            python_can_wall_timestamp = datetime(
                2026, 6, 29, 8, 0, tzinfo=timezone.utc
            ).timestamp()
            blf_timestamp = blf_timestamp_to_utc_timestamp(
                python_can_wall_timestamp,
                task.blf_utc_offset,
            )
            self.assertEqual(table_timestamp, blf_timestamp)
            self.assertTrue(timestamp_in_window(window.start, window))
            self.assertTrue(timestamp_in_window(window.end, window))

            existing = output_dir / "测试-20260629-080000.blf"
            existing.write_bytes(b"existing output")
            allocator = OutputNameAllocator(output_dir)
            name_result = allocator.allocate(condition.name, condition.recorded_at)
            self.assertEqual(name_result.final_filename, "测试-20260629-080000-1.blf")
            paths = build_output_paths(output_dir, name_result)
            self.assertTrue(paths.is_valid)
            self.assertEqual(paths.temporary_path.name, name_result.final_filename + ".part")

            paths.temporary_path.write_bytes(b"completed output")
            owned = OwnedTemporaryFiles()
            owned.register(paths.temporary_path)
            commit = commit_temporary_output(
                paths,
                allocator,
                owned,
                condition.recorded_at,
            )

            self.assertTrue(commit.committed)
            self.assertEqual(commit.paths.final_path.read_bytes(), b"completed output")
            self.assertFalse(commit.paths.temporary_path.exists())
            self.assertEqual(existing.read_bytes(), b"existing output")
            self.assertEqual(source.read_bytes(), b"source must remain unchanged")

    def test_reserved_name_failure_is_blocked_before_any_output_is_created(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            source = folder / "source.blf"
            source.write_bytes(b"source")
            table = folder / "conditions.xlsx"
            table.write_bytes(b"table")
            condition = ConditionSpec(
                original_row_number=5,
                original_name="CON.txt",
                name="CON.txt",
                recorded_at=datetime(2026, 6, 29, 8, 0),
                before_seconds=60,
                after_seconds=60,
            )
            task = SliceTask(
                input_mode=InputMode.FILE,
                input_path=source,
                table_path=table,
                output_dir=folder,
                conditions=(condition,),
                started_at=datetime(2026, 9, 16, 10, 0),
            )

            self.assertTrue(validate_task(task).is_valid)
            before = {path.name for path in folder.iterdir()}
            name_result = OutputNameAllocator(folder).allocate(
                condition.name,
                condition.recorded_at,
            )
            self.assertFalse(name_result.is_valid)
            self.assertEqual(name_result.issue.code, "windows_reserved_name")
            path_result = build_output_paths(folder, name_result)
            self.assertFalse(path_result.is_valid)
            self.assertEqual(path_result.issue.code, "invalid_output_name")
            self.assertEqual({path.name for path in folder.iterdir()}, before)
            self.assertFalse(any(path.name.endswith(".part") for path in folder.iterdir()))
            self.assertEqual(source.read_bytes(), b"source")


if __name__ == "__main__":
    unittest.main()
