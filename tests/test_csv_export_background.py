import os
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from vet_data_modular.window import MDFPlotter
from vet_data_modular.workers import (
    CsvExportCancelled,
    CsvExportSignal,
    CsvExportSnapshot,
    CsvExportWorker,
    export_csv_snapshot,
)


APP = QApplication.instance() or QApplication([])


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(0.005)
    APP.processEvents()
    return predicate()


class PlainSignal:
    def __init__(self, timestamps, samples):
        self.timestamps = np.asarray(timestamps)
        self.samples = np.asarray(samples)


class ResampleSignal(PlainSignal):
    def resample(self, raster):
        return PlainSignal(raster, np.interp(raster, self.timestamps, self.samples))


class SlowSignal(ResampleSignal):
    def resample(self, raster):
        time.sleep(0.2)
        return super().resample(raster)


def legacy_export(path, frequency, items):
    all_timestamps = []
    for item in items:
        all_timestamps.extend(item.signal.timestamps)
    t_start = np.min(all_timestamps)
    t_end = np.max(all_timestamps)
    raster = np.arange(t_start, t_end + 1 / frequency, 1 / frequency)
    data = {}
    for item in items:
        signal = item.signal
        if hasattr(signal, "resample"):
            data[item.display_name] = signal.resample(raster).samples
        else:
            df_signal = pd.DataFrame({
                "timestamp": signal.timestamps, "samples": signal.samples,
            }).set_index("timestamp")
            df_raster = pd.DataFrame({"timestamp": raster}).set_index("timestamp")
            aligned = pd.merge_asof(
                df_raster, df_signal, on="timestamp", direction="nearest"
            )
            data[item.display_name] = aligned["samples"].values
    frame = pd.DataFrame(data, index=raster)
    frame.index.name = "timestamp"
    frame.to_csv(path, encoding="utf-8")


class CsvExportBackgroundTests(unittest.TestCase):
    def test_worker_output_is_byte_equivalent_to_legacy_algorithm(self):
        items = (
            CsvExportSignal("numeric", ResampleSignal([0.0, 1.0, 2.0], [1.5, np.nan, 3.25])),
            CsvExportSignal("text", PlainSignal([0.0, 1.0, 2.0], ["off", "on", "off"])),
        )
        with tempfile.TemporaryDirectory() as folder:
            old_path = Path(folder, "old.csv")
            new_path = Path(folder, "new.csv")
            legacy_export(old_path, 2.0, items)
            export_csv_snapshot(CsvExportSnapshot(str(new_path), 2.0, items))
            self.assertEqual(old_path.read_bytes(), new_path.read_bytes())
            frame = pd.read_csv(new_path)
        self.assertEqual(list(frame.columns), ["timestamp", "numeric", "text"])
        self.assertEqual(len(frame), 5)
        self.assertEqual(frame.iloc[0]["text"], "off")
        self.assertEqual(frame.iloc[-1]["text"], "off")

    def test_unsorted_timestamps_preserve_global_min_max_semantics(self):
        item = CsvExportSignal("value", ResampleSignal([2, 0, 1], [20, 0, 10]))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "out.csv")
            export_csv_snapshot(CsvExportSnapshot(str(path), 1.0, (item,)))
            frame = pd.read_csv(path)
        np.testing.assert_allclose(frame["timestamp"], [0, 1, 2])

    def test_cancel_before_work_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "out.csv")
            event = Event()
            event.set()
            snapshot = CsvExportSnapshot(
                str(path), 1.0,
                (CsvExportSignal("a", ResampleSignal([0, 1], [1, 2])),),
            )
            with self.assertRaises(CsvExportCancelled):
                export_csv_snapshot(snapshot, event)
            self.assertFalse(path.exists())

    def test_worker_keeps_gui_event_loop_responsive(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = CsvExportSnapshot(
                str(Path(folder, "out.csv")), 10.0,
                (CsvExportSignal("slow", SlowSignal([0, 1], [0, 1])),),
            )
            worker = CsvExportWorker(snapshot)
            heartbeats = []
            timer = QTimer()
            timer.setInterval(10)
            timer.timeout.connect(lambda: heartbeats.append(time.monotonic()))
            timer.start()
            worker.start()
            self.assertTrue(wait_until(lambda: not worker.isRunning()))
            timer.stop()
            worker.wait()
        self.assertGreaterEqual(len(heartbeats), 5)
        delays = [b - a for a, b in zip(heartbeats, heartbeats[1:])]
        self.assertLess(max(delays, default=0), 0.1)

    def test_window_close_waits_for_running_export(self):
        window = MDFPlotter()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "out.csv")
            worker = CsvExportWorker(CsvExportSnapshot(
                str(path), 10.0,
                (CsvExportSignal("slow", SlowSignal([0, 1], [0, 1])),),
            ), window)
            window._csv_export_worker = worker
            worker.finished.connect(
                lambda: window._cleanup_csv_export(worker, window._csv_export_dialog)
            )
            from vet_data_modular.workers import BusyLoadDialog
            window._csv_export_dialog = BusyLoadDialog(window)
            worker.start()
            window.close()
            self.assertTrue(window._close_after_csv_export)
            self.assertTrue(wait_until(lambda: window._csv_export_worker is None))
        window.deleteLater()
        APP.processEvents()

    def test_timestamp_range_avoids_full_python_list_peak(self):
        timestamps = np.linspace(0, 100, 300_000)
        items = tuple(
            CsvExportSignal(str(index), PlainSignal(timestamps, timestamps))
            for index in range(4)
        )

        def old_bounds():
            values = []
            for item in items:
                values.extend(item.signal.timestamps)
            return min(values), max(values)

        def new_bounds():
            bounds = [
                (np.min(item.signal.timestamps), np.max(item.signal.timestamps))
                for item in items
            ]
            return min(x[0] for x in bounds), max(x[1] for x in bounds)

        tracemalloc.start()
        self.assertEqual(old_bounds(), (0.0, 100.0))
        old_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        tracemalloc.start()
        self.assertEqual(new_bounds(), (0.0, 100.0))
        new_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        self.assertLess(new_peak, old_peak // 20)


if __name__ == "__main__":
    unittest.main()
