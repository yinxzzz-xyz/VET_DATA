"""Merged VET_DATA main window."""

from pathlib import Path

from PyQt6.QtWidgets import QFileDialog, QLabel, QMessageBox, QPushButton

from .blf_slice_dialog import BlfSliceDialog
from .blf_slice_progress import (
    BlfSliceProgressDialog, BlfSliceResultDialog, BlfSliceWorker,
    request_duplicate_selection,
)
from .collapsible import CollapsiblePanelsMixin
from .legacy import baseline
from .math_channel import SearchableMathChannelDialog  # installs baseline override
from .plot_panel import PlotPanelMixin
from .signal_panel import SignalPanelMixin
from .workers import BusyLoadDialog, DataLoadWorker


class MDFPlotter(SignalPanelMixin, PlotPanelMixin, CollapsiblePanelsMixin, baseline.MDFPlotter):
    """26.09.01 baseline plus the v10 interaction enhancements."""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VET_DATA merged modular")
        self._data_load_worker = None; self._data_load_dialog = None
        self._blf_slice_dialog = None; self._blf_slice_worker = None; self._blf_slice_progress = None
        self._blf_slice_result = None
        self._close_after_load = False; self._close_after_blf_slice = False
        self._setup_signal_panel(); self._setup_plot_panel(); self._setup_collapsible_panels()
        self._setup_blf_slice_entry()

    def _setup_blf_slice_entry(self):
        self.blf_slice_button = QPushButton("BLF 工况切片")
        self.blf_slice_button.setToolTip("独立选择 BLF 和工况表并执行自动切片")
        self.blf_slice_button.clicked.connect(self.open_blf_slice_dialog)
        layout = self._find_layout_containing(self.layout(), self.save_data_button)
        if layout is None:
            raise RuntimeError("无法定位主窗口底部操作区")
        layout.addWidget(self.blf_slice_button)

    @classmethod
    def _find_layout_containing(cls, layout, widget):
        if layout is None:
            return None
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item.widget() is widget:
                return layout
            nested = item.layout()
            found = cls._find_layout_containing(nested, widget) if nested else None
            if found is not None:
                return found
            child = item.widget()
            found = cls._find_layout_containing(child.layout(), widget) if child else None
            if found is not None:
                return found
        return None

    def open_blf_slice_dialog(self):
        if self._blf_slice_worker is not None and self._blf_slice_worker.isRunning():
            return
        if self._blf_slice_dialog is not None and self._blf_slice_dialog.isVisible():
            self._blf_slice_dialog.raise_()
            self._blf_slice_dialog.activateWindow()
            return
        self.blf_slice_button.setEnabled(False)
        dialog = BlfSliceDialog(self)
        self._blf_slice_dialog = dialog
        dialog.task_confirmed.connect(self._start_blf_slice)
        dialog.finished.connect(self._blf_slice_input_closed)
        dialog.show()

    def _blf_slice_input_closed(self):
        self._blf_slice_dialog = None
        if self._blf_slice_worker is None:
            self.blf_slice_button.setEnabled(True)

    def _start_blf_slice(self, task, table_result):
        worker = BlfSliceWorker(task, table_result, self)
        progress = BlfSliceProgressDialog(worker, self)
        self._blf_slice_worker, self._blf_slice_progress = worker, progress
        worker.duplicate_found.connect(lambda result: request_duplicate_selection(worker, result, self))
        worker.completed.connect(self._finish_blf_slice)
        worker.cancelled.connect(self._finish_blf_slice)
        worker.failed.connect(self._fail_blf_slice)
        worker.finished.connect(self._cleanup_blf_slice)
        progress.show()
        worker.start()

    def _finish_blf_slice(self, result):
        if self._blf_slice_progress is not None:
            self._blf_slice_progress.hide()
        self._show_blf_slice_result(result)

    def _fail_blf_slice(self, _message, result):
        if self._blf_slice_progress is not None:
            self._blf_slice_progress.hide()
        self._show_blf_slice_result(result)

    def _show_blf_slice_result(self, result):
        if self._blf_slice_result is not None:
            self._blf_slice_result.close()
            self._blf_slice_result.deleteLater()
        dialog = BlfSliceResultDialog(result, self)
        self._blf_slice_result = dialog
        dialog.finished.connect(self._clear_blf_slice_result)
        dialog.show()

    def _clear_blf_slice_result(self):
        self._blf_slice_result = None

    def _cleanup_blf_slice(self):
        worker = self._blf_slice_worker
        self._blf_slice_worker = None
        if self._blf_slice_progress is not None:
            self._blf_slice_progress.deleteLater()
            self._blf_slice_progress = None
        self.blf_slice_button.setEnabled(True)
        if worker is not None:
            worker.deleteLater()
        if self._close_after_blf_slice:
            self._close_after_blf_slice = False
            self.close()

    def load_file_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择数据文件", "", "支持的数据文件 (*.mdf *.mf4 *.blf *.asc *.trc *.csv *.vbo);;所有文件 (*)")
        if not path: return
        suffix = Path(path).suffix.lower()
        if suffix in {".blf", ".asc", ".trc"}:
            # CAN parsing already has the baseline's determinate ParseWorker dialog.
            self.mdf_path = path; self.current_file_label.setText(f"当前文件: {Path(path).name}")
            self.load_can_bus_info(path); self.create_bus_config_ui()
            return
        self.set_buttons_enabled(False)
        self._data_load_dialog = BusyLoadDialog(self); self._data_load_dialog.show()
        worker = DataLoadWorker(path, self); self._data_load_worker = worker
        worker.status.connect(self._data_load_dialog.set_status)
        worker.result.connect(self._finish_data_load)
        worker.error.connect(self._fail_data_load)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _finish_data_load(self, result):
        old = getattr(self, "mdf_file", None)
        if old is not None and hasattr(old, "close"):
            try: old.close()
            except Exception: pass
        self.mdf_path = result.path; self.mdf_file = result.data
        self.current_file_label.setText(f"当前文件: {Path(result.path).name}")
        self.signals.clear(); self.signals.update(result.signals)
        self.can_parsed_data.clear(); self.bus_signal_map.clear(); self.custom_math_data.clear()
        self.can_bus_data.clear(); self.db_per_bus.clear(); self.bus_protocols.clear(); self.can_bus_signals.clear()
        self.clear_bus_config(); self._sig_value_cache.clear(); self._user_signal_order.clear()
        self.refresh_signal_list_ui(); self.load_config()
        self._close_load_dialog(); self._data_load_worker = None

    def _fail_data_load(self, message):
        self._close_load_dialog(); self._data_load_worker = None
        self.set_buttons_enabled(False)
        QMessageBox.critical(self, "加载文件失败", message)

    def _close_load_dialog(self):
        if self._data_load_dialog is not None:
            self._data_load_dialog.close(); self._data_load_dialog.deleteLater(); self._data_load_dialog = None

    def closeEvent(self, event):
        if self._blf_slice_worker is not None and self._blf_slice_worker.isRunning():
            self._blf_slice_worker.cancel()
            self._close_after_blf_slice = True
            if self._blf_slice_progress is not None:
                self._blf_slice_progress.phase_label.setText("正在安全停止 BLF 切片…")
            event.ignore()
            return
        if self._data_load_worker is not None and self._data_load_worker.isRunning():
            # MDF constructors cannot be interrupted safely.  Keep the window
            # alive until the worker exits instead of destroying a live QThread.
            self._data_load_worker.cancel()
            if not self._close_after_load:
                self._close_after_load = True
                self._data_load_worker.finished.connect(self.close)
            if self._data_load_dialog is not None:
                self._data_load_dialog.set_status("正在安全停止文件读取…")
            event.ignore()
            return
        self._close_load_dialog()
        super().closeEvent(event)
