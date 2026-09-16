import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from vet_data_modular.blf_slice_models import WINDOWS_MAX_PATH_CHARACTERS
from vet_data_modular.blf_slice_report import (
    OutputNameAllocator,
    OwnedTemporaryFiles,
    build_output_name,
    build_output_paths,
    build_temporary_output_path,
    commit_temporary_output,
)
from vet_data_modular import blf_slice_report


CONDITION_TIME = datetime(2026, 6, 29, 14, 30, 15)
BASE_FILENAME = "工况-20260629-143015.blf"


class OutputPathTests(unittest.TestCase):
    def test_normal_chinese_and_space_output_path(self):
        with tempfile.TemporaryDirectory(prefix="中文 输出 ") as folder_name:
            name_result = build_output_name("中文 工况", CONDITION_TIME)
            result = build_output_paths(folder_name, name_result)
        self.assertTrue(result.is_valid)
        self.assertEqual(result.final_path.name, "中文 工况-20260629-143015.blf")
        self.assertEqual(result.final_path.parent, result.temporary_path.parent)

    def test_temporary_name_appends_part_after_blf(self):
        final_path = Path("C:/output/example.blf")
        temporary_path = build_temporary_output_path(final_path)
        self.assertEqual(temporary_path.name, "example.blf.part")
        self.assertEqual(temporary_path.parent, final_path.parent)
        with self.assertRaises(ValueError):
            build_temporary_output_path(Path("C:/output/example.part"))

    def test_windows_safe_path_boundary_includes_temporary_name(self):
        name_result = build_output_name("x", CONDITION_TIME)
        temporary_filename = name_result.final_filename + ".part"
        directory_length = WINDOWS_MAX_PATH_CHARACTERS - 1 - len(temporary_filename)
        output_dir = Path("C:/") / ("a" * (directory_length - len("C:\\")))
        result = build_output_paths(output_dir, name_result)
        self.assertTrue(result.is_valid)
        self.assertEqual(len(str(result.temporary_path)), WINDOWS_MAX_PATH_CHARACTERS)

        too_long_dir = Path(str(output_dir) + "b")
        too_long = build_output_paths(too_long_dir, name_result)
        self.assertFalse(too_long.is_valid)
        self.assertEqual(too_long.issue.code, "temporary_path_too_long")

    def test_formal_path_over_limit_has_distinct_structured_error(self):
        name_result = build_output_name("x", CONDITION_TIME)
        directory_length = WINDOWS_MAX_PATH_CHARACTERS - len(name_result.final_filename)
        output_dir = Path("C:/") / ("a" * (directory_length - len("C:\\")))
        result = build_output_paths(Path(str(output_dir) + "bb"), name_result)
        self.assertFalse(result.is_valid)
        self.assertEqual(result.issue.code, "output_path_too_long")


class SafeOutputCommitTests(unittest.TestCase):
    def _prepare(self, folder: Path, name: str = "工况"):
        allocator = OutputNameAllocator(folder)
        name_result = allocator.allocate(name, CONDITION_TIME)
        paths = build_output_paths(folder, name_result)
        paths.temporary_path.write_bytes(b"new BLF")
        owned = OwnedTemporaryFiles()
        owned.register(paths.temporary_path)
        return allocator, paths, owned

    def test_commits_when_formal_file_does_not_exist(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            allocator, paths, owned = self._prepare(folder)
            result = commit_temporary_output(paths, allocator, owned, CONDITION_TIME)
            self.assertTrue(result.committed)
            self.assertEqual(result.paths.final_path.read_bytes(), b"new BLF")
            self.assertFalse(result.paths.temporary_path.exists())
            self.assertEqual(owned.paths, ())

    def test_commit_time_name_competition_reallocates_without_overwrite(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            allocator, paths, owned = self._prepare(folder)
            paths.final_path.write_bytes(b"competitor")
            result = commit_temporary_output(paths, allocator, owned, CONDITION_TIME)
            self.assertTrue(result.committed)
            self.assertEqual(paths.final_path.read_bytes(), b"competitor")
            self.assertEqual(result.paths.final_path.name, "工况-20260629-143015-1.blf")
            self.assertEqual(result.paths.final_path.read_bytes(), b"new BLF")
            self.assertFalse(result.paths.temporary_path.exists())

    def test_continuous_commit_time_conflicts_get_next_number(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            allocator, paths, owned = self._prepare(folder)
            paths.final_path.write_bytes(b"base")
            (folder / "工况-20260629-143015-1.blf").write_bytes(b"one")
            result = commit_temporary_output(paths, allocator, owned, CONDITION_TIME)
            self.assertTrue(result.committed)
            self.assertEqual(result.paths.final_path.name, "工况-20260629-143015-2.blf")
            self.assertEqual(paths.final_path.read_bytes(), b"base")
            self.assertEqual((folder / "工况-20260629-143015-1.blf").read_bytes(), b"one")

    def test_part_name_race_is_skipped_without_using_foreign_content(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            allocator, paths, owned = self._prepare(folder)
            paths.final_path.write_bytes(b"base competitor")
            real_move = blf_slice_report._move_without_overwrite
            call_count = 0

            def race_once(source, target):
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    target.write_bytes(b"foreign part")
                    raise FileExistsError(target)
                return real_move(source, target)

            with patch(
                "vet_data_modular.blf_slice_report._move_without_overwrite",
                side_effect=race_once,
            ):
                result = commit_temporary_output(paths, allocator, owned, CONDITION_TIME)

            self.assertTrue(result.committed)
            self.assertEqual(result.paths.final_path.name, "工况-20260629-143015-2.blf")
            self.assertEqual(result.paths.final_path.read_bytes(), b"new BLF")
            self.assertEqual(
                (folder / "工况-20260629-143015-1.blf.part").read_bytes(),
                b"foreign part",
            )

    def test_existing_formal_file_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            existing = folder / BASE_FILENAME
            existing.write_bytes(b"original")
            allocator, paths, owned = self._prepare(folder)
            self.assertEqual(paths.final_path.name, "工况-20260629-143015-1.blf")
            result = commit_temporary_output(paths, allocator, owned, CONDITION_TIME)
            self.assertTrue(result.committed)
            self.assertEqual(existing.read_bytes(), b"original")

    def test_commit_failure_keeps_part_owned_for_safe_cleanup(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            allocator, paths, owned = self._prepare(folder)
            with patch(
                "vet_data_modular.blf_slice_report._move_without_overwrite",
                side_effect=PermissionError("denied"),
            ):
                result = commit_temporary_output(paths, allocator, owned, CONDITION_TIME)
            self.assertFalse(result.committed)
            self.assertTrue(paths.temporary_path.exists())
            self.assertTrue(owned.owns(paths.temporary_path))
            self.assertTrue(owned.cleanup(paths.temporary_path))
            self.assertFalse(paths.temporary_path.exists())

    def test_cleanup_refuses_unowned_part_and_preserves_other_files(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            owned_part = folder / "owned.blf.part"
            foreign_part = folder / "foreign.blf.part"
            formal = folder / "existing.blf"
            owned_part.write_bytes(b"owned")
            foreign_part.write_bytes(b"foreign")
            formal.write_bytes(b"formal")
            owned = OwnedTemporaryFiles()
            owned.register(owned_part)
            with self.assertRaises(ValueError):
                owned.cleanup(foreign_part)
            self.assertTrue(owned.cleanup(owned_part))
            self.assertFalse(owned_part.exists())
            self.assertEqual(foreign_part.read_bytes(), b"foreign")
            self.assertEqual(formal.read_bytes(), b"formal")

    def test_cleanup_all_removes_only_registered_parts(self):
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            first = folder / "first.blf.part"
            second = folder / "second.blf.part"
            foreign = folder / "foreign.blf.part"
            for path in (first, second, foreign):
                path.write_bytes(path.name.encode("ascii"))
            owned = OwnedTemporaryFiles()
            owned.register(first)
            owned.register(second)
            self.assertEqual(owned.cleanup_all(), ())
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())
            self.assertTrue(foreign.exists())


if __name__ == "__main__":
    unittest.main()
