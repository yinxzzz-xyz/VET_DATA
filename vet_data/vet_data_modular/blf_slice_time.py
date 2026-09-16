"""Fixed-offset and time-window helpers for BLF condition slicing.

The internal time basis is a UTC Unix timestamp in seconds. python-can 4.6.1
interprets BLF ``SYSTEMTIME`` fields as UTC and adds object time offsets to
that value. Those timestamps preserve recorded wall-clock fields but still
need the task's BLF fixed offset applied exactly once.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import NewType

from .blf_slice_models import (
    MAX_UTC_OFFSET,
    MIN_UTC_OFFSET,
    UTC_OFFSET_STEP,
    ConditionSpec,
)


PythonCanBlfTimestamp = NewType("PythonCanBlfTimestamp", float)
UtcTimestamp = NewType("UtcTimestamp", float)


def validate_fixed_utc_offset(offset: timedelta) -> timedelta:
    """Return a valid fixed UTC offset or raise a descriptive exception."""
    if not isinstance(offset, timedelta):
        raise TypeError("UTC offset must be a datetime.timedelta")
    seconds = offset.total_seconds()
    if not MIN_UTC_OFFSET.total_seconds() <= seconds <= MAX_UTC_OFFSET.total_seconds():
        raise ValueError("UTC offset must be between UTC-12:00 and UTC+14:00")
    if seconds % UTC_OFFSET_STEP.total_seconds() != 0:
        raise ValueError("UTC offset must use 15-minute increments")
    return offset


def table_datetime_to_utc_timestamp(
    recorded_at: datetime,
    table_utc_offset: timedelta,
) -> UtcTimestamp:
    """Interpret a naive table wall time at a fixed offset and convert to UTC."""
    if not isinstance(recorded_at, datetime):
        raise TypeError("table time must be a datetime")
    if recorded_at.tzinfo is not None:
        raise ValueError("table time must be naive; its offset is supplied separately")
    offset = validate_fixed_utc_offset(table_utc_offset)
    return UtcTimestamp(recorded_at.replace(tzinfo=timezone(offset)).timestamp())


def blf_timestamp_to_utc_timestamp(
    timestamp: PythonCanBlfTimestamp | float,
    blf_utc_offset: timedelta,
) -> UtcTimestamp:
    """Normalize a python-can BLF timestamp using its fixed offset once.

    Call this only where a timestamp enters from ``BLFReader``. Downstream
    indexing and matching use ``UtcTimestamp`` and never apply the offset.
    """
    value = _finite_timestamp(timestamp, "BLF timestamp")
    offset = validate_fixed_utc_offset(blf_utc_offset)
    return UtcTimestamp(value - offset.total_seconds())


@dataclass(frozen=True, slots=True)
class ConditionTimeWindow:
    """A closed UTC time interval for one condition."""

    start: UtcTimestamp
    end: UtcTimestamp

    def __post_init__(self) -> None:
        start = _finite_timestamp(self.start, "window start")
        end = _finite_timestamp(self.end, "window end")
        if end < start:
            raise ValueError("window end must not be earlier than window start")
        object.__setattr__(self, "start", UtcTimestamp(start))
        object.__setattr__(self, "end", UtcTimestamp(end))

    def contains(self, timestamp: UtcTimestamp | float) -> bool:
        """Return whether a normalized timestamp is inside this closed window."""
        value = _finite_timestamp(timestamp, "timestamp")
        return self.start <= value <= self.end


def build_time_window(
    condition_time: datetime,
    before_seconds: int,
    after_seconds: int,
    table_utc_offset: timedelta,
) -> ConditionTimeWindow:
    """Build a closed UTC window around a table condition time."""
    before = _non_negative_seconds(before_seconds, "before_seconds")
    after = _non_negative_seconds(after_seconds, "after_seconds")
    center = table_datetime_to_utc_timestamp(condition_time, table_utc_offset)
    return ConditionTimeWindow(
        start=UtcTimestamp(center - before),
        end=UtcTimestamp(center + after),
    )


def build_condition_time_window(
    condition: ConditionSpec,
    table_utc_offset: timedelta,
) -> ConditionTimeWindow:
    """Build the closed UTC window for an existing stage-1 condition model."""
    return build_time_window(
        condition.recorded_at,
        condition.before_seconds,
        condition.after_seconds,
        table_utc_offset,
    )


def timestamp_in_window(
    timestamp: UtcTimestamp | float,
    window: ConditionTimeWindow,
) -> bool:
    """Return whether a normalized timestamp hits a closed time window."""
    if not isinstance(window, ConditionTimeWindow):
        raise TypeError("window must be a ConditionTimeWindow")
    return window.contains(timestamp)


def _finite_timestamp(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _non_negative_seconds(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value < 0:
        raise ValueError(f"{label} must not be negative")
    return value
