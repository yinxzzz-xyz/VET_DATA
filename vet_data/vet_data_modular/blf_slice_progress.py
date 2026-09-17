"""Background BLF slicing orchestration and progress UI for stage 5A."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Condition, Event

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QDialog, QLabel, QMessageBox, QProgressBar, QPushButton, QVBoxLayout

from .blf_slice_dialog import DuplicateSelectionDialog, retain_duplicate_choices
from .blf_slice_execution import build_slice_execution_plan, execute_slice
from .blf_slice_models import TaskResult, TaskStatus
from .blf_slice_report import write_report
from .blf_slice_service import (
    build_blf_header_index, build_blf_source_chains, detect_duplicate_blf_files,
    select_blf_candidates, validate_task,
)
from .blf_slice_time import build_condition_time_window


LOGGER = logging.getLogger(__name__)


class BlfSliceWorker(QThread):
    """Run the existing stage 1-4 services without touching widgets."""

    phase_changed = pyqtSignal(str, str)
    progress_changed = pyqtSignal(int, int, str)
    log_emitted = pyqtSignal(str, str, object)
    duplicate_found = pyqtSignal(object)
    completed = pyqtSignal(object)
    cancelled = pyqtSignal(object)
    failed = pyqtSignal(str, object)

    def __init__(self, task, table_result, parent=None):
        super().__init__(parent)
        self.task, self.table_result = task, table_result
        self.cancel_event = Event()
        self._duplicate_wait = Condition()
        self._duplicate_reply_received = False
        self._duplicate_choices = None

    def cancel(self):
        self.cancel_event.set()
        with self._duplicate_wait:
            self._duplicate_wait.notify_all()

    def submit_duplicate_choices(self, choices):
        with self._duplicate_wait:
            self._duplicate_reply_received = True
            self._duplicate_choices = None if choices is None else dict(choices)
            if choices is None:
                self.cancel_event.set()
            self._duplicate_wait.notify_all()

    def run(self):
        partial = None
        try:
            self.phase_changed.emit("validation", "正在检查输入…")
            validation = validate_task(self.task)
            if not validation.is_valid:
                raise ValueError("；".join(issue.message for issue in validation.issues))
            files = validation.blf_files
            self.phase_changed.emit("duplicates", "正在检查重复文件…")
            duplicates = detect_duplicate_blf_files(files, cancel_event=self.cancel_event, progress_callback=self._progress)
            if duplicates.cancelled or self.cancel_event.is_set():
                self._emit_cancelled(partial)
                return
            if duplicates.groups:
                self.duplicate_found.emit(duplicates)
                with self._duplicate_wait:
                    while not self._duplicate_reply_received and not self.cancel_event.is_set():
                        self._duplicate_wait.wait()
                if self.cancel_event.is_set():
                    self._emit_cancelled(partial)
                    return
                files = retain_duplicate_choices(files, duplicates.groups, self._duplicate_choices)
            self.phase_changed.emit("header_index", "正在索引 BLF Header…")
            index_started = datetime.now()
            entries = build_blf_header_index(files, self.task.blf_utc_offset)
            selected = tuple(item for item in self.task.conditions if item.selected)
            windows = tuple(build_condition_time_window(item, self.task.table_utc_offset) for item in selected)
            self.phase_changed.emit("range_confirmation", "正在确认 BLF 实际时间范围…")
            selection = select_blf_candidates(
                entries, windows, self.task.blf_utc_offset,
                cancel_event=self.cancel_event, progress_callback=self._progress,
            )
            if self.cancel_event.is_set():
                self._emit_cancelled(partial)
                return
            self.phase_changed.emit("source_chains", "正在匹配来源链…")
            chains = build_blf_source_chains(selection.entries)
            plan = build_slice_execution_plan(selection.matches, chains, selected)
            self.phase_changed.emit("slice", "正在切片并安全写入…")
            result = execute_slice(
                self.task, plan, selection.entries,
                cancel_event=self.cancel_event, progress_callback=self._progress,
            )
            retained = set(files)
            summary = [f"检测到完全重复组：{len(duplicates.groups)}"]
            for index, group in enumerate(duplicates.groups, 1):
                summary.append(f"重复组 {index} 保留：{next(path for path in group.paths if path in retained)}")
            result = replace(
                result,
                index_duration_seconds=(datetime.now() - index_started).total_seconds(),
                table_valid_rows=len(self.table_result.conditions),
                table_discarded_rows=len(self.table_result.discarded_rows),
                table_discard_reasons=tuple(f"{row.source_location}: {row.reason}" for row in self.table_result.discarded_rows),
                duplicate_summary=tuple(summary), retained_input_files=files,
            )
            partial = result
            self.phase_changed.emit("report", "正在生成报告…")
            result = replace(result, report_path=write_report(result, self.task.output_dir))
            (self.cancelled if result.status is TaskStatus.CANCELLED else self.completed).emit(result)
        except Exception as exc:
            LOGGER.exception("BLF slicing worker failed")
            self.failed.emit(f"{type(exc).__name__}: {exc}", partial)

    def _progress(self, progress):
        self.progress_changed.emit(progress.file_index, progress.file_count, str(progress.path))

    def _emit_cancelled(self, result):
        if result is None:
            result = TaskResult(
                task=self.task, status=TaskStatus.CANCELLED, finished_at=datetime.now(),
                table_valid_rows=len(self.table_result.conditions),
                table_discarded_rows=len(self.table_result.discarded_rows),
            )
        self.cancelled.emit(result)


class BlfSliceProgressDialog(QDialog):
    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self.worker = worker
        self.setWindowTitle("BLF 工况切片进度")
        self.setModal(False)
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        self.phase_label, self.file_label = QLabel("准备开始…"), QLabel("")
        self.progress, self.cancel_button = QProgressBar(), QPushButton("取消")
        self.progress.setRange(0, 0)
        for widget in (self.phase_label, self.file_label, self.progress, self.cancel_button):
            layout.addWidget(widget)
        self.cancel_button.clicked.connect(self.request_cancel)
        worker.phase_changed.connect(lambda _phase, text: self.phase_label.setText(text))
        worker.progress_changed.connect(self.set_progress)

    def set_progress(self, value, total, path):
        self.file_label.setText(path)
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(value)
        else:
            self.progress.setRange(0, 0)

    def request_cancel(self):
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("正在取消…")
        self.phase_label.setText("正在取消，等待当前安全点…")
        self.worker.cancel()

    def closeEvent(self, event):
        if self.worker.isRunning():
            self.request_cancel()
            event.ignore()
        else:
            super().closeEvent(event)


def request_duplicate_selection(worker, result, parent):
    dialog = DuplicateSelectionDialog(result.groups, parent)
    worker.submit_duplicate_choices(dialog.choices() if dialog.exec() == QDialog.DialogCode.Accepted else None)


def show_task_result(result, parent):
    outputs = sum(len(item.output_files) for item in result.condition_results)
    QMessageBox.information(parent, "BLF 工况切片完成", f"任务状态：{result.status.value}\n输出 BLF：{outputs}\n报告：{result.report_path or '未生成'}")
