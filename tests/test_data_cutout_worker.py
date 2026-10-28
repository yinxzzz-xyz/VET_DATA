"""Background cut-out execution: jobs, cancellation and GUI responsiveness."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import can
import numpy as np
import pandas as pd
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from vet_data_modular import baseline
from vet_data_modular.cutout_worker import CutoutWorker
from vet_data_modular.data_cutout import (
    CutoutCancelled,
    CutoutJob,
    CutoutOption,
    CutoutWindow,
    OutputFormat,
    OutputScope,
    SignalSeries,
    SourceKind,
    execute_cutout_job,
)
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


def wait_until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(0.005)
    APP.processEvents()
    return predicate()


def make_blf(path, count=150_000, base=1_700_000_000.0, step=0.001):
    writer = can.BLFWriter(str(path))
    try:
        for index in range(count):
            writer.on_message_received(can.Message(
                timestamp=base + index * step,
                arbitration_id=0x100 + index % 4,
                data=bytes([index % 251, 1, 2, 3]),
                channel=1 + index % 3,
                is_extended_id=False,
            ))
    finally:
        writer.stop()


def read_blf(path):
    reader = can.BLFReader(str(path))
    try:
        return list(reader)
    finally:
        reader.stop()


def csv_job(output, frame, window=CutoutWindow(1.0, 3.0), notes=()):
    return CutoutJob(
        source_path="D:/data/source.csv",
        kind=SourceKind.CSV,
        window=window,
        output_path=str(output),
        format=OutputFormat.CSV,
        scope=OutputScope.ALL,
        frame=frame,
        columns=None,
        calculated=(),
        notes=notes,
    )


class ExecuteJobTests(unittest.TestCase):
    def test_csv_job_writes_file_and_reports_progress(self):
        frame = pd.DataFrame({"a": [1.0, 2.0, 3.0, 4.0]}, index=[0.0, 1.0, 2.0, 3.0])
        frame.index.name = "timestamp"
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder, "cut.csv")
            result = execute_cutout_job(
                csv_job(output, frame, notes=("note: calc",)),
                progress_callback=lambda text, value, total: seen.append((text, value, total)),
            )
            self.assertTrue(output.exists())
            self.assertFalse(list(Path(folder).glob("*.part*")))
            frame_out = pd.read_csv(output)
        self.assertEqual(result.row_count, 3)
        self.assertEqual(result.skipped, ("note: calc",))
        self.assertTrue(seen)
        self.assertEqual(seen[0][0], "正在准备截取…")
        self.assertEqual(seen[-1][0], "截取完成")

    def test_cancel_before_job_removes_partial_output(self):
        frame = pd.DataFrame({"a": [1.0]}, index=[0.0])
        event = Event()
        event.set()
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder, "cut.csv")
            with self.assertRaises(CutoutCancelled):
                execute_cutout_job(csv_job(output, frame), cancel_event=event)
            self.assertFalse(output.exists())
            self.assertFalse(list(Path(folder).glob("*.part*")))

    def test_signal_mdf_job_reports_each_channel(self):
        series = (
            SignalSeries("a", "", "", np.array([0.0, 1.0, 2.0]), np.array([1.0, 2.0, 3.0])),
            SignalSeries("b", "", "", np.array([0.0, 1.0, 2.0]), np.array([4.0, 5.0, 6.0])),
        )
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            job = CutoutJob(
                source_path="D:/data/source.blf",
                kind=SourceKind.BLF,
                window=CutoutWindow(0.0, 1.0),
                output_path=str(Path(folder, "signals.mf4")),
                format=OutputFormat.MDF,
                scope=OutputScope.SELECTED,
                series=series,
                notes=("note: math",),
            )
            result = execute_cutout_job(
                job, progress_callback=lambda text, value, total: seen.append((value, total)),
            )
            self.assertTrue(Path(folder, "signals.mf4").exists())
        self.assertEqual(result.channel_count, 2)
        self.assertEqual(result.skipped, ("note: math",))
        self.assertIn((2, 2), seen)


class WorkerTests(unittest.TestCase):
    def test_worker_completes_and_emits_result(self):
        frame = pd.DataFrame({"a": [1.0, 2.0, 3.0]}, index=[0.0, 1.0, 2.0])
        frame.index.name = "timestamp"
        with tempfile.TemporaryDirectory() as folder:
            worker = CutoutWorker(csv_job(Path(folder, "cut.csv"), frame))
            results = []
            worker.completed.connect(results.append)
            worker.start()
            self.assertTrue(wait_until(lambda: not worker.isRunning()))
            worker.wait()
        self.assertTrue(results)
        self.assertEqual(results[0].row_count, 2)

    def test_worker_reports_cancelled_before_start(self):
        frame = pd.DataFrame({"a": [1.0]}, index=[0.0])
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder, "cut.csv")
            worker = CutoutWorker(csv_job(output, frame))
            worker.cancel_event.set()
            cancelled = []
            worker.cancelled.connect(lambda: cancelled.append(True))
            worker.start()
            self.assertTrue(wait_until(lambda: not worker.isRunning()))
            worker.wait()
            self.assertFalse(output.exists())
        self.assertEqual(cancelled, [True])

    def test_worker_keeps_gui_responsive_while_cutting_blf(self):
        base = 1_700_000_000.0
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, "source.blf")
            make_blf(source, count=150_000)
            last_message = read_blf(source)[-1]
            duration = last_message.timestamp - base
            job = CutoutJob(
                source_path=str(source),
                kind=SourceKind.BLF,
                window=CutoutWindow(0.0, duration + 1.0),
                output_path=str(Path(folder, "cut.blf")),
                format=OutputFormat.BLF,
                scope=OutputScope.ALL,
                total_messages=150_000,
            )
            worker = CutoutWorker(job)
            heartbeats = []
            timer = QTimer()
            timer.setInterval(10)
            timer.timeout.connect(lambda: heartbeats.append(time.monotonic()))
            timer.start()
            worker.start()
            self.assertTrue(wait_until(lambda: not worker.isRunning(), timeout=30.0))
            timer.stop()
            worker.wait()
        self.assertGreaterEqual(len(heartbeats), 5)
        delays = [b - a for a, b in zip(heartbeats, heartbeats[1:])]
        self.assertLess(max(delays, default=0.0), 0.1)


class CloseHandlingTests(unittest.TestCase):
    class StubDialog:
        option = CutoutOption(OutputFormat.BLF, OutputScope.ALL, "", "")
        path = None

        def __init__(self, *_args, **_kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selected_option(self):
            return self.option

        def output_path(self):
            return self.path

    def test_close_during_cutout_waits_for_worker(self):
        base = 1_700_000_000.0
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, "source.blf")
            make_blf(source, count=150_000)
            output = Path(folder, "cut.blf")
            window = MDFPlotter()
            try:
                window._gui_settings = type("S", (), {
                    "value": lambda self, key, default=None: None,
                    "setValue": lambda self, key, value: None,
                    "sync": lambda self: None,
                })()
                window.mdf_path = str(source)
                window.master_viewbox = type("VB", (), {
                    "viewRange": lambda _self: [[0.0, 140.0], [0.0, 1.0]],
                })()
                type(self).StubDialog.path = output
                with patch.object(baseline, "CutoutDialog", type(self).StubDialog), \
                     patch.object(QMessageBox, "information"):
                    window.save_data_cutout()
                    window.close()
                    self.assertTrue(window._close_after_cutout)
                    self.assertTrue(wait_until(lambda: window._cutout_worker is None, timeout=30.0))
            finally:
                window.deleteLater()
                APP.processEvents()
        self.assertFalse(window.isVisible())


if __name__ == "__main__":
    unittest.main()
