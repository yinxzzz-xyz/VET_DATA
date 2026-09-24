"""Background loading workers and progress UI.

The worker deliberately performs data I/O only. Widget creation and state
mutation remain in the GUI thread.
"""

from dataclasses import dataclass
from io import StringIO
from pathlib import Path

import asammdf
import numpy as np
import pandas as pd
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QDialog, QLabel, QProgressBar, QVBoxLayout


@dataclass
class LoadResult:
    path: str
    data: object
    signals: dict


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


class BusyLoadDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent); self.setWindowTitle("加载数据文件"); self.setModal(True); self.setMinimumWidth(380)
        layout = QVBoxLayout(self); self.label = QLabel("正在读取文件…"); layout.addWidget(self.label)
        bar = QProgressBar(); bar.setRange(0, 0); layout.addWidget(bar)
    def set_status(self, text): self.label.setText(text)
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
