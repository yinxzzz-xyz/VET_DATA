import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from vet_data_modular.blf_slice_report import (
    OutputNameAllocator,
    build_output_name,
    clean_condition_name,
    is_windows_reserved_name,
)


CONDITION_TIME = datetime(2026, 6, 29, 14, 30, 15)
TIMESTAMP_TEXT = "20260629-143015"


class OutputNameCleaningTests(unittest.TestCase):
    def test_builds_v13_basic_filename_and_preserves_mapping(self):
        result = build_output_name("高速工况", CONDITION_TIME)
        self.assertTrue(result.is_valid)
        self.assertEqual(result.original_name, "高速工况")
        self.assertEqual(result.cleaned_name, "高速工况")
        self.assertEqual(result.final_filename, f"高速工况-{TIMESTAMP_TEXT}.blf")
        self.assertEqual(result.conflict_index, 0)

    def test_deletes_windows_illegal_characters_and_edge_spaces_dots(self):
        original = '  ..工<况>名:称"/\\|?*\x00..  '
        result = build_output_name(original, CONDITION_TIME)
        self.assertEqual(clean_condition_name(original), "工况名称")
        self.assertEqual(result.cleaned_name, "工况名称")
        self.assertEqual(result.final_filename, f"工况名称-{TIMESTAMP_TEXT}.blf")

    def test_internal_spaces_dots_and_chinese_are_preserved(self):
        self.assertEqual(clean_condition_name("  中文 工况.版本  "), "中文 工况.版本")

    def test_reserved_names_are_case_insensitive_and_include_extensions(self):
        reserved = ("CON", "con.txt", "PRN.log", "AUX", "NUL.data", "COM1", "com9.bin", "LPT1", "lpt9.txt")
        for name in reserved:
            with self.subTest(name=name):
                self.assertTrue(is_windows_reserved_name(name))
                result = build_output_name(name, CONDITION_TIME)
                self.assertFalse(result.is_valid)
                self.assertEqual(result.issue.code, "windows_reserved_name")

    def test_similar_nonreserved_names_are_allowed(self):
        for name in ("CONDITION", "COM10", "LPT10", "NUL-data"):
            with self.subTest(name=name):
                self.assertFalse(is_windows_reserved_name(name))
                self.assertTrue(build_output_name(name, CONDITION_TIME).is_valid)

    def test_empty_after_cleaning_returns_structured_error(self):
        result = build_output_name(' .<>:"/\\|?* ', CONDITION_TIME)
        self.assertFalse(result.is_valid)
        self.assertEqual(result.cleaned_name, "")
        self.assertEqual(result.issue.code, "empty_cleaned_name")
        self.assertIsNone(result.final_filename)

    def test_nontext_name_and_invalid_time_return_structured_errors(self):
        invalid_name = build_output_name(None, CONDITION_TIME)
        self.assertEqual(invalid_name.issue.code, "invalid_name_type")
        invalid_time = build_output_name("工况", None)
        self.assertEqual(invalid_time.issue.code, "invalid_condition_time")
        aware_time = build_output_name("工况", CONDITION_TIME.replace(tzinfo=timezone.utc))
        self.assertEqual(aware_time.issue.code, "invalid_condition_time")

    def test_filename_length_boundary_includes_blf_extension(self):
        exactly_180 = build_output_name("名" * 160, CONDITION_TIME)
        self.assertTrue(exactly_180.is_valid)
        self.assertEqual(len(exactly_180.final_filename), 180)
        too_long = build_output_name("名" * 161, CONDITION_TIME)
        self.assertFalse(too_long.is_valid)
        self.assertEqual(too_long.issue.code, "filename_too_long")

    def test_conflict_suffix_is_also_subject_to_180_character_limit(self):
        result = build_output_name("名" * 160, CONDITION_TIME, conflict_index=1)
        self.assertFalse(result.is_valid)
        self.assertEqual(result.issue.code, "filename_too_long")


class OutputNameAllocatorTests(unittest.TestCase):
    def test_existing_file_gets_incrementing_suffix_without_overwrite(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            existing = folder / f"工况-{TIMESTAMP_TEXT}.blf"
            existing.write_bytes(b"keep me")
            result = OutputNameAllocator(folder).allocate("工况", CONDITION_TIME)
            self.assertEqual(result.final_filename, f"工况-{TIMESTAMP_TEXT}-1.blf")
            self.assertEqual(result.conflict_index, 1)
            self.assertEqual(existing.read_bytes(), b"keep me")
            self.assertFalse((folder / result.final_filename).exists())

    def test_disk_conflicts_are_windows_case_insensitive(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            (folder / f"TEST-{TIMESTAMP_TEXT}.BLF").write_bytes(b"existing")
            result = OutputNameAllocator(folder).allocate("test", CONDITION_TIME)
        self.assertEqual(result.final_filename, f"test-{TIMESTAMP_TEXT}-1.blf")

    def test_task_reservations_allocate_continuous_suffixes(self):
        with tempfile.TemporaryDirectory() as folder_name:
            allocator = OutputNameAllocator(folder_name)
            results = [allocator.allocate("同名", CONDITION_TIME) for _ in range(4)]
        self.assertEqual(
            [result.final_filename for result in results],
            [
                f"同名-{TIMESTAMP_TEXT}.blf",
                f"同名-{TIMESTAMP_TEXT}-1.blf",
                f"同名-{TIMESTAMP_TEXT}-2.blf",
                f"同名-{TIMESTAMP_TEXT}-3.blf",
            ],
        )

    def test_disk_and_initial_task_reservations_are_combined(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            base = f"工况-{TIMESTAMP_TEXT}.blf"
            (folder / base).write_bytes(b"existing")
            allocator = OutputNameAllocator(folder, reserved_names=(f"工况-{TIMESTAMP_TEXT}-1.blf",))
            result = allocator.allocate("工况", CONDITION_TIME)
        self.assertEqual(result.final_filename, f"工况-{TIMESTAMP_TEXT}-2.blf")

    def test_allocator_returns_length_error_instead_of_truncating(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            base_result = build_output_name("名" * 160, CONDITION_TIME)
            (folder / base_result.final_filename).write_bytes(b"existing")
            result = OutputNameAllocator(folder).allocate("名" * 160, CONDITION_TIME)
        self.assertFalse(result.is_valid)
        self.assertEqual(result.issue.code, "filename_too_long")
        self.assertEqual(result.cleaned_name, "名" * 160)


if __name__ == "__main__":
    unittest.main()
