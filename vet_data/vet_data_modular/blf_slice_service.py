"""GUI-independent services for BLF condition slicing."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .blf_slice_models import (
    HEADER_MAX_YEAR,
    HEADER_MIN_YEAR,
    MAX_SLICE_SECONDS,
    MIN_SLICE_SECONDS,
    ConditionSpec,
    InputMode,
    SliceTask,
)
from .blf_slice_time import validate_fixed_utc_offset


SUPPORTED_TABLE_SUFFIXES = frozenset({".csv", ".xlsx"})


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One user-facing validation error with an optional condition location."""

    code: str
    message: str
    field: str
    condition_row_number: int | None = None


@dataclass(frozen=True, slots=True)
class TaskValidationResult:
    """Structured result returned before any BLF indexing or scanning starts."""

    issues: tuple[ValidationIssue, ...] = field(default_factory=tuple)
    blf_files: tuple[Path, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))
        object.__setattr__(self, "blf_files", tuple(Path(path) for path in self.blf_files))

    @property
    def is_valid(self) -> bool:
        return not self.issues


def resolve_output_directory(
    input_mode: InputMode,
    input_path: str | Path,
    selected_output_dir: str | Path | None = None,
) -> Path:
    """Resolve V1.3's default output directory before constructing a task."""
    if selected_output_dir is not None:
        return Path(selected_output_dir)
    source = Path(input_path)
    if input_mode is InputMode.FILE:
        return source.parent
    if input_mode is InputMode.FOLDER:
        return source
    raise ValueError("unsupported BLF input mode")


def list_direct_blf_files(input_mode: InputMode, input_path: str | Path) -> tuple[Path, ...]:
    """List candidate BLFs without recursion and without opening their contents."""
    source = Path(input_path)
    if input_mode is InputMode.FILE:
        if source.is_file() and source.suffix.lower() == ".blf":
            return (source,)
        return ()
    if input_mode is InputMode.FOLDER:
        files = (
            child
            for child in source.iterdir()
            if child.is_file() and child.suffix.lower() == ".blf"
        )
        return tuple(sorted(files, key=_stable_path_key))
    raise ValueError("unsupported BLF input mode")


def validate_task(task: SliceTask) -> TaskValidationResult:
    """Validate the immutable task snapshot without reading any BLF content."""
    if not isinstance(task, SliceTask):
        raise TypeError("task must be a SliceTask")

    issues: list[ValidationIssue] = []
    blf_files: tuple[Path, ...] = ()
    selected_conditions = tuple(condition for condition in task.conditions if condition.selected)

    if not selected_conditions:
        issues.append(_issue("no_selected_conditions", "至少选择一个工况。", "conditions"))
    for condition in selected_conditions:
        issues.extend(_validate_condition(condition))

    _validate_offset(task.blf_utc_offset, "blf_utc_offset", "BLF 时区", issues)
    _validate_offset(task.table_utc_offset, "table_utc_offset", "表格时区", issues)

    if task.input_mode not in (InputMode.FILE, InputMode.FOLDER):
        issues.append(_issue("invalid_input_mode", "BLF 输入模式无效。", "input_mode"))
    elif not task.input_path.exists():
        issues.append(_issue("input_not_found", "BLF 输入路径不存在。", "input_path"))
    elif task.input_mode is InputMode.FILE and not task.input_path.is_file():
        issues.append(_issue("input_not_file", "单文件输入路径必须是文件。", "input_path"))
    elif task.input_mode is InputMode.FOLDER and not task.input_path.is_dir():
        issues.append(_issue("input_not_directory", "文件夹输入路径必须是目录。", "input_path"))
    elif task.input_mode is InputMode.FILE and task.input_path.suffix.lower() != ".blf":
        issues.append(_issue("input_not_blf", "单文件输入必须是 .blf 文件。", "input_path"))
    elif not os.access(task.input_path, os.R_OK):
        issues.append(_issue("input_not_readable", "BLF 输入路径不可读。", "input_path"))
    else:
        try:
            blf_files = list_direct_blf_files(task.input_mode, task.input_path)
        except OSError as exc:
            issues.append(_issue("input_read_failed", f"无法读取 BLF 输入路径：{exc}", "input_path"))
        if not blf_files:
            issues.append(_issue("no_blf_files", "输入中没有可用的 .blf 文件。", "input_path"))

    _validate_table_path(task.table_path, issues)
    _validate_output_directory(task.output_dir, issues)
    return TaskValidationResult(tuple(issues), blf_files)


def _validate_condition(condition: ConditionSpec) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    row = condition.original_row_number
    if not isinstance(condition.name, str) or not condition.name.strip():
        issues.append(_issue("invalid_condition_name", "工况名称不能为空。", "name", row))

    recorded_at = condition.recorded_at
    if not isinstance(recorded_at, datetime):
        issues.append(_issue("invalid_condition_time", "工况时间必须是日期时间。", "recorded_at", row))
    elif recorded_at.tzinfo is not None:
        issues.append(_issue("aware_condition_time", "工况时间不能自带时区。", "recorded_at", row))
    elif not HEADER_MIN_YEAR <= recorded_at.year <= HEADER_MAX_YEAR:
        issues.append(
            _issue(
                "condition_time_out_of_range",
                f"工况时间年份必须在 {HEADER_MIN_YEAR}～{HEADER_MAX_YEAR} 之间。",
                "recorded_at",
                row,
            )
        )

    before_valid = _validate_slice_seconds(condition.before_seconds, "before_seconds", "向前秒数", row, issues)
    after_valid = _validate_slice_seconds(condition.after_seconds, "after_seconds", "向后秒数", row, issues)
    if before_valid and after_valid and condition.before_seconds == condition.after_seconds == 0:
        issues.append(
            _issue(
                "empty_time_window",
                "向前秒数和向后秒数不能同时为 0。",
                "before_seconds",
                row,
            )
        )
    return issues


def _validate_slice_seconds(
    value: object,
    field_name: str,
    label: str,
    row: int,
    issues: list[ValidationIssue],
) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append(_issue("invalid_slice_seconds", f"{label}必须是整数。", field_name, row))
        return False
    if not MIN_SLICE_SECONDS <= value <= MAX_SLICE_SECONDS:
        issues.append(
            _issue(
                "slice_seconds_out_of_range",
                f"{label}必须在 {MIN_SLICE_SECONDS}～{MAX_SLICE_SECONDS} 秒之间。",
                field_name,
                row,
            )
        )
        return False
    return True


def _validate_offset(value: object, field_name: str, label: str, issues: list[ValidationIssue]) -> None:
    try:
        validate_fixed_utc_offset(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(
            _issue(
                "invalid_utc_offset",
                f"{label}必须在 UTC-12:00～UTC+14:00 范围内，并使用 15 分钟粒度。",
                field_name,
            )
        )


def _validate_table_path(path: Path, issues: list[ValidationIssue]) -> None:
    if not path.exists():
        issues.append(_issue("table_not_found", "工况表路径不存在。", "table_path"))
    elif not path.is_file():
        issues.append(_issue("table_not_file", "工况表路径必须是文件。", "table_path"))
    elif path.suffix.lower() not in SUPPORTED_TABLE_SUFFIXES:
        issues.append(_issue("unsupported_table_type", "工况表必须是 CSV 或 XLSX 文件。", "table_path"))
    elif not os.access(path, os.R_OK):
        issues.append(_issue("table_not_readable", "工况表不可读。", "table_path"))


def _validate_output_directory(path: Path, issues: list[ValidationIssue]) -> None:
    if not path.exists():
        issues.append(_issue("output_not_found", "输出目录不存在。", "output_dir"))
    elif not path.is_dir():
        issues.append(_issue("output_not_directory", "输出路径必须是目录。", "output_dir"))
    else:
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".vet_data_write_probe_",
                suffix=".tmp",
                dir=path,
            ):
                pass
        except OSError:
            issues.append(_issue("output_not_writable", "输出目录不可写。", "output_dir"))


def _stable_path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _issue(
    code: str,
    message: str,
    field_name: str,
    row: int | None = None,
) -> ValidationIssue:
    return ValidationIssue(code, message, field_name, row)
