"""GUI-independent services for BLF condition slicing."""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from threading import Event
from pathlib import Path
from typing import Any, Callable

import can

from .blf_slice_models import (
    BLF_VOLUME_GAP_THRESHOLD_SECONDS,
    HEADER_MAX_YEAR,
    HEADER_MIN_YEAR,
    MAX_SLICE_SECONDS,
    MIN_SLICE_SECONDS,
    BlfHeaderStatus,
    BlfIndexEntry,
    BlfTimeRangeStatus,
    ConditionSpec,
    DuplicateFileStatus,
    InputMode,
    SliceTask,
)
from .blf_slice_time import (
    ConditionTimeWindow,
    blf_timestamp_to_utc_timestamp,
    validate_fixed_utc_offset,
)


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


@dataclass(frozen=True, slots=True)
class RangeScanProgress:
    """Batch progress emitted while one BLF is scanned sequentially."""

    path: Path
    file_index: int
    file_count: int
    scanned_message_count: int


    valid_frame_count: int


@dataclass(frozen=True, slots=True)
class ConditionCandidateMatch:
    """Exact candidates and unresolved files for one closed condition window."""

    window: ConditionTimeWindow
    candidate_paths: tuple[Path, ...] = field(default_factory=tuple)
    unresolved_paths: tuple[Path, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_paths",
            tuple(Path(path) for path in self.candidate_paths),
        )
        object.__setattr__(
            self,
            "unresolved_paths",
            tuple(Path(path) for path in self.unresolved_paths),
        )

    @property
    def is_complete(self) -> bool:
        return not self.unresolved_paths


@dataclass(frozen=True, slots=True)
class CandidateSelectionResult:
    """Updated index and per-window results from conservative candidate selection."""

    entries: tuple[BlfIndexEntry, ...]
    matches: tuple[ConditionCandidateMatch, ...]
    range_confirmation_paths: tuple[Path, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        object.__setattr__(self, "matches", tuple(self.matches))
        object.__setattr__(
            self,
            "range_confirmation_paths",
            tuple(Path(path) for path in self.range_confirmation_paths),
        )

    @property
    def is_complete(self) -> bool:
        return all(match.is_complete for match in self.matches)

@dataclass(frozen=True, slots=True)
class BlfSourceChain:
    """One deterministic chain of confirmed, non-overlapping physical BLFs."""

    entries: tuple[BlfIndexEntry, ...]
    gaps_seconds: tuple[float, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        gaps = tuple(float(gap) for gap in self.gaps_seconds)
        if not entries:
            raise ValueError("source chain must contain at least one BLF")
        if len(gaps) != len(entries) - 1:
            raise ValueError("source chain gaps must describe adjacent BLFs")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "gaps_seconds", gaps)

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(entry.path for entry in self.entries)


@dataclass(frozen=True, slots=True)
class SourceChainBreak:
    """A greater-than-threshold gap that starts a new source chain."""

    previous_path: Path
    next_path: Path
    gap_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "previous_path", Path(self.previous_path))
        object.__setattr__(self, "next_path", Path(self.next_path))
        object.__setattr__(self, "gap_seconds", float(self.gap_seconds))


@dataclass(frozen=True, slots=True)
class SourceChainBuildResult:
    """Confirmed chains plus entries that cannot yield a deterministic conclusion."""

    chains: tuple[BlfSourceChain, ...] = field(default_factory=tuple)
    unresolved_entries: tuple[BlfIndexEntry, ...] = field(default_factory=tuple)
    breaks: tuple[SourceChainBreak, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "chains", tuple(self.chains))
        object.__setattr__(self, "unresolved_entries", tuple(self.unresolved_entries))
        object.__setattr__(self, "breaks", tuple(self.breaks))

    @property
    def is_complete(self) -> bool:
        return not self.unresolved_entries

@dataclass(frozen=True, slots=True)
class DuplicateFileRecord:
    """Traceable result for one file in staged duplicate detection."""

    path: Path
    status: DuplicateFileStatus = DuplicateFileStatus.PENDING
    size: int | None = None
    quick_digest: str | None = None
    sha256: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))


@dataclass(frozen=True, slots=True)
class DuplicateFileGroup:
    """One exact-duplicate group ready for later single-choice confirmation."""

    size: int
    sha256: str
    paths: tuple[Path, ...]

    def __post_init__(self) -> None:
        paths = tuple(Path(path) for path in self.paths)
        if len(paths) < 2:
            raise ValueError("duplicate group must contain at least two files")
        object.__setattr__(self, "paths", paths)


@dataclass(frozen=True, slots=True)
class DuplicateDetectionProgress:
    """Progress for size, quick-digest, or full-SHA-256 work."""

    stage: str
    path: Path
    file_index: int
    file_count: int
    bytes_processed: int
    total_bytes: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))


@dataclass(frozen=True, slots=True)
class DuplicateDetectionResult:
    """Stable duplicate groups plus every per-file result."""

    files: tuple[DuplicateFileRecord, ...]
    groups: tuple[DuplicateFileGroup, ...] = field(default_factory=tuple)
    cancelled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "groups", tuple(self.groups))

    @property
    def errors(self) -> tuple[DuplicateFileRecord, ...]:
        return tuple(
            item for item in self.files
            if item.status is DuplicateFileStatus.FAILED
        )


def resolve_output_directory(
    input_mode: InputMode,
    input_path: str | Path,
    selected_output_dir: str | Path | None = None,
) -> Path:
    """Resolve V1.4's default output directory before constructing a task."""
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


def build_blf_header_index(
    blf_files: tuple[Path, ...] | list[Path],
    blf_utc_offset: timedelta,
    *,
    reader_factory: Callable[[Path], Any] = can.BLFReader,
) -> tuple[BlfIndexEntry, ...]:
    """Read only BLF headers and return isolated per-file index results.

    This stage deliberately does not iterate a reader. Header evidence remains
    separate from the effective range confirmed by a later streaming scan.
    """
    offset = validate_fixed_utc_offset(blf_utc_offset)
    return tuple(
        _index_one_blf_header(Path(path), offset, reader_factory)
        for path in blf_files
    )


def _index_one_blf_header(
    path: Path,
    blf_utc_offset: timedelta,
    reader_factory: Callable[[Path], Any],
) -> BlfIndexEntry:
    reader = None
    try:
        reader = reader_factory(path)
        raw_start = reader.start_timestamp
        raw_stop = reader.stop_timestamp
    except Exception as exc:
        return BlfIndexEntry(
            path=path,
            header_status=BlfHeaderStatus.READ_FAILED,
            header_untrusted_reasons=("reader_or_header_error",),
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if reader is not None:
            try:
                reader.stop()
            except Exception:
                pass

    reasons: list[str] = []
    start = _normalize_header_timestamp(
        raw_start,
        "start_timestamp",
        blf_utc_offset,
        reasons,
    )
    stop = _normalize_header_timestamp(
        raw_stop,
        "stop_timestamp",
        blf_utc_offset,
        reasons,
    )

    if _is_finite_number(raw_start) and _is_finite_number(raw_stop):
        if float(raw_stop) < float(raw_start):
            reasons.append("stop_timestamp_before_start_timestamp")

    header_status = (
        BlfHeaderStatus.VALID
        if not reasons
        else BlfHeaderStatus.UNTRUSTED
    )
    return BlfIndexEntry(
        path=path,
        header_status=header_status,
        header_start_timestamp_raw=(
            float(raw_start) if _is_finite_number(raw_start) else None
        ),
        header_stop_timestamp_raw=(
            float(raw_stop) if _is_finite_number(raw_stop) else None
        ),
        header_start_timestamp=start,
        header_stop_timestamp=stop,
        header_untrusted_reasons=tuple(reasons),
    )


def _normalize_header_timestamp(
    value: object,
    field_name: str,
    blf_utc_offset: timedelta,
    reasons: list[str],
) -> float | None:
    if value is None:
        reasons.append(f"{field_name}_missing")
        return None
    if not _is_finite_number(value):
        reason = (
            "not_numeric"
            if isinstance(value, bool) or not isinstance(value, (int, float))
            else "not_finite"
        )
        reasons.append(f"{field_name}_{reason}")
        return None

    numeric_value = float(value)
    normalized = float(blf_timestamp_to_utc_timestamp(numeric_value, blf_utc_offset))
    if numeric_value <= 0:
        reasons.append(f"{field_name}_not_positive")
        return normalized

    try:
        year = datetime.fromtimestamp(normalized, timezone.utc).year
    except (OverflowError, OSError, ValueError):
        reasons.append(f"{field_name}_out_of_range")
        return normalized
    if not HEADER_MIN_YEAR <= year <= HEADER_MAX_YEAR:
        reasons.append(f"{field_name}_year_out_of_range")
    return normalized


def _is_finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )




def confirm_blf_effective_ranges(
    entries: tuple[BlfIndexEntry, ...] | list[BlfIndexEntry],
    blf_utc_offset: timedelta,
    *,
    reader_factory: Callable[[Path], Any] = can.BLFReader,
    cancel_event: Event | None = None,
    progress_callback: Callable[[RangeScanProgress], None] | None = None,
    valid_frame_callback: Callable[[Path, Any, float], None] | None = None,
    progress_interval: int = 100000,
) -> tuple[BlfIndexEntry, ...]:
    """Confirm physical BLF ranges with one streaming pass per file.

    Header validity does not suppress scanning. The valid-frame callback allows
    a later stage to consume frames in this pass without retaining messages.
    """
    offset = validate_fixed_utc_offset(blf_utc_offset)
    if isinstance(progress_interval, bool) or not isinstance(progress_interval, int):
        raise TypeError("progress_interval must be an integer")
    if progress_interval <= 0:
        raise ValueError("progress_interval must be positive")

    token = cancel_event if cancel_event is not None else Event()
    source_entries = tuple(entries)
    results: list[BlfIndexEntry] = []
    cancelled = False
    for file_index, entry in enumerate(source_entries, start=1):
        if cancelled or token.is_set():
            cancelled = True
            results.append(_cancel_range_confirmation(entry))
            continue
        result, cancelled = _confirm_one_blf_effective_range(
            entry,
            offset,
            reader_factory=reader_factory,
            cancel_event=token,
            progress_callback=progress_callback,
            valid_frame_callback=valid_frame_callback,
            progress_interval=progress_interval,
            file_index=file_index,
            file_count=len(source_entries),
        )
        results.append(result)
    return tuple(results)


def _confirm_one_blf_effective_range(
    entry: BlfIndexEntry,
    blf_utc_offset: timedelta,
    *,
    reader_factory: Callable[[Path], Any],
    cancel_event: Event,
    progress_callback: Callable[[RangeScanProgress], None] | None,
    valid_frame_callback: Callable[[Path, Any, float], None] | None,
    progress_interval: int,
    file_index: int,
    file_count: int,
) -> tuple[BlfIndexEntry, bool]:
    reader = None
    scan_error: Exception | None = None
    cancelled = False
    scanned = valid = remote = error_frames = invalid = inversions = 0
    previous_raw: float | None = None
    first_raw: float | None = None
    last_raw: float | None = None
    offset_seconds = blf_utc_offset.total_seconds()
    minimum_utc = datetime(HEADER_MIN_YEAR, 1, 1, tzinfo=timezone.utc).timestamp()
    maximum_utc = datetime(HEADER_MAX_YEAR + 1, 1, 1, tzinfo=timezone.utc).timestamp()

    try:
        reader = reader_factory(entry.path)
        for message in reader:
            scanned += 1
            if scanned % progress_interval == 0:
                _emit_range_scan_progress(
                    progress_callback,
                    entry.path,
                    file_index,
                    file_count,
                    scanned,
                    valid,
                )
                if cancel_event.is_set():
                    cancelled = True
                    break

            if message.is_remote_frame:
                remote += 1
                continue
            if message.is_error_frame:
                error_frames += 1
                continue

            raw_timestamp = message.timestamp
            if not _is_finite_number(raw_timestamp):
                invalid += 1
                continue
            raw_timestamp = float(raw_timestamp)
            normalized_timestamp = raw_timestamp - offset_seconds
            if not minimum_utc <= normalized_timestamp < maximum_utc:
                invalid += 1
                continue

            if previous_raw is not None and raw_timestamp < previous_raw:
                inversions += 1
            previous_raw = raw_timestamp
            if first_raw is None:
                first_raw = raw_timestamp
            last_raw = raw_timestamp
            valid += 1
            if valid_frame_callback is not None:
                valid_frame_callback(
                    entry.path,
                    message,
                    float(
                        blf_timestamp_to_utc_timestamp(
                            raw_timestamp,
                            blf_utc_offset,
                        )
                    ),
                )
    except Exception as exc:
        scan_error = exc
    finally:
        if reader is not None:
            try:
                reader.stop()
            except Exception as exc:
                if scan_error is None:
                    scan_error = exc

    _emit_range_scan_progress(
        progress_callback,
        entry.path,
        file_index,
        file_count,
        scanned,
        valid,
    )
    cancelled = cancelled or cancel_event.is_set()
    counters = dict(
        scanned_message_count=scanned,
        valid_frame_count=valid,
        ignored_remote_frames=remote,
        ignored_error_frames=error_frames,
        invalid_timestamp_count=invalid,
        timestamp_inversion_count=inversions,
    )

    if cancelled:
        return (
            replace(
                entry,
                time_range_status=BlfTimeRangeStatus.CANCELLED,
                effective_start_timestamp=None,
                effective_stop_timestamp=None,
                **counters,
            ),
            True,
        )
    if scan_error is not None:
        return (
            replace(
                entry,
                time_range_status=BlfTimeRangeStatus.FAILED,
                effective_start_timestamp=None,
                effective_stop_timestamp=None,
                error=_append_error(entry.error, "range_scan_error", scan_error),
                **counters,
            ),
            False,
        )

    warnings = list(entry.warnings)
    if invalid:
        warnings.append(f"ignored_invalid_timestamps:{invalid}")
    if inversions:
        warnings.append(f"timestamp_inversions:{inversions}")

    if valid == 0:
        if invalid:
            return (
                replace(
                    entry,
                    time_range_status=BlfTimeRangeStatus.FAILED,
                    effective_start_timestamp=None,
                    effective_stop_timestamp=None,
                    warnings=tuple(warnings),
                    error=_append_error(
                        entry.error,
                        "range_scan_error",
                        ValueError("no valid data-frame timestamps"),
                    ),
                    **counters,
                ),
                False,
            )
        return (
            replace(
                entry,
                time_range_status=BlfTimeRangeStatus.EMPTY,
                effective_start_timestamp=None,
                effective_stop_timestamp=None,
                warnings=tuple(warnings),
                **counters,
            ),
            False,
        )

    return (
        replace(
            entry,
            time_range_status=BlfTimeRangeStatus.CONFIRMED,
            effective_start_timestamp=float(
                blf_timestamp_to_utc_timestamp(first_raw, blf_utc_offset)
            ),
            effective_stop_timestamp=float(
                blf_timestamp_to_utc_timestamp(last_raw, blf_utc_offset)
            ),
            warnings=tuple(warnings),
            **counters,
        ),
        False,
    )


def _cancel_range_confirmation(entry: BlfIndexEntry) -> BlfIndexEntry:
    return replace(
        entry,
        scanned_message_count=0,
        valid_frame_count=0,
        ignored_remote_frames=0,
        ignored_error_frames=0,
        invalid_timestamp_count=0,
        timestamp_inversion_count=0,
        time_range_status=BlfTimeRangeStatus.CANCELLED,
        effective_start_timestamp=None,
        effective_stop_timestamp=None,
    )


def _emit_range_scan_progress(
    callback: Callable[[RangeScanProgress], None] | None,
    path: Path,
    file_index: int,
    file_count: int,
    scanned_message_count: int,
    valid_frame_count: int,
) -> None:
    if callback is not None:
        callback(
            RangeScanProgress(
                path=path,
                file_index=file_index,
                file_count=file_count,
                scanned_message_count=scanned_message_count,
                valid_frame_count=valid_frame_count,
            )
        )


def _append_error(existing: str | None, label: str, exc: Exception) -> str:
    detail = f"{label}: {type(exc).__name__}: {exc}"
    return f"{existing}; {detail}" if existing else detail


def select_blf_candidates(
    entries: tuple[BlfIndexEntry, ...] | list[BlfIndexEntry],
    windows: tuple[ConditionTimeWindow, ...] | list[ConditionTimeWindow],
    blf_utc_offset: timedelta,
    *,
    reader_factory: Callable[[Path], Any] = can.BLFReader,
    cancel_event: Event | None = None,
    progress_callback: Callable[[RangeScanProgress], None] | None = None,
    valid_frame_callback: Callable[[Path, Any, float], None] | None = None,
    progress_interval: int = 100000,
) -> CandidateSelectionResult:
    """Conservatively prefilter, confirm once, then match exact closed ranges."""
    source_entries = tuple(entries)
    condition_windows = tuple(windows)
    if not all(isinstance(entry, BlfIndexEntry) for entry in source_entries):
        raise TypeError("entries must contain only BlfIndexEntry values")
    if not all(isinstance(window, ConditionTimeWindow) for window in condition_windows):
        raise TypeError("windows must contain only ConditionTimeWindow values")

    seen_paths: set[str] = set()
    for entry in source_entries:
        key = _stable_path_key(entry.path)
        if key in seen_paths:
            raise ValueError(f"duplicate BLF path: {entry.path}")
        seen_paths.add(key)

    pending_indices: list[int] = []
    for index, entry in enumerate(source_entries):
        if entry.time_range_status in (
            BlfTimeRangeStatus.CONFIRMED,
            BlfTimeRangeStatus.EMPTY,
        ):
            continue
        if entry.time_range_status is not BlfTimeRangeStatus.UNCONFIRMED:
            continue
        if any(
            not _header_safely_excludes_window(entry, window)
            for window in condition_windows
        ):
            pending_indices.append(index)

    updated_entries = list(source_entries)
    confirmation_paths = tuple(source_entries[index].path for index in pending_indices)
    if pending_indices:
        confirmed = confirm_blf_effective_ranges(
            [source_entries[index] for index in pending_indices],
            blf_utc_offset,
            reader_factory=reader_factory,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            valid_frame_callback=valid_frame_callback,
            progress_interval=progress_interval,
        )
        for index, confirmed_entry in zip(pending_indices, confirmed):
            updated_entries[index] = confirmed_entry

    matches: list[ConditionCandidateMatch] = []
    for window in condition_windows:
        candidates: list[Path] = []
        unresolved: list[Path] = []
        for entry in updated_entries:
            if _has_usable_effective_range(entry):
                if _closed_ranges_overlap(
                    entry.effective_start_timestamp,
                    entry.effective_stop_timestamp,
                    window.start,
                    window.end,
                ):
                    candidates.append(entry.path)
                continue
            if entry.time_range_status is BlfTimeRangeStatus.EMPTY:
                continue
            if (
                entry.time_range_status is BlfTimeRangeStatus.UNCONFIRMED
                and _header_safely_excludes_window(entry, window)
            ):
                continue
            unresolved.append(entry.path)
        matches.append(
            ConditionCandidateMatch(
                window=window,
                candidate_paths=tuple(candidates),
                unresolved_paths=tuple(unresolved),
            )
        )

    return CandidateSelectionResult(
        entries=tuple(updated_entries),
        matches=tuple(matches),
        range_confirmation_paths=confirmation_paths,
    )


def _header_safely_excludes_window(
    entry: BlfIndexEntry,
    window: ConditionTimeWindow,
) -> bool:
    return (
        entry.header_status is BlfHeaderStatus.VALID
        and _is_finite_number(entry.header_start_timestamp)
        and window.end < float(entry.header_start_timestamp)
    )


def _has_usable_effective_range(entry: BlfIndexEntry) -> bool:
    return (
        entry.time_range_status is BlfTimeRangeStatus.CONFIRMED
        and _is_finite_number(entry.effective_start_timestamp)
        and _is_finite_number(entry.effective_stop_timestamp)
        and float(entry.effective_start_timestamp)
        <= float(entry.effective_stop_timestamp)
    )


def _closed_ranges_overlap(
    first_start: float,
    first_end: float,
    second_start: float,
    second_end: float,
) -> bool:
    return first_start <= second_end and second_start <= first_end

def build_blf_source_chains(
    entries: tuple[BlfIndexEntry, ...] | list[BlfIndexEntry],
) -> SourceChainBuildResult:
    """Build stable source chains solely from confirmed effective ranges."""
    indexed_entries = tuple(entries)
    if any(not isinstance(entry, BlfIndexEntry) for entry in indexed_entries):
        raise TypeError("entries must contain only BlfIndexEntry values")

    normalized_paths: set[str] = set()
    for entry in indexed_entries:
        path_key = _stable_path_key(entry.path)
        if path_key in normalized_paths:
            raise ValueError(f"duplicate BLF path: {entry.path}")
        normalized_paths.add(path_key)

    confirmed: list[BlfIndexEntry] = []
    unresolved: list[BlfIndexEntry] = []
    for entry in indexed_entries:
        if entry.time_range_status is BlfTimeRangeStatus.EMPTY:
            continue
        if _has_usable_effective_range(entry):
            confirmed.append(entry)
        else:
            unresolved.append(entry)

    confirmed.sort(
        key=lambda entry: (
            float(entry.effective_start_timestamp),
            _stable_path_key(entry.path),
        )
    )
    chain_entries: list[list[BlfIndexEntry]] = []
    chain_gaps: list[list[float]] = []
    breaks: list[SourceChainBreak] = []

    for entry in confirmed:
        entry_start = float(entry.effective_start_timestamp)
        joinable: list[tuple[float, str, int]] = []
        non_overlapping: list[tuple[float, str, int]] = []
        for chain_index, chain in enumerate(chain_entries):
            tail = chain[-1]
            gap = entry_start - float(tail.effective_stop_timestamp)
            candidate = (gap, _stable_path_key(chain[0].path), chain_index)
            if gap > 0:
                non_overlapping.append(candidate)
                if gap <= BLF_VOLUME_GAP_THRESHOLD_SECONDS:
                    joinable.append(candidate)

        if joinable:
            gap, _, chain_index = min(joinable)
            chain_entries[chain_index].append(entry)
            chain_gaps[chain_index].append(gap)
            continue

        if non_overlapping:
            gap, _, chain_index = min(non_overlapping)
            if gap > BLF_VOLUME_GAP_THRESHOLD_SECONDS:
                breaks.append(
                    SourceChainBreak(
                        previous_path=chain_entries[chain_index][-1].path,
                        next_path=entry.path,
                        gap_seconds=gap,
                    )
                )
        chain_entries.append([entry])
        chain_gaps.append([])

    chains = tuple(
        BlfSourceChain(entries=tuple(chain), gaps_seconds=tuple(gaps))
        for chain, gaps in zip(chain_entries, chain_gaps)
    )
    return SourceChainBuildResult(
        chains=chains,
        unresolved_entries=tuple(unresolved),
        breaks=tuple(breaks),
    )

class _DuplicateDetectionCancelled(Exception):
    pass


def detect_duplicate_blf_files(
    blf_files: tuple[Path, ...] | list[Path],
    *,
    cancel_event: Event | None = None,
    progress_callback: Callable[[DuplicateDetectionProgress], None] | None = None,
    quick_chunk_size: int = 64 * 1024,
    hash_chunk_size: int = 1024 * 1024,
    stat_file: Callable[[Path], Any] = os.stat,
    open_file: Callable[..., Any] = open,
) -> DuplicateDetectionResult:
    """Detect byte-identical BLFs with size, quick digest, then full SHA-256."""
    if isinstance(quick_chunk_size, bool) or not isinstance(quick_chunk_size, int):
        raise TypeError("quick_chunk_size must be an integer")
    if isinstance(hash_chunk_size, bool) or not isinstance(hash_chunk_size, int):
        raise TypeError("hash_chunk_size must be an integer")
    if quick_chunk_size <= 0 or hash_chunk_size <= 0:
        raise ValueError("digest chunk sizes must be positive")

    paths = tuple(sorted((Path(path) for path in blf_files), key=_stable_path_key))
    seen: set[str] = set()
    for path in paths:
        key = _stable_path_key(path)
        if key in seen:
            raise ValueError(f"duplicate BLF path: {path}")
        seen.add(key)

    token = cancel_event if cancel_event is not None else Event()
    records = {
        _stable_path_key(path): DuplicateFileRecord(path=path)
        for path in paths
    }
    file_count = len(paths)
    path_indices = {path: index for index, path in enumerate(paths, start=1)}
    groups: list[DuplicateFileGroup] = []

    for file_index, path in enumerate(paths, start=1):
        if token.is_set():
            return _cancel_duplicate_detection(records, paths, groups)
        key = _stable_path_key(path)
        try:
            size = int(stat_file(path).st_size)
            if size < 0:
                raise ValueError("file size cannot be negative")
            records[key] = replace(records[key], size=size)
        except Exception as exc:
            records[key] = replace(
                records[key],
                status=DuplicateFileStatus.FAILED,
                error=f"size_error: {type(exc).__name__}: {exc}",
            )
        _emit_duplicate_progress(
            progress_callback, "size", path, file_index, file_count, 0, 0
        )
        if token.is_set():
            return _cancel_duplicate_detection(records, paths, groups)

    size_groups: dict[int, list[Path]] = {}
    for path in paths:
        record = records[_stable_path_key(path)]
        if record.status is DuplicateFileStatus.PENDING:
            size_groups.setdefault(record.size, []).append(path)

    quick_candidates: list[list[Path]] = []
    for size in sorted(size_groups):
        group = size_groups[size]
        if len(group) == 1:
            key = _stable_path_key(group[0])
            records[key] = replace(
                records[key], status=DuplicateFileStatus.SIZE_UNIQUE
            )
            continue

        digest_groups: dict[str, list[Path]] = {}
        for path in group:
            if token.is_set():
                return _cancel_duplicate_detection(records, paths, groups)
            key = _stable_path_key(path)
            try:
                digest = _quick_file_digest(
                    path,
                    size,
                    quick_chunk_size,
                    token,
                    open_file,
                    progress_callback,
                    path_indices[path],
                    file_count,
                )
                records[key] = replace(records[key], quick_digest=digest)
                digest_groups.setdefault(digest, []).append(path)
            except _DuplicateDetectionCancelled:
                return _cancel_duplicate_detection(records, paths, groups)
            except Exception as exc:
                records[key] = replace(
                    records[key],
                    status=DuplicateFileStatus.FAILED,
                    error=f"quick_digest_error: {type(exc).__name__}: {exc}",
                )

        for digest in sorted(digest_groups):
            candidates = digest_groups[digest]
            if len(candidates) == 1:
                key = _stable_path_key(candidates[0])
                records[key] = replace(
                    records[key],
                    status=DuplicateFileStatus.QUICK_DIGEST_UNIQUE,
                )
            else:
                quick_candidates.append(candidates)

    for candidates in quick_candidates:
        full_groups: dict[str, list[Path]] = {}
        for path in candidates:
            if token.is_set():
                return _cancel_duplicate_detection(records, paths, groups)
            key = _stable_path_key(path)
            size = records[key].size
            try:
                digest = _full_file_sha256(
                    path,
                    size,
                    hash_chunk_size,
                    token,
                    open_file,
                    progress_callback,
                    path_indices[path],
                    file_count,
                )
                records[key] = replace(records[key], sha256=digest)
                full_groups.setdefault(digest, []).append(path)
            except _DuplicateDetectionCancelled:
                return _cancel_duplicate_detection(records, paths, groups)
            except Exception as exc:
                records[key] = replace(
                    records[key],
                    status=DuplicateFileStatus.FAILED,
                    error=f"sha256_error: {type(exc).__name__}: {exc}",
                )

        for digest in sorted(full_groups):
            matches = tuple(full_groups[digest])
            if len(matches) == 1:
                key = _stable_path_key(matches[0])
                records[key] = replace(
                    records[key],
                    status=DuplicateFileStatus.FULL_DIGEST_UNIQUE,
                )
                continue
            for path in matches:
                key = _stable_path_key(path)
                records[key] = replace(
                    records[key],
                    status=DuplicateFileStatus.DUPLICATE,
                )
            size = records[_stable_path_key(matches[0])].size
            groups.append(
                DuplicateFileGroup(size=size, sha256=digest, paths=matches)
            )

    groups.sort(key=lambda group: tuple(_stable_path_key(path) for path in group.paths))
    return DuplicateDetectionResult(
        files=tuple(records[_stable_path_key(path)] for path in paths),
        groups=tuple(groups),
    )


def _quick_file_digest(
    path: Path,
    size: int,
    chunk_size: int,
    cancel_event: Event,
    open_file: Callable[..., Any],
    progress_callback: Callable[[DuplicateDetectionProgress], None] | None,
    file_index: int,
    file_count: int,
) -> str:
    digest = hashlib.sha256()
    digest.update(size.to_bytes(16, "big", signed=False))
    sample_size = min(size, chunk_size)
    total_bytes = sample_size * 2
    processed = 0
    with open_file(path, "rb") as stream:
        if cancel_event.is_set():
            raise _DuplicateDetectionCancelled
        head = stream.read(sample_size)
        if len(head) != sample_size:
            raise OSError("short read while hashing file head")
        digest.update(len(head).to_bytes(8, "big"))
        digest.update(head)
        processed += len(head)
        _emit_duplicate_progress(
            progress_callback,
            "quick_digest",
            path,
            file_index,
            file_count,
            processed,
            total_bytes,
        )
        if cancel_event.is_set():
            raise _DuplicateDetectionCancelled

        stream.seek(max(size - sample_size, 0))
        tail = stream.read(sample_size)
        if len(tail) != sample_size:
            raise OSError("short read while hashing file tail")
        digest.update(len(tail).to_bytes(8, "big"))
        digest.update(tail)
        processed += len(tail)
        _emit_duplicate_progress(
            progress_callback,
            "quick_digest",
            path,
            file_index,
            file_count,
            processed,
            total_bytes,
        )
        if cancel_event.is_set():
            raise _DuplicateDetectionCancelled
    return digest.hexdigest()


def _full_file_sha256(
    path: Path,
    size: int,
    chunk_size: int,
    cancel_event: Event,
    open_file: Callable[..., Any],
    progress_callback: Callable[[DuplicateDetectionProgress], None] | None,
    file_index: int,
    file_count: int,
) -> str:
    digest = hashlib.sha256()
    processed = 0
    with open_file(path, "rb") as stream:
        while True:
            if cancel_event.is_set():
                raise _DuplicateDetectionCancelled
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            processed += len(chunk)
            _emit_duplicate_progress(
                progress_callback,
                "sha256",
                path,
                file_index,
                file_count,
                processed,
                size,
            )
        if processed != size:
            raise OSError(
                f"file size changed while hashing: expected {size}, read {processed}"
            )
        if size == 0:
            _emit_duplicate_progress(
                progress_callback,
                "sha256",
                path,
                file_index,
                file_count,
                0,
                0,
            )
            if cancel_event.is_set():
                raise _DuplicateDetectionCancelled
    return digest.hexdigest()


def _emit_duplicate_progress(
    callback: Callable[[DuplicateDetectionProgress], None] | None,
    stage: str,
    path: Path,
    file_index: int,
    file_count: int,
    bytes_processed: int,
    total_bytes: int,
) -> None:
    if callback is not None:
        callback(
            DuplicateDetectionProgress(
                stage=stage,
                path=path,
                file_index=file_index,
                file_count=file_count,
                bytes_processed=bytes_processed,
                total_bytes=total_bytes,
            )
        )


def _cancel_duplicate_detection(
    records: dict[str, DuplicateFileRecord],
    paths: tuple[Path, ...],
    groups: list[DuplicateFileGroup],
) -> DuplicateDetectionResult:
    for path in paths:
        key = _stable_path_key(path)
        if records[key].status is DuplicateFileStatus.PENDING:
            records[key] = replace(
                records[key],
                status=DuplicateFileStatus.CANCELLED,
            )
    return DuplicateDetectionResult(
        files=tuple(records[_stable_path_key(path)] for path in paths),
        groups=tuple(groups),
        cancelled=True,
    )


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
