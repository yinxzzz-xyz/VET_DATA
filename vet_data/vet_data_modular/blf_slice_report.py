"""Output naming helpers and, later, report generation for BLF slicing."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .blf_slice_models import MAX_OUTPUT_BLF_FILENAME_LENGTH, WINDOWS_MAX_PATH_CHARACTERS


_WINDOWS_ILLEGAL_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_STEMS = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
        *(f"COM{number}" for number in "¹²³"),
        *(f"LPT{number}" for number in "¹²³"),
    }
)


@dataclass(frozen=True, slots=True)
class OutputNameIssue:
    """One user-facing file-name validation error."""

    code: str
    message: str
    field: str = "name"


@dataclass(frozen=True, slots=True)
class OutputNameResult:
    """Traceable mapping from a condition name to one allocated BLF name."""

    original_name: object
    cleaned_name: str
    final_filename: str | None = None
    conflict_index: int | None = None
    issue: OutputNameIssue | None = None

    @property
    def is_valid(self) -> bool:
        return self.issue is None and self.final_filename is not None


def clean_condition_name(name: str) -> str:
    """Delete V1.3/Windows-invalid characters and trim edge spaces/dots."""
    if not isinstance(name, str):
        raise TypeError("condition name must be a string")
    return _WINDOWS_ILLEGAL_CHARACTERS.sub("", name).strip(" .")


def is_windows_reserved_name(name: str) -> bool:
    """Return whether a cleaned name is a Windows device name, with extension."""
    if not isinstance(name, str):
        raise TypeError("name must be a string")
    normalized = name.rstrip(" .")
    stem = normalized.split(".", 1)[0].rstrip(" .").upper()
    return stem in _WINDOWS_RESERVED_STEMS


def build_output_name(
    original_name: object,
    condition_time: object,
    conflict_index: int = 0,
) -> OutputNameResult:
    """Build and validate one V1.3 BLF filename without touching the disk."""
    if not isinstance(original_name, str):
        return _error(original_name, "", "invalid_name_type", "工况名称必须是文本。")

    cleaned_name = clean_condition_name(original_name)
    if not cleaned_name:
        return _error(original_name, cleaned_name, "empty_cleaned_name", "工况名称清洗后为空。")
    if is_windows_reserved_name(cleaned_name):
        return _error(
            original_name,
            cleaned_name,
            "windows_reserved_name",
            "工况名称是 Windows 保留名称。",
        )
    if not isinstance(condition_time, datetime) or condition_time.tzinfo is not None:
        return _error(
            original_name,
            cleaned_name,
            "invalid_condition_time",
            "无法使用无效的工况时间生成文件名。",
            field_name="recorded_at",
        )
    if isinstance(conflict_index, bool) or not isinstance(conflict_index, int) or conflict_index < 0:
        raise ValueError("conflict_index must be a non-negative integer")

    suffix = "" if conflict_index == 0 else f"-{conflict_index}"
    filename = f"{cleaned_name}-{condition_time:%Y%m%d-%H%M%S}{suffix}.blf"
    if len(filename) > MAX_OUTPUT_BLF_FILENAME_LENGTH:
        return _error(
            original_name,
            cleaned_name,
            "filename_too_long",
            f"输出文件名不能超过 {MAX_OUTPUT_BLF_FILENAME_LENGTH} 个字符。",
        )
    return OutputNameResult(original_name, cleaned_name, filename, conflict_index)


class OutputNameAllocator:
    """Allocate case-insensitively unique names against disk and task reserves."""

    def __init__(
        self,
        output_dir: str | Path,
        reserved_names: tuple[str | Path, ...] = (),
    ) -> None:
        self.output_dir = Path(output_dir)
        self._reserved_names = {_windows_name_key(Path(name).name) for name in reserved_names}

    @property
    def reserved_names(self) -> frozenset[str]:
        return frozenset(self._reserved_names)

    def allocate(self, original_name: object, condition_time: object) -> OutputNameResult:
        """Reserve the first available filename without creating or replacing it."""
        try:
            occupied = {
                _windows_name_key(entry.name)
                for entry in self.output_dir.iterdir()
            }
        except OSError as exc:
            return _error(
                original_name,
                _clean_for_diagnostic(original_name),
                "output_directory_unreadable",
                f"无法读取输出目录中的已有文件名：{exc}",
                field_name="output_dir",
            )
        occupied.update(self._reserved_names)

        conflict_index = 0
        while True:
            result = build_output_name(original_name, condition_time, conflict_index)
            if not result.is_valid:
                return result
            assert result.final_filename is not None
            key = _windows_name_key(result.final_filename)
            if key not in occupied:
                self._reserved_names.add(key)
                return result
            conflict_index += 1


@dataclass(frozen=True, slots=True)
class OutputPathIssue:
    """One path-level validation or commit error."""

    code: str
    message: str
    field: str = "output_dir"


@dataclass(frozen=True, slots=True)
class OutputPathResult:
    """Validated formal and temporary paths for one allocated output name."""

    name_result: OutputNameResult
    final_path: Path | None = None
    temporary_path: Path | None = None
    issue: OutputPathIssue | None = None

    @property
    def is_valid(self) -> bool:
        return self.issue is None and self.final_path is not None and self.temporary_path is not None


@dataclass(frozen=True, slots=True)
class OutputCommitResult:
    """Result of publishing one completed task-owned temporary file."""

    paths: OutputPathResult
    committed: bool
    issue: OutputPathIssue | None = None


def build_temporary_output_path(final_path: str | Path) -> Path:
    """Append ``.part`` after an existing formal ``.blf`` filename."""
    path = Path(final_path)
    if path.suffix.lower() != ".blf":
        raise ValueError("formal output path must end with .blf")
    return path.with_name(path.name + ".part")


def build_output_paths(
    output_dir: str | Path,
    name_result: OutputNameResult,
) -> OutputPathResult:
    """Build and prevalidate full Windows paths before BLF processing starts."""
    if not isinstance(name_result, OutputNameResult):
        raise TypeError("name_result must be an OutputNameResult")
    if not name_result.is_valid or name_result.final_filename is None:
        return OutputPathResult(
            name_result,
            issue=OutputPathIssue("invalid_output_name", "无法为无效文件名生成输出路径。", "name"),
        )

    filename = name_result.final_filename
    if Path(filename).name != filename:
        return OutputPathResult(
            name_result,
            issue=OutputPathIssue("filename_contains_path", "最终文件名不能包含目录。", "name"),
        )
    final_path = _absolute_path(Path(output_dir) / filename)
    temporary_path = build_temporary_output_path(final_path)
    for path, code, label in (
        (final_path, "output_path_too_long", "正式输出路径"),
        (temporary_path, "temporary_path_too_long", "临时输出路径"),
    ):
        if len(str(path)) > WINDOWS_MAX_PATH_CHARACTERS:
            return OutputPathResult(
                name_result,
                final_path,
                temporary_path,
                OutputPathIssue(
                    code,
                    f"{label}不能超过 {WINDOWS_MAX_PATH_CHARACTERS} 个字符。",
                ),
            )
    return OutputPathResult(name_result, final_path, temporary_path)


class OwnedTemporaryFiles:
    """Track and clean only temporary paths explicitly owned by this task."""

    def __init__(self) -> None:
        self._paths: dict[str, Path] = {}

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(self._paths.values())

    def register(self, path: str | Path) -> Path:
        owned_path = _absolute_path(Path(path))
        _require_part_path(owned_path)
        self._paths[_path_key(owned_path)] = owned_path
        return owned_path

    def owns(self, path: str | Path) -> bool:
        return _path_key(_absolute_path(Path(path))) in self._paths

    def move_registration(self, old_path: str | Path, new_path: str | Path) -> None:
        old = _absolute_path(Path(old_path))
        new = _absolute_path(Path(new_path))
        _require_part_path(new)
        old_key = _path_key(old)
        if old_key not in self._paths:
            raise ValueError("temporary file is not owned by this task")
        del self._paths[old_key]
        self._paths[_path_key(new)] = new

    def release(self, path: str | Path) -> None:
        self._paths.pop(_path_key(_absolute_path(Path(path))), None)

    def cleanup(self, path: str | Path) -> bool:
        owned_path = _absolute_path(Path(path))
        key = _path_key(owned_path)
        if key not in self._paths:
            raise ValueError("refusing to clean a temporary file not owned by this task")
        try:
            owned_path.unlink(missing_ok=True)
        except OSError:
            return False
        del self._paths[key]
        return True

    def cleanup_all(self) -> tuple[Path, ...]:
        failed: list[Path] = []
        for path in tuple(self._paths.values()):
            if not self.cleanup(path):
                failed.append(path)
        return tuple(failed)


def commit_temporary_output(
    paths: OutputPathResult,
    allocator: OutputNameAllocator,
    owned_files: OwnedTemporaryFiles,
    condition_time: datetime,
) -> OutputCommitResult:
    """Publish a completed part file without ever replacing a formal output."""
    if not paths.is_valid or paths.final_path is None or paths.temporary_path is None:
        return OutputCommitResult(
            paths,
            False,
            OutputPathIssue("invalid_output_paths", "输出路径尚未通过预校验。"),
        )
    current = paths
    temporary_path = current.temporary_path
    if not owned_files.owns(temporary_path):
        return OutputCommitResult(
            current,
            False,
            OutputPathIssue("temporary_file_not_owned", "临时文件不属于当前任务。"),
        )
    if not temporary_path.is_file():
        return OutputCommitResult(
            current,
            False,
            OutputPathIssue("temporary_file_missing", "临时文件不存在或不是文件。"),
        )

    while True:
        assert current.final_path is not None and current.temporary_path is not None
        try:
            _move_without_overwrite(current.temporary_path, current.final_path)
        except FileExistsError:
            while True:
                replacement = _allocate_replacement_paths(
                    allocator,
                    current.name_result.original_name,
                    condition_time,
                )
                if not replacement.is_valid:
                    return OutputCommitResult(replacement, False, replacement.issue)
                assert replacement.temporary_path is not None
                try:
                    _move_without_overwrite(current.temporary_path, replacement.temporary_path)
                except FileExistsError:
                    continue
                except OSError as exc:
                    return OutputCommitResult(
                        current,
                        False,
                        OutputPathIssue("temporary_rename_failed", f"无法调整临时文件名：{exc}"),
                    )
                owned_files.move_registration(current.temporary_path, replacement.temporary_path)
                current = replacement
                break
        except OSError as exc:
            return OutputCommitResult(
                current,
                False,
                OutputPathIssue("output_commit_failed", f"无法提交正式输出文件：{exc}"),
            )
        else:
            owned_files.release(current.temporary_path)
            return OutputCommitResult(current, True)


def _allocate_replacement_paths(
    allocator: OutputNameAllocator,
    original_name: object,
    condition_time: datetime,
) -> OutputPathResult:
    while True:
        replacement_name = allocator.allocate(original_name, condition_time)
        replacement = build_output_paths(allocator.output_dir, replacement_name)
        if not replacement.is_valid or replacement.temporary_path is None:
            return replacement
        if not replacement.temporary_path.exists():
            return replacement


def _move_without_overwrite(source: Path, target: Path) -> None:
    if source.parent != target.parent:
        raise ValueError("safe output commit requires source and target in the same directory")
    if os.name == "nt":
        os.rename(source, target)
        return
    os.link(source, target)
    source.unlink()


def _absolute_path(path: Path) -> Path:
    return path.resolve(strict=False)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path)).casefold()


def _require_part_path(path: Path) -> None:
    if not path.name.lower().endswith(".blf.part"):
        raise ValueError("owned temporary path must end with .blf.part")


def _windows_name_key(name: str) -> str:
    return os.path.normcase(name).casefold()


def _clean_for_diagnostic(name: object) -> str:
    return clean_condition_name(name) if isinstance(name, str) else ""


def _error(
    original_name: object,
    cleaned_name: str,
    code: str,
    message: str,
    field_name: str = "name",
) -> OutputNameResult:
    return OutputNameResult(
        original_name=original_name,
        cleaned_name=cleaned_name,
        issue=OutputNameIssue(code, message, field_name),
    )
