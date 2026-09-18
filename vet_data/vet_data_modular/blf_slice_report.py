"""Output naming helpers and, later, report generation for BLF slicing."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .blf_slice_models import (
    MAX_OUTPUT_BLF_FILENAME_LENGTH,
    WINDOWS_MAX_PATH_CHARACTERS,
    ConditionStatus,
    TaskResult,
)
from .blf_slice_time import build_condition_time_window


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


def write_report(task_result: TaskResult, output_dir: str | Path) -> Path:
    """Write a traceable V1.4 Markdown report without replacing an old report."""
    directory = Path(output_dir)
    timestamp = task_result.task.started_at.strftime("%Y%m%d-%H%M%S")
    base = f"BLF切片报告-{timestamp}"
    index = 0
    while True:
        suffix = "" if index == 0 else f"-{index}"
        path = directory / f"{base}{suffix}.md"
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(build_report_markdown(task_result))
        except FileExistsError:
            index += 1
            continue
        return path


def build_report_markdown(task_result: TaskResult) -> str:
    """Render report values only from the immutable task/result evidence."""
    task = task_result.task
    finished = task_result.finished_at.isoformat(sep=" ") if task_result.finished_at else "未结束"
    lines = [
        "# BLF 工况切片报告",
        "",
        "## 任务汇总",
        "",
        f"- 任务状态：{task_result.status.value}",
        f"- 开始时间：{task.started_at.isoformat(sep=' ')}",
        f"- 结束时间：{finished}",
        f"- BLF 输入：`{task.input_path}`",
        f"- 工况表：`{task.table_path}`",
        f"- 输出目录：`{task.output_dir}`",
        f"- BLF 时区：{_format_offset(task.blf_utc_offset)}",
        f"- 工况表时区：{_format_offset(task.table_utc_offset)}",
        f"- Header/范围索引耗时：{task_result.index_duration_seconds:.3f} 秒",
        f"- 工况表有效行：{task_result.table_valid_rows}",
        f"- 工况表舍弃行：{task_result.table_discarded_rows}",
        "",
        "### 状态数量",
        "",
    ]
    counts = {status: 0 for status in ConditionStatus}
    for result in task_result.condition_results:
        counts[result.status] += 1
    lines.extend(f"- {status.value}：{counts[status]}" for status in ConditionStatus)

    lines.extend(["", "## BLF 索引与实际范围", ""])
    if not task_result.blf_index:
        lines.append("- 无索引记录")
    for entry in task_result.blf_index:
        lines.extend(
            [
                f"### `{entry.path}`",
                "",
                f"- Header 状态：{entry.header_status.value}",
                f"- Header UTC 范围：{_format_timestamp(entry.header_start_timestamp)} ～ {_format_timestamp(entry.header_stop_timestamp)}",
                f"- Effective range 状态：{entry.time_range_status.value}",
                f"- 实际 UTC 范围：{_format_timestamp(entry.effective_start_timestamp)} ～ {_format_timestamp(entry.effective_stop_timestamp)}",
                f"- 扫描对象数：{entry.scanned_message_count}",
                f"- 普通 CAN/CAN FD 帧：{entry.valid_frame_count}",
                f"- 忽略 Remote/RTR：{entry.ignored_remote_frames}",
                f"- 忽略 Error Frame：{entry.ignored_error_frames}",
                f"- 错误：{entry.error or '无'}",
                "",
            ]
        )

    lines.extend(["## 重复检测与最终输入", ""])
    lines.extend(f"- {item}" for item in task_result.duplicate_summary or ("无重复检测记录",))
    lines.extend(f"- 保留：`{path}`" for path in task_result.retained_input_files)
    lines.extend(["", "## 逐工况结果", ""])
    for result in task_result.condition_results:
        condition = result.condition
        window = build_condition_time_window(condition, task.table_utc_offset)
        lines.extend(
            [
                f"### 行 {condition.original_row_number}：{condition.name}",
                "",
                f"- 原始名称：{condition.original_name}",
                f"- 工况时间：{condition.recorded_at.isoformat(sep=' ')}",
                f"- 前/后窗口：{condition.before_seconds} / {condition.after_seconds} 秒",
                f"- 目标 UTC 窗口：{_format_timestamp(window.start)} ～ {_format_timestamp(window.end)}",
                f"- 状态：{result.status.value}",
                f"- 有效帧数量：{result.message_count}",
                f"- 实际首末 UTC 报文：{_format_timestamp(result.actual_first_timestamp)} ～ {_format_timestamp(result.actual_last_timestamp)}",
                f"- 忽略 Remote/RTR：{result.ignored_remote_frames}",
                f"- 忽略 Error Frame：{result.ignored_error_frames}",
                f"- 忽略其他对象：{result.ignored_other_objects}",
                f"- 来源：{', '.join(f'`{path}`' for path in result.source_files) or '无'}",
                f"- 输出：{', '.join(f'`{path}`' for path in result.output_files) or '无'}",
                f"- 断点/缺失：{'; '.join(result.gaps) or '无'}",
                f"- 警告：{'; '.join(result.warnings) or '无'}",
                f"- 错误：{'; '.join(result.errors) or '无'}",
                "",
            ]
        )
    lines.extend(["## 任务级警告与错误", ""])
    lines.append(f"- 警告：{'; '.join(task_result.warnings) or '无'}")
    lines.append(f"- 错误：{'; '.join(task_result.errors) or '无'}")
    lines.extend(["", "## 运行事件", ""])
    lines.extend(f"- {event}" for event in task_result.log_events or ("无",))
    return "\n".join(lines) + "\n"


def _format_timestamp(value: float | None) -> str:
    if value is None:
        return "无"
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _format_offset(value) -> str:
    total_minutes = int(value.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"
