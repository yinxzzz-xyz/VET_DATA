"""Data cutout: source windows, output options and file writers.

The GUI only decides *what* window and which signals to cut; every byte of
file I/O lives here.  This module never imports Qt so the writers can run in a
worker thread and are testable without a display.
"""

from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np
import pandas as pd

import asammdf
from asammdf import Signal

from .workers import message_raw_channel


BLF_EXTENSIONS = (".blf",)
MDF_EXTENSIONS = (".mdf", ".mf4")
CSV_EXTENSIONS = (".csv", ".vbo")
CAN_LOG_EXTENSIONS = (".asc", ".trc")
ALL_EXTENSIONS = BLF_EXTENSIONS + MDF_EXTENSIONS + CSV_EXTENSIONS + CAN_LOG_EXTENSIONS

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SIGNAL_ICON_PREFIX = re.compile(r"^[^\w\s]+")
CHECK_CANCEL_INTERVAL = 4096
# BLF objects are written in capture order.  A small margin past the window end
# keeps a cut correct even if adjacent channels are interleaved by fractions of
# a second, while still avoiding a scan of the whole recording.
EARLY_STOP_MARGIN_SECONDS = 1.0


class CutoutCancelled(Exception):
    """Raised when a cutout task is cancelled between non-interruptible steps."""


class SourceKind(str, Enum):
    BLF = "blf"
    MDF = "mdf"
    CSV = "csv"
    CAN_LOG = "can_log"


class OutputFormat(str, Enum):
    BLF = "blf"
    MDF = "mdf"
    CSV = "csv"


class OutputScope(str, Enum):
    ALL = "all"
    SELECTED = "selected"


FORMAT_EXTENSIONS = {
    OutputFormat.BLF: ".blf",
    OutputFormat.MDF: ".mf4",
    OutputFormat.CSV: ".csv",
}
FORMAT_LABELS = {
    OutputFormat.BLF: "BLF",
    OutputFormat.MDF: "MDF4 (.mf4)",
    OutputFormat.CSV: "CSV",
}


def source_kind(path: str | os.PathLike) -> SourceKind:
    suffix = Path(path).suffix.lower()
    if suffix in BLF_EXTENSIONS:
        return SourceKind.BLF
    if suffix in MDF_EXTENSIONS:
        return SourceKind.MDF
    if suffix in CSV_EXTENSIONS:
        return SourceKind.CSV
    if suffix in CAN_LOG_EXTENSIONS:
        return SourceKind.CAN_LOG
    raise ValueError(f"不支持的数据截取源文件类型: {suffix or '无扩展名'}")


@dataclass(frozen=True)
class CutoutWindow:
    """Closed relative-time window taken from the current plot view."""

    start: float
    stop: float

    def __post_init__(self) -> None:
        start = _finite_number(self.start, "截取起点")
        stop = _finite_number(self.stop, "截取终点")
        if stop < start:
            raise ValueError("截取时间范围无效：终点不能早于起点")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "stop", stop)

    @property
    def duration(self) -> float:
        return self.stop - self.start

    def mask(self, timestamps: Iterable[float]) -> np.ndarray:
        values = np.asarray(timestamps)
        return (values >= self.start) & (values <= self.stop)

    def contains(self, timestamp: float) -> bool:
        return self.start <= float(timestamp) <= self.stop

    def describe(self) -> str:
        return f"{self.start:.2f} s – {self.stop:.2f} s（共 {self.duration:.2f} s）"


def _finite_number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.floating, np.integer)):
        raise TypeError(f"{label}必须是数字")
    number = float(value)
    if not np.isfinite(number):
        raise ValueError(f"{label}必须是有限数值")
    return number


@dataclass(frozen=True)
class CutoutOption:
    """One radio choice in the cut-out dialog."""

    format: OutputFormat
    scope: OutputScope
    title: str
    description: str
    enabled: bool = True
    disabled_reason: str = ""

    @property
    def key(self) -> str:
        return f"{self.format.value}:{self.scope.value}"

    @property
    def extension(self) -> str:
        return FORMAT_EXTENSIONS[self.format]

    @property
    def format_label(self) -> str:
        return FORMAT_LABELS[self.format]


@dataclass(frozen=True)
class CutoutContext:
    """Everything the dialog needs to describe and offer the cut."""

    source_path: str
    kind: SourceKind
    window: CutoutWindow
    selected_total: int = 0
    selected_available: int = 0
    file_signal_count: int = 0
    parsed_signal_count: int = 0


def build_options(context: CutoutContext) -> tuple[CutoutOption, ...]:
    """Return the two radio options for one source kind, with exact counts."""
    selected_available = max(0, int(context.selected_available))
    file_count = max(0, int(context.file_signal_count))
    parsed_count = max(0, int(context.parsed_signal_count))

    if context.kind is SourceKind.BLF:
        mdf_enabled = selected_available > 0
        if mdf_enabled:
            reason = ""
            title = f"信号数据（MDF4，仅已选 {selected_available} 个信号）"
        elif context.selected_total > 0:
            reason = "已勾选的信号在当前文件中没有可导出的数据"
            title = "信号数据（MDF4，仅已选信号）"
        else:
            reason = "左侧尚未勾选任何信号"
            title = "信号数据（MDF4，仅已选信号）"
        return (
            CutoutOption(
                OutputFormat.BLF,
                OutputScope.ALL,
                "原始报文（BLF）",
                "保留该时间段内所有总线的全部原始报文，不依赖 DBC，可直接用本软件或其他 CAN 工具打开。",
            ),
            CutoutOption(
                OutputFormat.MDF,
                OutputScope.SELECTED,
                title,
                "只导出已勾选信号在该时间段内的采样点，按各信号原始采样时间保存，不做重采样。",
                enabled=mdf_enabled,
                disabled_reason=reason,
            ),
        )

    if context.kind is SourceKind.CAN_LOG:
        selected_enabled = selected_available > 0
        all_enabled = parsed_count > 0
        selected_reason = (
            ""
            if selected_enabled
            else ("已勾选的信号尚未解析出数据" if context.selected_total > 0 else "左侧尚未勾选任何信号")
        )
        return (
            CutoutOption(
                OutputFormat.MDF,
                OutputScope.ALL,
                f"全部已解析信号（MDF4，{parsed_count} 个）",
                "保存该时间段内当前已解析的全部信号；未解析的信号不会出现在输出中。",
                enabled=all_enabled,
                disabled_reason="" if all_enabled else "当前文件尚未解析出任何信号",
            ),
            CutoutOption(
                OutputFormat.MDF,
                OutputScope.SELECTED,
                (
                    f"仅已选信号（MDF4，{selected_available} 个）"
                    if selected_available
                    else "仅已选信号（MDF4）"
                ),
                "只保存左侧已勾选且已解析的信号。",
                enabled=selected_enabled,
                disabled_reason=selected_reason,
            ),
        )

    selected_label = (
        f"仅已选信号（{selected_available} 个）" if selected_available else "仅已选信号"
    )
    if context.kind is SourceKind.MDF:
        all_label = f"全部信号（{file_count} 个）"
        all_description = "保存该时间段内源文件中的全部通道（不含公式计算通道）。"
        selected_description = "只保存左侧已勾选、且属于源文件的通道；公式计算通道不会写入。"
        selected_reason = (
            ""
            if selected_available
            else ("已勾选的信号不属于该文件" if context.selected_total else "左侧尚未勾选任何信号")
        )
        fmt = OutputFormat.MDF
    else:
        all_label = f"全部信号（{file_count} 个）"
        all_description = "保存该时间段内源文件中的全部列。"
        selected_description = "只保存左侧已勾选的信号；公式计算通道会按窗口时间插值成列。"
        selected_reason = (
            ""
            if selected_available
            else ("已勾选的信号没有可用数据" if context.selected_total else "左侧尚未勾选任何信号")
        )
        fmt = OutputFormat.CSV

    return (
        CutoutOption(
            fmt,
            OutputScope.ALL,
            all_label,
            all_description,
            enabled=file_count > 0,
            disabled_reason="" if file_count > 0 else "源文件中没有可用信号",
        ),
        CutoutOption(
            fmt,
            OutputScope.SELECTED,
            selected_label,
            selected_description,
            enabled=selected_available > 0,
            disabled_reason=selected_reason,
        ),
    )


def option_by_key(options: Sequence[CutoutOption], key: str | None) -> CutoutOption | None:
    if not key:
        return None
    for option in options:
        if option.key == key:
            return option
    return None


def default_option(options: Sequence[CutoutOption]) -> CutoutOption | None:
    """Prefer the first enabled option, which is the lossless choice."""
    for option in options:
        if option.enabled:
            return option
    return None


def default_output_name(
    source_path: str | os.PathLike,
    window: CutoutWindow,
    fmt: OutputFormat,
    scope: OutputScope,
) -> str:
    stem = Path(source_path).stem or "cut"
    suffix = ""
    if fmt is OutputFormat.BLF:
        suffix = ""
    elif scope is OutputScope.SELECTED and fmt is OutputFormat.MDF:
        suffix = "_signals"
    elif scope is OutputScope.SELECTED:
        suffix = "_selected"
    name = f"{stem}_cut_{window.start:.2f}s-{window.stop:.2f}s{suffix}"
    return sanitize_file_name(name) + FORMAT_EXTENSIONS[fmt]


def sanitize_file_name(name: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", str(name)).strip()
    cleaned = cleaned.rstrip(". ")
    return cleaned or "cut"


@dataclass(frozen=True)
class OutputPathIssue:
    ok: bool
    reason: str = ""


def validate_output_path(output_path: str | os.PathLike, source_path: str | os.PathLike) -> OutputPathIssue:
    path = Path(output_path)
    if not path.name or not path.stem:
        return OutputPathIssue(False, "请输入输出文件名。")
    folder = path.parent
    if not folder.exists():
        return OutputPathIssue(False, f"输出目录不存在：{folder}")
    if not folder.is_dir():
        return OutputPathIssue(False, f"输出目录无效：{folder}")
    try:
        same = os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(source_path)
        )
    except (TypeError, ValueError):
        same = False
    if same:
        return OutputPathIssue(False, "输出文件不能覆盖源文件，请换一个名称或目录。")
    return OutputPathIssue(True)


def clean_signal_name(display_name: str, fallback: str = "") -> str:
    """Drop the UI marker prefix (emoji) so MDF channels keep plain names."""
    text = SIGNAL_ICON_PREFIX.sub("", str(display_name or "")).strip()
    return text or str(fallback or "signal")


@dataclass(frozen=True)
class SignalSeries:
    """One in-memory signal to write into an MDF file."""

    name: str
    unit: str
    comment: str
    timestamps: object
    samples: object


@dataclass(frozen=True)
class CutoutResult:
    output_path: str
    message_count: int = 0
    channel_count: int = 0
    row_count: int = 0
    sample_count: int = 0
    bus_counts: tuple = ()
    skipped: tuple = ()
    notes: tuple = ()
    elapsed: float = 0.0

    def summary(self) -> str:
        if self.message_count:
            return f"{self.message_count:,} 条原始报文"
        if self.channel_count:
            return f"{self.channel_count:,} 个信号通道"
        return f"{self.row_count:,} 行"


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CutoutCancelled()


def mdf_signal_count(mdf) -> int:
    """Count non-master channels; asammdf only exposes groups, not a flat list."""
    total = 0
    for group in getattr(mdf, "groups", ()):
        total += max(0, len(group.channels) - 1)
    return total


@contextmanager
def atomic_output(output_path: str | os.PathLike):
    """Yield a temporary sibling path and move it into place only on success."""
    target = Path(output_path)
    temporary = target.with_name(f"{target.stem}.part{target.suffix}")
    try:
        yield temporary
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        os.replace(temporary, target)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def cut_blf_raw(
    source_path: str | os.PathLike,
    output_path: str | os.PathLike,
    window: CutoutWindow,
    *,
    mapping=None,
    bus_filter: Sequence[int] | None = None,
    cancel_event=None,
    progress_callback: Callable[[int, int], None] | None = None,
    progress_interval: int = 100_000,
    use_object_copy: bool = True,
    reader_factory=None,
    writer_factory=None,
) -> CutoutResult:
    """Copy the raw messages of one relative-time window into a new BLF.

    The BLF time base is the first message of the file, the same basis that
    parsing and plotting use for every bus.  The object-level copy path is used
    when possible (see :mod:`blf_object_copy`); the python-can message path
    remains the fallback and handles bus filtering.
    """
    import can

    started = time.perf_counter()
    if use_object_copy and bus_filter is None:
        try:
            from .blf_object_copy import RawCopyUnavailable, copy_blf_window
        except Exception:  # pragma: no cover - defensive
            copy_blf_window = None
            RawCopyUnavailable = ()
        if copy_blf_window is not None:
            try:
                copied, copied_scanned, bus_counts = copy_blf_window(
                    source_path,
                    output_path,
                    window,
                    mapping=mapping,
                    cancel_event=cancel_event,
                    progress_callback=progress_callback,
                )
                return CutoutResult(
                    output_path=str(output_path),
                    message_count=copied,
                    bus_counts=bus_counts,
                    elapsed=time.perf_counter() - started,
                )
            except RawCopyUnavailable:
                pass
            except OSError:
                # Missing/unreadable source: let the reader raise a clear error.
                pass

    if reader_factory is None:
        reader_factory = can.BLFReader
    if writer_factory is None:
        writer_factory = can.BLFWriter

    started = time.perf_counter()
    bus_filter_set = None if bus_filter is None else {int(bus) for bus in bus_filter}
    written = 0
    scanned = 0
    bus_counts: dict[int, int] = {}
    t0 = None
    last_timestamp = None
    saw_inversion = False
    early_stop_after = window.stop + EARLY_STOP_MARGIN_SECONDS
    raw_channel_of = message_raw_channel
    raw_to_bus = getattr(mapping, "raw_to_bus", None) if mapping is not None else None
    start, stop = window.start, window.stop
    _check_cancel(cancel_event)

    writer = None
    reader = None
    try:
        reader = reader_factory(str(source_path))
        writer = writer_factory(str(output_path))
        write_message = getattr(writer, "write", None)
        if not callable(write_message):
            write_message = writer.on_message_received
        for message in reader:
            scanned += 1
            timestamp = message.timestamp
            if t0 is None:
                t0 = timestamp
            if last_timestamp is not None and timestamp < last_timestamp:
                saw_inversion = True
            last_timestamp = timestamp
            if scanned % CHECK_CANCEL_INTERVAL == 0:
                _check_cancel(cancel_event)
            if progress_callback is not None and scanned % progress_interval == 0:
                progress_callback(scanned, written)
            relative = timestamp - t0
            if relative > early_stop_after and not saw_inversion:
                break
            raw_channel = raw_channel_of(message)
            bus_id = raw_to_bus.get(raw_channel, raw_channel) if raw_to_bus is not None else raw_channel
            if bus_filter_set is not None and bus_id not in bus_filter_set:
                continue
            if relative < start or relative > stop:
                continue
            write_message(message)
            written += 1
            bus_counts[bus_id] = bus_counts.get(bus_id, 0) + 1
    finally:
        if reader is not None:
            try:
                reader.stop()
            except Exception:
                pass
        if writer is not None:
            try:
                writer.stop()
            except Exception:
                pass

    if written == 0:
        raise ValueError(f"指定时间范围内没有找到任何报文 (总报文数: {scanned})")

    if progress_callback is not None:
        progress_callback(scanned, written)
    return CutoutResult(
        output_path=str(output_path),
        message_count=written,
        bus_counts=tuple(sorted(bus_counts.items())),
        elapsed=time.perf_counter() - started,
    )


def cut_signal_mdf(
    series: Sequence[SignalSeries],
    output_path: str | os.PathLike,
    window: CutoutWindow,
    *,
    cancel_event=None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> CutoutResult:
    """Write selected in-memory signals as MDF4 channels inside the window."""
    started = time.perf_counter()
    _check_cancel(cancel_event)
    mdf = asammdf.MDF(version="4.10")
    channel_count = 0
    sample_count = 0
    skipped = []
    try:
        for index, item in enumerate(series, start=1):
            _check_cancel(cancel_event)
            timestamps = np.asarray(item.timestamps)
            samples = np.asarray(item.samples)
            if len(timestamps) == 0 or len(samples) != len(timestamps):
                skipped.append(f"{item.name}: 数据为空或长度不一致")
                continue
            mask = window.mask(timestamps)
            if not bool(mask.any()):
                skipped.append(f"{item.name}: 该时间段内没有采样点")
                continue
            mdf.append(
                Signal(
                    samples=samples[mask],
                    timestamps=timestamps[mask],
                    name=item.name,
                    unit=item.unit or "",
                ),
                comment=item.comment or "",
            )
            channel_count += 1
            sample_count += int(mask.sum())
            if progress_callback is not None:
                progress_callback(index, len(series))
        if channel_count == 0:
            raise ValueError("选定时间段内没有可导出的信号数据。")
        _check_cancel(cancel_event)
        mdf.save(str(output_path), overwrite=True)
    finally:
        try:
            mdf.close()
        except Exception:
            pass

    return CutoutResult(
        output_path=str(output_path),
        channel_count=channel_count,
        sample_count=sample_count,
        skipped=tuple(skipped),
        elapsed=time.perf_counter() - started,
    )


def cut_mdf_file(
    source_path: str | os.PathLike,
    output_path: str | os.PathLike,
    window: CutoutWindow,
    *,
    channels: Sequence[tuple] | None = None,
    cancel_event=None,
    progress_callback: Callable[[str], None] | None = None,
) -> CutoutResult:
    """Cut an existing MDF/MF4 file, optionally keeping only selected channels."""
    started = time.perf_counter()
    _check_cancel(cancel_event)
    if progress_callback is not None:
        progress_callback("正在读取源文件…")
    source = asammdf.MDF(str(source_path))
    filtered = None
    channel_count = 0
    try:
        base = source
        if channels is not None:
            if progress_callback is not None:
                progress_callback("正在筛选已选通道…")
            filtered = source.filter(list(channels))
            base = filtered
            if mdf_signal_count(filtered) == 0:
                raise ValueError("已选信号不属于该文件，没有可导出的通道。")
        _check_cancel(cancel_event)
        if progress_callback is not None:
            progress_callback("正在裁剪时间范围…")
        cut = base.cut(start=window.start, stop=window.stop, time_from_zero=False)
        try:
            channel_count = mdf_signal_count(cut)
            _check_cancel(cancel_event)
            if progress_callback is not None:
                progress_callback("正在写入 MDF4 文件…")
            cut.save(str(output_path), overwrite=True)
            _check_cancel(cancel_event)
        finally:
            try:
                cut.close()
            except Exception:
                pass
    finally:
        if filtered is not None:
            try:
                filtered.close()
            except Exception:
                pass
        try:
            source.close()
        except Exception:
            pass

    return CutoutResult(
        output_path=str(output_path),
        channel_count=channel_count,
        elapsed=time.perf_counter() - started,
    )


def cut_csv_frame(
    source_frame: pd.DataFrame,
    output_path: str | os.PathLike,
    window: CutoutWindow,
    *,
    columns: Sequence[str] | None = None,
    calculated: Sequence[SignalSeries] = (),
    cancel_event=None,
    progress_callback: Callable[[str], None] | None = None,
) -> CutoutResult:
    """Slice a CSV/VBO frame by timestamp and write it back as CSV."""
    started = time.perf_counter()
    _check_cancel(cancel_event)
    if source_frame is None or len(source_frame) == 0:
        raise ValueError("源文件中没有可导出的数据。")
    index = np.asarray(source_frame.index, dtype=float)
    mask = window.mask(index)
    if not bool(mask.any()):
        raise ValueError("选定时间段内没有可导出的数据行。")
    frame = source_frame.loc[mask]
    if columns is not None:
        missing = [name for name in columns if name not in frame.columns]
        if missing:
            raise ValueError(f"源文件中不存在这些列: {', '.join(missing)}")
        frame = frame.loc[:, list(columns)]
    frame = frame.copy()
    for item in calculated:
        _check_cancel(cancel_event)
        frame[item.name] = np.interp(
            np.asarray(frame.index, dtype=float),
            np.asarray(item.timestamps, dtype=float),
            np.asarray(item.samples, dtype=float),
        )
    if source_frame.index.name:
        frame.index.name = source_frame.index.name
    if progress_callback is not None:
        progress_callback("正在写入 CSV 文件…")
    _check_cancel(cancel_event)
    frame.to_csv(str(output_path), encoding="utf-8")
    return CutoutResult(
        output_path=str(output_path),
        row_count=len(frame),
        channel_count=len(frame.columns),
        elapsed=time.perf_counter() - started,
    )


@dataclass(frozen=True)
class CutoutJob:
    """Everything a cut-out run needs, captured on the GUI thread."""

    source_path: str
    kind: SourceKind
    window: CutoutWindow
    output_path: str
    format: OutputFormat
    scope: OutputScope
    mapping: object = None
    total_messages: int = 0
    series: tuple = ()
    channels: tuple | None = None
    frame: object = None
    columns: tuple | None = None
    calculated: tuple = ()
    notes: tuple = ()


ProgressCallback = Callable[[str, int, int], None]


def execute_cutout_job(
    job: CutoutJob,
    cancel_event=None,
    progress_callback: ProgressCallback | None = None,
) -> CutoutResult:
    """Run one cut-out job and move the output into place atomically.

    The progress callback receives ``(text, value, total)``; ``total <= 1``
    means the caller should show an indeterminate progress bar.
    """
    started = time.perf_counter()

    def report(text: str, value: int = 0, total: int = 0) -> None:
        if progress_callback is not None:
            progress_callback(text, int(value), int(total))

    report("正在准备截取…")
    _check_cancel(cancel_event)

    with atomic_output(job.output_path) as temporary_path:
        if job.format is OutputFormat.BLF:
            total = max(0, int(job.total_messages or 0))

            def blf_progress(scanned: int, written: int) -> None:
                report(
                    f"正在读取报文… 已扫描 {scanned:,} 条，已写入 {written:,} 条",
                    scanned,
                    total,
                )

            result = cut_blf_raw(
                job.source_path,
                temporary_path,
                job.window,
                mapping=job.mapping,
                cancel_event=cancel_event,
                progress_callback=blf_progress,
            )
        elif job.format is OutputFormat.MDF and job.kind is SourceKind.MDF:
            result = cut_mdf_file(
                job.source_path,
                temporary_path,
                job.window,
                channels=job.channels,
                cancel_event=cancel_event,
                progress_callback=lambda text: report(text),
            )
        elif job.format is OutputFormat.MDF:
            def signal_progress(index: int, count: int) -> None:
                report(f"正在写入信号 {index}/{count}…", index, count)

            result = cut_signal_mdf(
                job.series,
                temporary_path,
                job.window,
                cancel_event=cancel_event,
                progress_callback=signal_progress,
            )
        else:
            result = cut_csv_frame(
                job.frame,
                temporary_path,
                job.window,
                columns=job.columns,
                calculated=job.calculated,
                cancel_event=cancel_event,
                progress_callback=lambda text: report(text),
            )

    report("截取完成", 100, 100)
    return replace(
        result,
        output_path=str(job.output_path),
        skipped=tuple(result.skipped) + tuple(job.notes),
        elapsed=time.perf_counter() - started,
    )


__all__ = [
    "ALL_EXTENSIONS",
    "BLF_EXTENSIONS",
    "CAN_LOG_EXTENSIONS",
    "CSV_EXTENSIONS",
    "EARLY_STOP_MARGIN_SECONDS",
    "MDF_EXTENSIONS",
    "CutoutCancelled",
    "CutoutContext",
    "CutoutJob",
    "CutoutOption",
    "CutoutResult",
    "CutoutWindow",
    "FORMAT_EXTENSIONS",
    "FORMAT_LABELS",
    "OutputFormat",
    "OutputPathIssue",
    "OutputScope",
    "SignalSeries",
    "SourceKind",
    "atomic_output",
    "build_options",
    "clean_signal_name",
    "cut_blf_raw",
    "cut_csv_frame",
    "cut_mdf_file",
    "cut_signal_mdf",
    "default_option",
    "default_output_name",
    "execute_cutout_job",
    "mdf_signal_count",
    "option_by_key",
    "sanitize_file_name",
    "source_kind",
    "validate_output_path",
]
