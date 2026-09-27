"""Background loading workers and progress UI.

The worker deliberately performs data I/O only. Widget creation and state
mutation remain in the GUI thread.
"""

from collections import defaultdict
from dataclasses import dataclass
from io import StringIO
from numbers import Integral
import os
from pathlib import Path
from threading import Event
from types import MappingProxyType

import asammdf
import can
import numpy as np
import pandas as pd
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QDialog, QLabel, QProgressBar, QPushButton, QVBoxLayout


@dataclass
class LoadResult:
    path: str
    data: object
    signals: dict


class CanBusDetectionCancelled(Exception):
    """Raised when a cooperative CAN log scan cancellation is requested."""


class CsvExportCancelled(Exception):
    """Raised when a CSV export is cancelled between non-interruptible steps."""


@dataclass(frozen=True)
class CsvExportSignal:
    """Stable export metadata with a read-only reference to existing signal data."""

    display_name: str
    signal: object


@dataclass(frozen=True)
class CsvExportSnapshot:
    output_path: str
    target_frequency: float
    signals: tuple


def export_csv_snapshot(snapshot, cancel_event=None, progress_callback=None):
    """Run the legacy CSV algorithm without accessing GUI state or widgets."""
    cancel_event = cancel_event or Event()

    def check_cancelled():
        if cancel_event.is_set():
            raise CsvExportCancelled()

    check_cancelled()
    bounds = []
    for item in snapshot.signals:
        timestamps = item.signal.timestamps
        if len(timestamps) > 0:
            # CSV input is not required to be sorted.  Per-signal scalar
            # extrema preserve the old global min/max semantics without the
            # large temporary Python list of every timestamp.
            bounds.append((np.min(timestamps), np.max(timestamps)))
        check_cancelled()
    if not bounds:
        raise ValueError("无法获取信号数据。")

    t_start = min(bound[0] for bound in bounds)
    t_end = max(bound[1] for bound in bounds)
    step = 1 / snapshot.target_frequency
    raster = np.arange(t_start, t_end + step, step)
    if progress_callback is not None:
        progress_callback(10, "正在创建重采样时间轴…")

    data = {}
    signal_count = len(snapshot.signals)
    for index, item in enumerate(snapshot.signals, start=1):
        check_cancelled()
        try:
            signal = item.signal
            if hasattr(signal, "resample"):
                data[item.display_name] = signal.resample(raster).samples
            else:
                df_signal = pd.DataFrame({
                    "timestamp": signal.timestamps,
                    "samples": signal.samples,
                }).set_index("timestamp")
                df_raster = pd.DataFrame({"timestamp": raster}).set_index("timestamp")
                df_aligned = pd.merge_asof(
                    df_raster, df_signal, on="timestamp", direction="nearest"
                )
                data[item.display_name] = df_aligned["samples"].values
        except Exception as exc:
            print(f"导出信号 '{item.display_name}' 出错: {exc}")
        if progress_callback is not None:
            progress_callback(
                10 + int(index * 70 / max(signal_count, 1)),
                f"正在重采样信号… {index}/{signal_count}",
            )

    if not data:
        raise ValueError("没有可导出的信号数据。")
    check_cancelled()
    frame = pd.DataFrame(data, index=raster)
    frame.index.name = "timestamp"
    if progress_callback is not None:
        progress_callback(85, "正在写入 CSV 文件…")
    check_cancelled()
    frame.to_csv(snapshot.output_path, encoding="utf-8")
    if progress_callback is not None:
        progress_callback(100, "CSV 导出完成")
    return snapshot.output_path


MISSING_CAN_CHANNEL = 1


def normalize_can_channel(channel):
    """Return a stable non-negative raw channel for log-reader messages.

    python-can permits channel to be an int, string, or None.  File readers in
    this application normally provide integers.  Missing, None, boolean,
    negative, and non-numeric values retain the detector's historical fallback
    to raw channel 1; numeric strings are accepted explicitly.
    """
    if isinstance(channel, bool):
        return MISSING_CAN_CHANNEL
    if isinstance(channel, Integral):
        value = int(channel)
    elif isinstance(channel, str):
        try:
            value = int(channel.strip())
        except (TypeError, ValueError):
            return MISSING_CAN_CHANNEL
    else:
        return MISSING_CAN_CHANNEL
    return value if value >= 0 else MISSING_CAN_CHANNEL


def message_raw_channel(message):
    return normalize_can_channel(getattr(message, "channel", None))


@dataclass(frozen=True)
class CanBusMapping:
    raw_to_bus: object
    bus_to_raw: object
    channel_offset: int

    @classmethod
    def from_raw_channels(cls, raw_channels):
        channels = tuple(sorted(set(raw_channels)))
        offset = 1 if 0 in channels else 0
        raw_to_bus = {raw: raw + offset for raw in channels}
        bus_to_raw = {bus: raw for raw, bus in raw_to_bus.items()}
        if len(bus_to_raw) != len(raw_to_bus):
            raise ValueError("CAN raw channel 到 GUI Bus 的映射不唯一")
        return cls(
            MappingProxyType(raw_to_bus),
            MappingProxyType(bus_to_raw),
            offset,
        )


def normalized_can_path(path):
    return os.path.normcase(str(Path(path).resolve()))


@dataclass(frozen=True)
class CanFileIdentity:
    path: str
    size: int
    mtime_ns: int


def capture_can_file_identity(path):
    resolved = normalized_can_path(path)
    stat = Path(resolved).stat()
    return CanFileIdentity(resolved, stat.st_size, stat.st_mtime_ns)


@dataclass(frozen=True)
class CanBusCountSnapshot:
    bus_id: int
    original_bus_id: int
    msg_count: int


@dataclass(frozen=True)
class CanDetectionSnapshot:
    file_identity: CanFileIdentity
    mapping: CanBusMapping
    buses: object


class CanBusData(dict):
    def __init__(self, snapshot):
        super().__init__()
        self.snapshot = snapshot
        self.mapping = snapshot.mapping


def detect_can_bus_info(log_path, cancel_event=None, progress_callback=None):
    """Scan a CAN log without touching Qt widgets, preserving legacy semantics."""
    cancel_event = cancel_event or Event()
    reader = None
    try:
        identity_before = capture_can_file_identity(log_path)
        reader = can.BLFReader(log_path) if str(log_path).lower().endswith(".blf") else can.LogReader(log_path)
        bus_stats = {}
        total_count = 0
        sample_interval = 100
        bus_samples = defaultdict(lambda: {"timestamps": [], "ids": set(), "data": [], "is_fd": [], "dlc": []})
        t0 = None
        bus_id_sets = defaultdict(set)
        raw_channels = set()
        for msg in reader:
            if cancel_event.is_set():
                raise CanBusDetectionCancelled()
            if t0 is None:
                t0 = msg.timestamp
            total_count += 1
            raw_channel = message_raw_channel(msg)
            raw_channels.add(raw_channel)
            bus_id = raw_channel
            if bus_id == 0:
                bus_id = 0
            rel_t = msg.timestamp - t0 if t0 else 0
            is_fd = getattr(msg, "is_fd", False) or getattr(msg, "is_can_fd", False)
            if bus_id not in bus_stats:
                bus_stats[bus_id] = {"count": 0, "first_timestamp": msg.timestamp, "is_fd": is_fd, "raw_channel": raw_channel}
            bus_stats[bus_id]["count"] += 1
            if is_fd:
                bus_stats[bus_id]["is_fd"] = True
            bus_id_sets[bus_id].add(msg.arbitration_id)
            bus_samples[bus_id]["ids"].add(msg.arbitration_id)
            if bus_stats[bus_id]["count"] % sample_interval == 1 or bus_stats[bus_id]["count"] <= 10:
                bus_samples[bus_id]["timestamps"].append(rel_t)
                if hasattr(msg, "data"):
                    bus_samples[bus_id]["data"].append(msg.data)
                bus_samples[bus_id]["is_fd"].append(is_fd)
                bus_samples[bus_id]["dlc"].append(len(msg.data) if hasattr(msg, "data") else 0)
            if progress_callback is not None and total_count % 10000 == 0:
                progress_callback(total_count)
        if cancel_event.is_set():
            raise CanBusDetectionCancelled()
        if total_count == 0:
            raise Exception("未读取到任何CAN报文")
        mapping = CanBusMapping.from_raw_channels(raw_channels)
        channel_offset = mapping.channel_offset
        has_channel_zero = channel_offset == 1
        print(f"\n{'=' * 60}\n📊 通道检测结果:")
        print(f"  原始通道号: {sorted(raw_channels)}")
        print(f"  是否包含通道0: {has_channel_zero}")
        print(f"  通道偏移量: {channel_offset}")
        if has_channel_zero:
            print("  ⚠️ 检测到通道0，所有通道将+1映射")
        print(f"{'=' * 60}\n")
        bus_counts = {
            mapping.raw_to_bus[raw]: CanBusCountSnapshot(
                mapping.raw_to_bus[raw], raw, stats.get("count", 0)
            )
            for raw, stats in bus_stats.items()
        }
        identity_after = capture_can_file_identity(log_path)
        if identity_after != identity_before:
            raise RuntimeError("CAN日志在总线探测期间发生变化，请重新探测")
        snapshot = CanDetectionSnapshot(
            identity_after,
            mapping,
            MappingProxyType(bus_counts),
        )
        result = CanBusData(snapshot)
        for raw_bus_id, samples in bus_samples.items():
            bus_id = mapping.raw_to_bus[raw_bus_id]
            actual_id_count = len(bus_id_sets.get(raw_bus_id, set()))
            stats = bus_stats.get(raw_bus_id, {})
            is_fd = stats.get("is_fd", False)
            actual_ids_list = list(bus_id_sets.get(raw_bus_id, set()))
            print(f"📊 总线 {bus_id} (原始通道: {raw_bus_id})")
            print(f"   - 实际ID数量: {len(actual_ids_list)}")
            print(f"   - 前10个ID: {actual_ids_list[:10]}")
            result[bus_id] = {
                "timestamps": samples["timestamps"], "ids": list(samples["ids"]),
                "actual_ids": actual_ids_list, "data": samples["data"],
                "is_fd": samples["is_fd"], "dlc": samples["dlc"],
                "msg_count": stats.get("count", 0), "id_count": actual_id_count,
                "name": f"Bus {bus_id}", "has_fd": is_fd,
                "raw_channel": stats.get("raw_channel", raw_bus_id),
                "original_bus_id": raw_bus_id,
            }
        return result
    finally:
        if reader is not None:
            try:
                reader.stop()
            except Exception:
                pass


def _read_vbo(path):
    lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    lower = [line.strip().lower() for line in lines]
    try:
        names_at = lower.index("[column names]") + 1
        data_at = lower.index("[data]") + 1
    except ValueError as exc:
        raise ValueError("无法找到 [column names] 或 [data] 区。") from exc
    seen = {}; names = []
    for name in lines[names_at].split():
        number = seen.get(name, -1) + 1; seen[name] = number
        names.append(name if number == 0 else f"{name}_dup{number}")
    rows = [line for line in lines[data_at:] if line.strip() and not line.strip().startswith("[")]
    if not rows: raise ValueError("数据区为空。")
    frame = pd.read_csv(StringIO("\n".join(rows)), sep=r"\s+", header=None, names=names, engine="python", on_bad_lines="skip")
    time_name = next((c for c in frame.columns if c.lower() == "time"), frame.columns[1])
    frame.rename(columns={time_name: "timestamp"}, inplace=True)
    frame.dropna(how="all", axis=1, inplace=True)
    frame["timestamp"] = pd.to_numeric(frame["timestamp"], errors="coerce")
    frame.dropna(subset=["timestamp"], inplace=True)
    raw = frame["timestamp"].to_numpy()
    if len(raw) and raw.max() >= 100:
        integer = np.floor(raw).astype(int); fraction = raw - integer
        raw = integer // 10000 * 3600 + integer % 10000 // 100 * 60 + integer % 100 + fraction
    if len(raw): frame["timestamp"] = raw - raw[0]
    frame.sort_values("timestamp", inplace=True)
    frame["timestamp"] += frame.groupby("timestamp").cumcount() * 1e-6
    return frame.set_index("timestamp")


def load_data_file(path):
    suffix = Path(path).suffix.lower()
    if suffix in {".mdf", ".mf4"}:
        data = asammdf.MDF(path)
        signals = {}
        for signal in data.iter_channels():
            name = signal.name
            comment = signal.comment or "无描述"
            if not isinstance(comment, str):
                comment = comment.decode("utf-8", errors="ignore") if isinstance(comment, bytes) else str(comment)
            key = f"{name}_G{signal.group_index}_C{signal.channel_index}"
            signals[key] = {"name": name, "display_name": f"{name} (Group: {signal.group_index}, Channel: {signal.channel_index})", "group": signal.group_index, "channel": signal.channel_index, "comment": comment, "bus_id": None}
    elif suffix == ".csv":
        try: frame = pd.read_csv(path, encoding="utf-8")
        except UnicodeDecodeError: frame = pd.read_csv(path, encoding="gbk")
        if "timestamp" not in frame.columns:
            first = frame.columns[0]
            if "signal" not in first.lower() and "value" not in first.lower(): frame.rename(columns={first: "timestamp"}, inplace=True)
            else: frame.insert(0, "timestamp", np.arange(len(frame)))
        data = frame.set_index("timestamp")
        signals = {str(c): {"name": str(c), "display_name": str(c), "group": -1, "channel": -1, "comment": "从CSV文件导入", "bus_id": None} for c in data.columns}
    elif suffix == ".vbo":
        data = _read_vbo(path)
        signals = {str(c): {"name": str(c), "display_name": str(c), "group": -1, "channel": -1, "comment": "VBO导入" + (" (重复列已重命名)" if "_dup" in str(c) else ""), "bus_id": None} for c in data.columns}
    else:
        raise ValueError(f"不支持的后台加载文件类型: {suffix}")
    return LoadResult(str(path), data, signals)


class CanBusDetectionWorker(QThread):
    status = pyqtSignal(str)
    progress = pyqtSignal(int)
    result = pyqtSignal(object)
    error = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        self.cancel_event = Event()

    def cancel(self):
        self.cancel_event.set()
        self.requestInterruption()

    def run(self):
        try:
            self.status.emit("正在扫描 CAN 日志总线…")
            result = detect_can_bus_info(self.path, self.cancel_event, self._report_progress)
            if self.cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.result.emit(result)
        except CanBusDetectionCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.error.emit(f"读取CAN总线信息失败: {exc}")

    def _report_progress(self, count):
        self.progress.emit(count)
        self.status.emit(f"正在扫描 CAN 日志总线… 已读取 {count:,} 条报文")


class DataLoadWorker(QThread):
    status = pyqtSignal(str); result = pyqtSignal(object); error = pyqtSignal(str); cancelled = pyqtSignal()
    def __init__(self, path, parent=None):
        super().__init__(parent); self.path = path; self._cancelled = False
    def cancel(self): self._cancelled = True; self.requestInterruption()
    def run(self):
        try:
            self.status.emit("正在读取文件并建立信号索引…")
            result = load_data_file(self.path)
            if self._cancelled: self.cancelled.emit()
            else: self.result.emit(result)
        except Exception as exc: self.error.emit(str(exc))


class CsvExportWorker(QThread):
    progress = pyqtSignal(int, str)
    completed = pyqtSignal(str)
    error = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, snapshot, parent=None):
        super().__init__(parent)
        self.snapshot = snapshot
        self.cancel_event = Event()

    def cancel(self):
        self.cancel_event.set()
        self.requestInterruption()

    def run(self):
        try:
            path = export_csv_snapshot(
                self.snapshot, self.cancel_event, self.progress.emit
            )
            if self.cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.completed.emit(path)
        except CsvExportCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.error.emit(f"导出CSV时出错: {exc}")


class BusyLoadDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent); self.setWindowTitle("加载数据文件"); self.setModal(True); self.setMinimumWidth(380)
        layout = QVBoxLayout(self); self.label = QLabel("正在读取文件…"); layout.addWidget(self.label)
        bar = QProgressBar(); bar.setRange(0, 0); layout.addWidget(bar)
    def set_status(self, text): self.label.setText(text)


class CanBusDetectionDialog(BusyLoadDialog):
    cancel_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("探测 CAN 总线")
        self.label.setText("正在扫描 CAN 日志总线…")
        self.cancel_button = QPushButton("取消")
        self.layout().addWidget(self.cancel_button)
        self.cancel_button.clicked.connect(self.request_cancel)

    def request_cancel(self):
        if not self.cancel_button.isEnabled():
            return
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("正在取消…")
        self.label.setText("正在安全停止 CAN 总线探测…")
        self.cancel_requested.emit()

    def reject(self):
        self.request_cancel()
_ACTIVE_FORMULA_WORKERS = set()


class FormulaCalculationWorker(QThread):
    """Run pure formula parsing/resolution/alignment/calculation off the GUI thread."""

    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, definition, calculation):
        super().__init__()
        self.definition = definition
        self.calculation = calculation

    def run(self):
        try:
            self.completed.emit(self.calculation(self.definition))
        except Exception as exc:
            self.failed.emit(str(exc))

    def start_tracked(self):
        _ACTIVE_FORMULA_WORKERS.add(self)
        self.finished.connect(lambda: _ACTIVE_FORMULA_WORKERS.discard(self))
        self.start()
class FormulaRestoreWorker(QThread):
    """Run a complete dependency restore session without touching widgets."""

    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, config_data, resolver):
        super().__init__()
        self.config_data = config_data
        self.resolver = resolver

    def run(self):
        try:
            from .calculated_signal_config import CalculatedSignalRestoreService
            self.completed.emit(
                CalculatedSignalRestoreService().restore(self.config_data, self.resolver)
            )
        except Exception as exc:
            self.failed.emit(str(exc))

    def start_tracked(self):
        _ACTIVE_FORMULA_WORKERS.add(self)
        self.finished.connect(lambda: _ACTIVE_FORMULA_WORKERS.discard(self))
        self.start()
