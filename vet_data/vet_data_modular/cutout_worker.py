"""Background thread for data cut-out jobs.

The GUI captures a :class:`CutoutJob` snapshot and this worker performs all
file I/O.  It never touches widgets, so the main window stays responsive.
"""

from __future__ import annotations

from threading import Event

from PyQt6.QtCore import QThread, pyqtSignal

from .data_cutout import CutoutCancelled, execute_cutout_job


class CutoutWorker(QThread):
    progress = pyqtSignal(str, int, int)
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, job, parent=None):
        super().__init__(parent)
        self.job = job
        self.cancel_event = Event()

    def cancel(self):
        self.cancel_event.set()
        self.requestInterruption()

    def run(self):
        try:
            result = execute_cutout_job(self.job, self.cancel_event, self._report_progress)
            if self.cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.completed.emit(result)
        except CutoutCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))

    def _report_progress(self, text, value, total):
        self.progress.emit(str(text), int(value), int(total))


__all__ = ["CutoutWorker"]
