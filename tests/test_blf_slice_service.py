import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from vet_data_modular.blf_slice_models import ConditionSpec, InputMode, SliceTask
from vet_data_modular.blf_slice_service import (
    list_direct_blf_files,
    resolve_output_directory,
    validate_task,
)


class TaskValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.blf_path = self.root / "source.BLF"
        self.blf_path.write_bytes(b"not scanned during validation")
        self.table_path = self.root / "conditions.csv"
        self.table_path.write_text("记录时间,记录内容\n", encoding="utf-8")
        self.output_dir = self.root / "output"
        self.output_dir.mkdir()
        self.condition = ConditionSpec(
            original_row_number=2,
            original_name="原始工况",
            name="有效工况",
            recorded_at=datetime(2026, 6, 29, 14, 30),
            before_seconds=60,
            after_seconds=60,
        )
        self.task = SliceTask(
            input_mode=InputMode.FILE,
            input_path=self.blf_path,
            table_path=self.table_path,
            output_dir=self.output_dir,
            conditions=(self.condition,),
            started_at=datetime(2026, 9, 15, 10),
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_valid_task_returns_structured_result_and_candidate_file(self):
        result = validate_task(self.task)
        self.assertTrue(result.is_valid)
        self.assertEqual(result.issues, ())
        self.assertEqual(result.blf_files, (self.blf_path,))

    def test_requires_at_least_one_selected_condition(self):
        task = replace(self.task, conditions=(replace(self.condition, selected=False),))
        result = validate_task(task)
        self.assertEqual([issue.code for issue in result.issues], ["no_selected_conditions"])

    def test_invalid_unselected_condition_does_not_block_task(self):
        invalid_unselected = replace(
            self.condition,
            original_row_number=99,
            name="",
            recorded_at=None,
            before_seconds=-1,
            after_seconds=301,
            selected=False,
        )
        result = validate_task(replace(self.task, conditions=(self.condition, invalid_unselected)))
        self.assertTrue(result.is_valid)

    def test_selected_condition_issues_identify_fields_and_source_row(self):
        invalid = replace(
            self.condition,
            original_row_number=7,
            name="   ",
            recorded_at=None,
            before_seconds=True,
            after_seconds=301,
        )
        result = validate_task(replace(self.task, conditions=(invalid,)))
        by_field = {issue.field: issue for issue in result.issues}
        self.assertEqual(
            set(by_field),
            {"name", "recorded_at", "before_seconds", "after_seconds"},
        )
        self.assertTrue(all(issue.condition_row_number == 7 for issue in result.issues))

    def test_condition_time_must_be_naive_and_in_supported_year_range(self):
        aware = replace(self.condition, recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        old = replace(self.condition, recorded_at=datetime(1999, 12, 31))
        self.assertIn("aware_condition_time", _codes(validate_task(replace(self.task, conditions=(aware,)))))
        self.assertIn("condition_time_out_of_range", _codes(validate_task(replace(self.task, conditions=(old,)))))

    def test_seconds_accept_boundaries_but_reject_both_zero(self):
        boundary = replace(self.condition, before_seconds=0, after_seconds=300)
        self.assertTrue(validate_task(replace(self.task, conditions=(boundary,))).is_valid)
        both_zero = replace(self.condition, before_seconds=0, after_seconds=0)
        self.assertIn("empty_time_window", _codes(validate_task(replace(self.task, conditions=(both_zero,)))))

    def test_seconds_reject_negative_noninteger_and_above_maximum(self):
        cases = (
            replace(self.condition, before_seconds=-1),
            replace(self.condition, before_seconds=1.5),
            replace(self.condition, after_seconds=301),
        )
        for condition in cases:
            with self.subTest(condition=condition):
                result = validate_task(replace(self.task, conditions=(condition,)))
                self.assertFalse(result.is_valid)
                self.assertTrue(
                    {"invalid_slice_seconds", "slice_seconds_out_of_range"} & _codes(result)
                )

    def test_both_fixed_offsets_are_validated(self):
        task = replace(
            self.task,
            blf_utc_offset=timedelta(hours=-12, minutes=-15),
            table_utc_offset=timedelta(minutes=1),
        )
        result = validate_task(task)
        self.assertEqual(
            {issue.field for issue in result.issues if issue.code == "invalid_utc_offset"},
            {"blf_utc_offset", "table_utc_offset"},
        )

    def test_single_file_input_requires_existing_blf_file(self):
        text_file = self.root / "source.txt"
        text_file.write_text("x", encoding="utf-8")
        result = validate_task(replace(self.task, input_path=text_file))
        self.assertIn("input_not_blf", _codes(result))
        missing = validate_task(replace(self.task, input_path=self.root / "missing.blf"))
        self.assertIn("input_not_found", _codes(missing))

    def test_folder_input_lists_only_direct_blf_files_without_scanning(self):
        direct = self.root / "direct.blf"
        direct.write_bytes(b"invalid BLF content is not opened")
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "ignored.blf").write_bytes(b"ignored")
        task = replace(self.task, input_mode=InputMode.FOLDER, input_path=self.root)
        result = validate_task(task)
        self.assertTrue(result.is_valid)
        self.assertEqual(set(result.blf_files), {self.blf_path, direct})

    def test_folder_without_direct_blf_is_rejected(self):
        folder = self.root / "empty"
        folder.mkdir()
        nested = folder / "nested"
        nested.mkdir()
        (nested / "ignored.blf").write_bytes(b"ignored")
        result = validate_task(replace(self.task, input_mode=InputMode.FOLDER, input_path=folder))
        self.assertIn("no_blf_files", _codes(result))

    def test_table_must_be_existing_csv_or_xlsx_file(self):
        unsupported = self.root / "conditions.xls"
        unsupported.write_bytes(b"x")
        result = validate_task(replace(self.task, table_path=unsupported))
        self.assertIn("unsupported_table_type", _codes(result))
        missing = validate_task(replace(self.task, table_path=self.root / "missing.csv"))
        self.assertIn("table_not_found", _codes(missing))

    def test_output_must_be_existing_writable_directory(self):
        missing = validate_task(replace(self.task, output_dir=self.root / "missing-output"))
        self.assertIn("output_not_found", _codes(missing))
        output_file = self.root / "output-file"
        output_file.write_bytes(b"x")
        not_directory = validate_task(replace(self.task, output_dir=output_file))
        self.assertIn("output_not_directory", _codes(not_directory))
        with patch(
            "vet_data_modular.blf_slice_service.tempfile.NamedTemporaryFile",
            side_effect=PermissionError("read only"),
        ):
            not_writable = validate_task(self.task)
        self.assertIn("output_not_writable", _codes(not_writable))


class InputResolutionTests(unittest.TestCase):
    def test_default_output_for_file_is_parent(self):
        self.assertEqual(
            resolve_output_directory(InputMode.FILE, Path("C:/logs/source.blf")),
            Path("C:/logs"),
        )

    def test_default_output_for_folder_is_selected_folder(self):
        self.assertEqual(
            resolve_output_directory(InputMode.FOLDER, Path("C:/logs")),
            Path("C:/logs"),
        )

    def test_explicit_output_overrides_both_defaults(self):
        selected = Path("C:/exports")
        self.assertEqual(
            resolve_output_directory(InputMode.FILE, Path("C:/logs/source.blf"), selected),
            selected,
        )
        self.assertEqual(
            resolve_output_directory(InputMode.FOLDER, Path("C:/logs"), selected),
            selected,
        )

    def test_direct_listing_is_stably_sorted_and_non_recursive(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            (folder / "z.BLF").write_bytes(b"")
            (folder / "A.blf").write_bytes(b"")
            nested = folder / "nested"
            nested.mkdir()
            (nested / "ignored.blf").write_bytes(b"")
            result = list_direct_blf_files(InputMode.FOLDER, folder)
        self.assertEqual([path.name for path in result], ["A.blf", "z.BLF"])


def _codes(result):
    return {issue.code for issue in result.issues}


if __name__ == "__main__":
    unittest.main()
