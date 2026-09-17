"""Core constants and data models for BLF condition slicing.

This module is deliberately independent of Qt and contains no parsing,
validation, file I/O, or time conversion logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path


DEFAULT_BEFORE_SECONDS = 60
DEFAULT_AFTER_SECONDS = 60
MIN_SLICE_SECONDS = 0
MAX_SLICE_SECONDS = 300
BLF_VOLUME_GAP_THRESHOLD_SECONDS = 5
DEFAULT_UTC_OFFSET = timedelta(hours=8)
MIN_UTC_OFFSET = timedelta(hours=-12)
MAX_UTC_OFFSET = timedelta(hours=14)
UTC_OFFSET_STEP = timedelta(minutes=15)
HEADER_MIN_YEAR = 2000
HEADER_MAX_YEAR = 2100
MAX_OUTPUT_BLF_FILENAME_LENGTH = 180
WINDOWS_MAX_PATH_CHARACTERS = 259


class InputMode(str, Enum):
    """How BLF input files are selected."""

    FILE = "file"
    FOLDER = "folder"


class BlfHeaderStatus(str, Enum):
    """Readability and structural validity of a BLF Header."""

    PENDING = "pending"
    VALID = "valid"
    UNTRUSTED = "untrusted"
    READ_FAILED = "read_failed"


class BlfTimeRangeStatus(str, Enum):
    """Whether a physical BLF's effective data-frame range is known."""

    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    EMPTY = "empty"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DuplicateFileStatus(str, Enum):
    """Per-file result of staged exact-duplicate detection."""

    PENDING = "pending"
    SIZE_UNIQUE = "size_unique"
    QUICK_DIGEST_UNIQUE = "quick_digest_unique"
    FULL_DIGEST_UNIQUE = "full_digest_unique"
    DUPLICATE = "duplicate"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ConditionStatus(str, Enum):
    """Final processing state defined for one condition."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    NO_DATA = "no_data"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NOT_PROCESSED = "not_processed"
    NOT_SELECTED = "not_selected"


class TaskStatus(str, Enum):
    """Lifecycle/result state of an entire slicing task."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL_COMPLETED = "partial_completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ConditionSpec:
    """One user-confirmed row from the condition table."""

    original_row_number: int
    original_name: str
    name: str
    recorded_at: datetime
    before_seconds: int = DEFAULT_BEFORE_SECONDS
    after_seconds: int = DEFAULT_AFTER_SECONDS
    selected: bool = True


@dataclass(frozen=True, slots=True)
class SliceTask:
    """Immutable snapshot handed to the slicing worker when processing starts."""

    input_mode: InputMode
    input_path: Path
    table_path: Path
    output_dir: Path
    conditions: tuple[ConditionSpec, ...]
    started_at: datetime
    blf_utc_offset: timedelta = DEFAULT_UTC_OFFSET
    table_utc_offset: timedelta = DEFAULT_UTC_OFFSET

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_path", Path(self.input_path))
        object.__setattr__(self, "table_path", Path(self.table_path))
        object.__setattr__(self, "output_dir", Path(self.output_dir))
        object.__setattr__(self, "conditions", tuple(self.conditions))


@dataclass(frozen=True, slots=True)
class BlfIndexEntry:
    """Header evidence and independently confirmed effective range for one BLF."""

    path: Path
    header_status: BlfHeaderStatus = BlfHeaderStatus.PENDING
    header_start_timestamp_raw: float | None = None
    header_stop_timestamp_raw: float | None = None
    header_start_timestamp: float | None = None
    header_stop_timestamp: float | None = None
    header_untrusted_reasons: tuple[str, ...] = field(default_factory=tuple)
    time_range_status: BlfTimeRangeStatus = BlfTimeRangeStatus.UNCONFIRMED
    effective_start_timestamp: float | None = None
    effective_stop_timestamp: float | None = None
    scanned_message_count: int = 0
    valid_frame_count: int = 0
    ignored_remote_frames: int = 0
    ignored_error_frames: int = 0
    invalid_timestamp_count: int = 0
    timestamp_inversion_count: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "header_untrusted_reasons", tuple(self.header_untrusted_reasons))


@dataclass(frozen=True, slots=True)
class ConditionResult:
    """Outputs and diagnostics produced for one condition."""

    condition: ConditionSpec
    status: ConditionStatus = ConditionStatus.NOT_PROCESSED
    output_files: tuple[Path, ...] = field(default_factory=tuple)
    source_files: tuple[Path, ...] = field(default_factory=tuple)
    actual_first_timestamp: float | None = None
    actual_last_timestamp: float | None = None
    message_count: int = 0
    ignored_remote_frames: int = 0
    ignored_error_frames: int = 0
    ignored_other_objects: int = 0
    gaps: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_files", tuple(Path(path) for path in self.output_files))
        object.__setattr__(self, "source_files", tuple(Path(path) for path in self.source_files))
        object.__setattr__(self, "gaps", tuple(self.gaps))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "errors", tuple(self.errors))


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Task-level summary suitable for the result UI and report layer."""

    task: SliceTask
    status: TaskStatus = TaskStatus.PENDING
    condition_results: tuple[ConditionResult, ...] = field(default_factory=tuple)
    blf_index: tuple[BlfIndexEntry, ...] = field(default_factory=tuple)
    finished_at: datetime | None = None
    report_path: Path | None = None
    index_duration_seconds: float = 0.0
    table_valid_rows: int = 0
    table_discarded_rows: int = 0
    table_discard_reasons: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
    duplicate_summary: tuple[str, ...] = field(default_factory=tuple)
    retained_input_files: tuple[Path, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "condition_results", tuple(self.condition_results))
        object.__setattr__(self, "blf_index", tuple(self.blf_index))
        object.__setattr__(self, "table_discard_reasons", tuple(self.table_discard_reasons))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "errors", tuple(self.errors))
        object.__setattr__(self, "duplicate_summary", tuple(self.duplicate_summary))
        object.__setattr__(
            self, "retained_input_files", tuple(Path(path) for path in self.retained_input_files)
        )
        if self.report_path is not None:
            object.__setattr__(self, "report_path", Path(self.report_path))
