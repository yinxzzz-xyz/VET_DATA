"""Order-independent time alignment for the future formula engine."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

import numpy as np

from .signal_resolver import ResolvedSignal


class InterpolationPolicy(str, Enum):
    LINEAR = "linear"
    PREVIOUS = "previous"


class TimeAlignmentError(ValueError):
    def __init__(self, code: str, message: str, signal_key: str | None = None):
        super().__init__(message)
        self.code = code
        self.signal_key = signal_key


@dataclass(frozen=True)
class AlignmentInput:
    key: str
    timestamps: np.ndarray
    samples: np.ndarray
    interpolation: InterpolationPolicy = InterpolationPolicy.LINEAR

    @classmethod
    def from_resolved(
        cls,
        resolved: ResolvedSignal,
        interpolation: InterpolationPolicy = InterpolationPolicy.LINEAR,
    ) -> "AlignmentInput":
        return cls(
            key=resolved.key,
            timestamps=resolved.timestamps,
            samples=resolved.samples,
            interpolation=interpolation,
        )


@dataclass(frozen=True)
class SignalAlignmentDiagnostic:
    interpolation: InterpolationPolicy
    input_sample_count: int
    in_range_sample_count: int
    output_sample_count: int
    interpolated: bool
    input_nan_count: int
    input_inf_count: int


@dataclass(frozen=True)
class TimeAlignmentDiagnostics:
    effective_start: float
    effective_end: float
    output_sample_count: int
    input_signal_count: int
    signals: Mapping[str, SignalAlignmentDiagnostic]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TimeAlignmentResult:
    timestamps: np.ndarray
    samples_by_key: Mapping[str, np.ndarray]
    diagnostics: TimeAlignmentDiagnostics


def align_signals(inputs: Sequence[AlignmentInput]) -> TimeAlignmentResult:
    """Align all dependencies once on their intersection's timestamp union."""
    if not inputs:
        raise TimeAlignmentError("no_inputs", "至少需要一个待对齐信号")

    prepared = [_validate_input(item) for item in inputs]
    keys = [item.key for item, _, _ in prepared]
    if len(set(keys)) != len(keys):
        raise TimeAlignmentError("duplicate_key", "待对齐信号 key 必须唯一")

    effective_start = max(timestamps[0] for _, timestamps, _ in prepared)
    effective_end = min(timestamps[-1] for _, timestamps, _ in prepared)
    if effective_start > effective_end:
        raise TimeAlignmentError("no_overlap", "参与信号不存在有效时间交集")

    in_range_timestamps = []
    in_range_counts = {}
    for item, timestamps, _ in prepared:
        mask = (timestamps >= effective_start) & (timestamps <= effective_end)
        selected = timestamps[mask]
        in_range_timestamps.append(selected)
        in_range_counts[item.key] = int(selected.size)

    timeline = np.unique(np.concatenate(in_range_timestamps))
    if timeline.size == 0:
        raise TimeAlignmentError("empty_timeline", "有效交集内没有时间点")
    if timeline[0] < effective_start or timeline[-1] > effective_end:
        raise TimeAlignmentError("timeline_out_of_range", "统一时间轴超出有效交集")

    aligned = {}
    signal_diagnostics = {}
    warnings = []
    for item, timestamps, samples in prepared:
        if timeline[0] < timestamps[0] or timeline[-1] > timestamps[-1]:
            raise TimeAlignmentError(
                "extrapolation_required", "统一时间轴需要范围外外推", item.key
            )
        if item.interpolation is InterpolationPolicy.LINEAR:
            values = np.interp(timeline, timestamps, samples)
        elif item.interpolation is InterpolationPolicy.PREVIOUS:
            indices = np.searchsorted(timestamps, timeline, side="right") - 1
            if np.any(indices < 0) or np.any(indices >= samples.size):
                raise TimeAlignmentError(
                    "extrapolation_required", "previous 对齐需要范围外取值", item.key
                )
            values = samples[indices].copy()
        else:
            raise TimeAlignmentError(
                "unsupported_interpolation", f"不支持的插值策略: {item.interpolation}", item.key
            )

        aligned[item.key] = np.asarray(values).copy()
        source_in_range = timestamps[
            (timestamps >= effective_start) & (timestamps <= effective_end)
        ]
        nan_count, inf_count = _nonfinite_counts(samples)
        if nan_count or inf_count:
            warnings.append(
                f"{item.key}: input contains {nan_count} NaN and {inf_count} Inf samples"
            )
        signal_diagnostics[item.key] = SignalAlignmentDiagnostic(
            interpolation=item.interpolation,
            input_sample_count=int(timestamps.size),
            in_range_sample_count=in_range_counts[item.key],
            output_sample_count=int(timeline.size),
            interpolated=not np.array_equal(source_in_range, timeline),
            input_nan_count=nan_count,
            input_inf_count=inf_count,
        )

    diagnostics = TimeAlignmentDiagnostics(
        effective_start=float(effective_start),
        effective_end=float(effective_end),
        output_sample_count=int(timeline.size),
        input_signal_count=len(prepared),
        signals=signal_diagnostics,
        warnings=tuple(warnings),
    )
    return TimeAlignmentResult(timeline.copy(), aligned, diagnostics)


def _validate_input(
    item: AlignmentInput,
) -> tuple[AlignmentInput, np.ndarray, np.ndarray]:
    timestamps = np.asarray(item.timestamps)
    samples = np.asarray(item.samples)
    if timestamps.ndim != 1 or samples.ndim != 1:
        raise TimeAlignmentError("not_one_dimensional", "samples/timestamps 必须是一维", item.key)
    if timestamps.size == 0:
        raise TimeAlignmentError("empty_timestamps", "timestamps 不能为空", item.key)
    if samples.size == 0:
        raise TimeAlignmentError("empty_samples", "samples 不能为空", item.key)
    if timestamps.size != samples.size:
        raise TimeAlignmentError(
            "length_mismatch", "samples/timestamps 长度不一致", item.key
        )
    try:
        timestamps = timestamps.astype(float, copy=False)
    except (TypeError, ValueError) as exc:
        raise TimeAlignmentError("invalid_timestamp_type", "timestamp 必须为数值", item.key) from exc
    if not np.all(np.isfinite(timestamps)):
        raise TimeAlignmentError("nonfinite_timestamp", "timestamp 包含 NaN 或 Inf", item.key)
    differences = np.diff(timestamps)
    if np.any(differences == 0):
        raise TimeAlignmentError("duplicate_timestamp", "timestamp 不允许重复", item.key)
    if np.any(differences < 0):
        raise TimeAlignmentError("nonincreasing_timestamp", "timestamp 必须严格递增", item.key)
    if samples.dtype.kind not in "biufc":
        raise TimeAlignmentError("unsupported_samples", "对齐仅支持数值 samples", item.key)
    return item, timestamps, samples


def _nonfinite_counts(samples: np.ndarray) -> tuple[int, int]:
    try:
        return int(np.count_nonzero(np.isnan(samples))), int(np.count_nonzero(np.isinf(samples)))
    except TypeError:
        return 0, 0
