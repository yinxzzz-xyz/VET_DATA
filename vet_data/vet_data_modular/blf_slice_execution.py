"""Stage-4 source-oriented BLF slicing execution.

The module is Qt-independent.  It consumes the exact candidates and source
chains established by stage 3, scans every physical source at most once, and
streams matching ordinary CAN/CAN-FD frames to task-owned ``.blf.part`` files.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any, Callable

import can

from .blf_slice_models import (
    BlfIndexEntry,
    ConditionResult,
    ConditionSpec,
    ConditionStatus,
    SliceTask,
    TaskResult,
    TaskStatus,
)
from .blf_slice_report import (
    OutputNameAllocator,
    OutputPathResult,
    OwnedTemporaryFiles,
    build_output_paths,
    commit_temporary_output,
)
from .blf_slice_service import (
    BlfSourceChain,
    ConditionCandidateMatch,
    SourceChainBuildResult,
)
from .blf_slice_time import ConditionTimeWindow, blf_timestamp_to_utc_timestamp


@dataclass(frozen=True, slots=True)
class SlicePlanTarget:
    """One condition/source-chain output produced by a shared source scan."""

    target_id: str
    condition: ConditionSpec
    window: ConditionTimeWindow
    source_paths: tuple[Path, ...]
    unresolved_paths: tuple[Path, ...] = field(default_factory=tuple)
    coverage_gaps: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_paths", tuple(Path(p) for p in self.source_paths))
        object.__setattr__(self, "unresolved_paths", tuple(Path(p) for p in self.unresolved_paths))
        object.__setattr__(self, "coverage_gaps", tuple(self.coverage_gaps))


@dataclass(frozen=True, slots=True)
class SourceScanPlan:
    """A physical BLF and all targets served by its one sequential scan."""

    path: Path
    target_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "target_ids", tuple(self.target_ids))


@dataclass(frozen=True, slots=True)
class SliceExecutionPlan:
    targets: tuple[SlicePlanTarget, ...]
    source_scans: tuple[SourceScanPlan, ...]


@dataclass(frozen=True, slots=True)
class SliceScanProgress:
    path: Path
    file_index: int
    file_count: int
    scanned_message_count: int


def build_slice_execution_plan(
    matches: tuple[ConditionCandidateMatch, ...] | list[ConditionCandidateMatch],
    source_chains: SourceChainBuildResult,
    conditions: tuple[ConditionSpec, ...] | list[ConditionSpec],
) -> SliceExecutionPlan:
    """Build a deterministic source-first plan without re-deriving provenance."""
    condition_specs = tuple(conditions)
    if len(condition_specs) != len(tuple(matches)):
        raise ValueError("conditions must correspond one-to-one with candidate matches")
    chain_by_path: dict[str, tuple[int, BlfSourceChain]] = {}
    entry_by_path: dict[str, BlfIndexEntry] = {}
    for chain_index, chain in enumerate(source_chains.chains):
        for entry in chain.entries:
            key = _path_key(entry.path)
            chain_by_path[key] = (chain_index, chain)
            entry_by_path[key] = entry

    targets: list[SlicePlanTarget] = []
    source_targets: dict[str, list[str]] = {}
    source_paths: dict[str, Path] = {}
    for match_index, match in enumerate(matches):
        condition = condition_specs[match_index]
        candidates = {_path_key(path) for path in match.candidate_paths}
        grouped: dict[int, tuple[Path, ...]] = {}
        for path in match.candidate_paths:
            located = chain_by_path.get(_path_key(path))
            if located is None:
                continue
            chain_index, chain = located
            grouped[chain_index] = tuple(
                item for item in chain.paths if _path_key(item) in candidates
            )

        if not grouped:
            grouped[-1] = ()
        for chain_index, paths in sorted(grouped.items()):
            target_id = f"condition-{match_index}-source-{chain_index}"
            target = SlicePlanTarget(
                target_id=target_id,
                condition=condition,
                window=match.window,
                source_paths=paths,
                unresolved_paths=match.unresolved_paths,
                coverage_gaps=_coverage_gaps(match.window, paths, entry_by_path),
            )
            targets.append(target)
            for path in paths:
                key = _path_key(path)
                source_paths[key] = path
                source_targets.setdefault(key, []).append(target_id)

    ordered_source_keys = sorted(
        source_targets,
        key=lambda key: (
            float(entry_by_path[key].effective_start_timestamp),
            key,
        ),
    )
    scans = tuple(
        SourceScanPlan(source_paths[key], tuple(source_targets[key]))
        for key in ordered_source_keys
    )
    return SliceExecutionPlan(tuple(targets), scans)


def _coverage_gaps(
    window: ConditionTimeWindow,
    paths: tuple[Path, ...],
    entries: dict[str, BlfIndexEntry],
) -> tuple[str, ...]:
    ranges = sorted(
        (
            float(entries[_path_key(path)].effective_start_timestamp),
            float(entries[_path_key(path)].effective_stop_timestamp),
        )
        for path in paths
    )
    gaps: list[str] = []
    cursor = window.start
    for start, stop in ranges:
        if stop < window.start or start > window.end:
            continue
        if start > cursor:
            gaps.append(f"coverage_gap:{cursor:.6f}-{min(start, window.end):.6f}")
        cursor = max(cursor, stop)
    if cursor < window.end:
        gaps.append(f"coverage_gap:{cursor:.6f}-{window.end:.6f}")
    return tuple(gaps)


@dataclass(slots=True)
class _TargetState:
    plan: SlicePlanTarget
    paths: OutputPathResult
    writer: Any | None = None
    message_count: int = 0
    ignored_remote: int = 0
    ignored_error: int = 0
    ignored_other: int = 0
    first_timestamp: float | None = None
    last_timestamp: float | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    failed: bool = False
    cancelled: bool = False
    committed_path: Path | None = None
    started: bool = False


def execute_slice(
    task: SliceTask,
    plan: SliceExecutionPlan,
    blf_index: tuple[BlfIndexEntry, ...] = (),
    *,
    reader_factory: Callable[[Path], Any] = can.BLFReader,
    writer_factory: Callable[[Path], Any] = can.BLFWriter,
    cancel_event: Event | None = None,
    progress_callback: Callable[[SliceScanProgress], None] | None = None,
    progress_interval: int = 100000,
) -> TaskResult:
    """Execute a plan with one scan per source and atomic per-target output."""
    if progress_interval <= 0:
        raise ValueError("progress_interval must be positive")
    token = cancel_event if cancel_event is not None else Event()
    allocator = OutputNameAllocator(task.output_dir)
    owned = OwnedTemporaryFiles()
    states: dict[str, _TargetState] = {}
    for target in plan.targets:
        name = allocator.allocate(target.condition.name, target.condition.recorded_at)
        states[target.target_id] = _TargetState(
            target,
            build_output_paths(task.output_dir, name),
        )

    for file_index, scan in enumerate(plan.source_scans, start=1):
        if token.is_set():
            for state in states.values():
                if state.message_count or state.writer is not None:
                    state.cancelled = True
            break
        relevant = [states[target_id] for target_id in scan.target_ids]
        for state in relevant:
            state.started = True
        reader = None
        scanned = 0
        try:
            reader = reader_factory(scan.path)
            for message in reader:
                scanned += 1
                if scanned % progress_interval == 0:
                    _emit_progress(progress_callback, scan.path, file_index, len(plan.source_scans), scanned)
                    if token.is_set():
                        for state in relevant:
                            state.cancelled = True
                        break
                _dispatch_message(task, message, relevant, writer_factory, owned, allocator)
        except Exception as exc:
            detail = f"source_read_or_process_error:{scan.path}:{type(exc).__name__}: {exc}"
            for state in relevant:
                state.failed = True
                state.errors.append(detail)
        finally:
            if reader is not None:
                try:
                    reader.stop()
                except Exception as exc:
                    for state in relevant:
                        state.failed = True
                        state.errors.append(f"source_close_error:{scan.path}:{type(exc).__name__}: {exc}")
        _emit_progress(progress_callback, scan.path, file_index, len(plan.source_scans), scanned)
        if token.is_set():
            break

    if token.is_set():
        for state in states.values():
            if state.writer is not None or state.message_count:
                state.cancelled = True

    for state in states.values():
        _finalize_writer(state)
    for state in states.values():
        if state.failed or state.cancelled or state.message_count == 0:
            _cleanup_target(state, owned)
            continue
        commit = commit_temporary_output(
            state.paths,
            allocator,
            owned,
            state.plan.condition.recorded_at,
        )
        if commit.committed and commit.paths.final_path is not None:
            state.committed_path = commit.paths.final_path
            state.paths = commit.paths
        else:
            state.failed = True
            state.errors.append(commit.issue.message if commit.issue else "output_commit_failed")
            _cleanup_target(state, owned)
    cleanup_failures = owned.cleanup_all()
    if cleanup_failures:
        for state in states.values():
            state.warnings.extend(f"temporary_cleanup_failed:{path}" for path in cleanup_failures)

    condition_results = _aggregate_results(task, tuple(states.values()), token.is_set())
    task_status = _task_status(condition_results, token.is_set())
    return TaskResult(
        task=task,
        status=task_status,
        condition_results=condition_results,
        blf_index=tuple(blf_index),
        finished_at=datetime.now(),
        retained_input_files=tuple(scan.path for scan in plan.source_scans),
    )


def _dispatch_message(
    task: SliceTask,
    message: Any,
    states: list[_TargetState],
    writer_factory: Callable[[Path], Any],
    owned: OwnedTemporaryFiles,
    allocator: OutputNameAllocator,
) -> None:
    raw_timestamp = getattr(message, "timestamp", None)
    if isinstance(raw_timestamp, bool) or not isinstance(raw_timestamp, (int, float)) or not math.isfinite(float(raw_timestamp)):
        for state in states:
            state.ignored_other += 1
            state.warnings.append("ignored_invalid_timestamp")
        return
    timestamp = float(blf_timestamp_to_utc_timestamp(float(raw_timestamp), task.blf_utc_offset))
    matching = [state for state in states if state.plan.window.start <= timestamp <= state.plan.window.end]
    if not matching:
        return
    if not hasattr(message, "is_remote_frame") or not hasattr(message, "is_error_frame"):
        for state in matching:
            state.ignored_other += 1
        return
    if bool(message.is_remote_frame):
        for state in matching:
            state.ignored_remote += 1
        return
    if bool(message.is_error_frame):
        for state in matching:
            state.ignored_error += 1
        return
    for state in matching:
        if state.failed or state.cancelled:
            continue
        try:
            if state.writer is None:
                if not state.paths.is_valid or state.paths.temporary_path is None:
                    raise OSError(state.paths.issue.message if state.paths.issue else "invalid output path")
                while state.paths.temporary_path.exists():
                    replacement_name = allocator.allocate(
                        state.plan.condition.name,
                        state.plan.condition.recorded_at,
                    )
                    replacement_paths = build_output_paths(task.output_dir, replacement_name)
                    if not replacement_paths.is_valid or replacement_paths.temporary_path is None:
                        raise OSError(
                            replacement_paths.issue.message
                            if replacement_paths.issue
                            else "cannot reallocate temporary output"
                        )
                    state.paths = replacement_paths
                owned.register(state.paths.temporary_path)
                state.writer = writer_factory(state.paths.temporary_path)
            state.writer.on_message_received(message)
            state.message_count += 1
            state.first_timestamp = timestamp if state.first_timestamp is None else state.first_timestamp
            state.last_timestamp = timestamp
        except Exception as exc:
            state.failed = True
            state.errors.append(f"write_error:{type(exc).__name__}: {exc}")


def _finalize_writer(state: _TargetState) -> None:
    if state.writer is None:
        return
    try:
        state.writer.stop()
    except Exception as exc:
        state.failed = True
        state.errors.append(f"writer_close_error:{type(exc).__name__}: {exc}")
    finally:
        state.writer = None


def _cleanup_target(state: _TargetState, owned: OwnedTemporaryFiles) -> None:
    path = state.paths.temporary_path
    if path is not None and owned.owns(path):
        if not owned.cleanup(path):
            state.warnings.append(f"temporary_cleanup_failed:{path}")


def _aggregate_results(
    task: SliceTask,
    states: tuple[_TargetState, ...],
    cancelled: bool,
) -> tuple[ConditionResult, ...]:
    results: list[ConditionResult] = []
    for condition in task.conditions:
        if not condition.selected:
            results.append(ConditionResult(condition, ConditionStatus.NOT_SELECTED))
            continue
        related = [state for state in states if state.plan.condition is condition or state.plan.condition == condition]
        if not related:
            status = ConditionStatus.NOT_PROCESSED if cancelled else ConditionStatus.NO_DATA
            results.append(ConditionResult(condition, status))
            continue
        count = sum(state.message_count for state in related if not state.failed and not state.cancelled)
        if any(state.failed for state in related):
            status = ConditionStatus.FAILED
        elif cancelled and not any(state.started for state in related):
            status = ConditionStatus.NOT_PROCESSED
        elif any(state.cancelled for state in related):
            status = ConditionStatus.CANCELLED
        elif count == 0:
            status = ConditionStatus.NO_DATA
        elif any(state.plan.unresolved_paths or state.plan.coverage_gaps for state in related):
            status = ConditionStatus.PARTIAL
        else:
            status = ConditionStatus.COMPLETE
        first_values = [state.first_timestamp for state in related if state.first_timestamp is not None]
        last_values = [state.last_timestamp for state in related if state.last_timestamp is not None]
        results.append(
            ConditionResult(
                condition=condition,
                status=status,
                output_files=tuple(state.committed_path for state in related if state.committed_path),
                source_files=tuple(dict.fromkeys(path for state in related for path in state.plan.source_paths)),
                actual_first_timestamp=min(first_values) if first_values else None,
                actual_last_timestamp=max(last_values) if last_values else None,
                message_count=count,
                ignored_remote_frames=sum(state.ignored_remote for state in related),
                ignored_error_frames=sum(state.ignored_error for state in related),
                ignored_other_objects=sum(state.ignored_other for state in related),
                gaps=tuple(dict.fromkeys(gap for state in related for gap in state.plan.coverage_gaps)),
                warnings=tuple(warning for state in related for warning in state.warnings),
                errors=tuple(error for state in related for error in state.errors),
            )
        )
    return tuple(results)


def _task_status(results: tuple[ConditionResult, ...], cancelled: bool) -> TaskStatus:
    if cancelled:
        return TaskStatus.CANCELLED
    selected = [result for result in results if result.status is not ConditionStatus.NOT_SELECTED]
    if selected and all(result.status is ConditionStatus.FAILED for result in selected):
        return TaskStatus.FAILED
    if any(result.status in (ConditionStatus.FAILED, ConditionStatus.PARTIAL) for result in selected):
        return TaskStatus.PARTIAL_COMPLETED
    return TaskStatus.COMPLETED


def _emit_progress(
    callback: Callable[[SliceScanProgress], None] | None,
    path: Path,
    file_index: int,
    file_count: int,
    scanned: int,
) -> None:
    if callback is not None:
        callback(SliceScanProgress(path, file_index, file_count, scanned))


def _path_key(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve(strict=False))).casefold()
