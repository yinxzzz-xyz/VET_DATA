import os
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import can
from PyQt6.QtCore import QThread, QTimer
from PyQt6.QtWidgets import QApplication

from vet_data_modular import baseline
from vet_data_modular.window import MDFPlotter
from vet_data_modular.workers import (
    CanFileIdentity,
    CanBusDetectionCancelled,
    CanBusDetectionWorker,
    detect_can_bus_info,
    normalized_can_path,
)


APP = QApplication.instance() or QApplication([])


def wait_until(predicate, timeout=3.0):
    deadline = time.perf_counter() + timeout
    while not predicate() and time.perf_counter() < deadline:
        APP.processEvents()
        time.sleep(0.002)
    APP.processEvents()
    return predicate()


class FakeReader:
    def __init__(self, messages):
        self.messages = messages
        self.stopped = False

    def __iter__(self):
        return iter(self.messages)

    def stop(self):
        self.stopped = True


class CanBusDetectionBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.identity_patcher = patch(
            "vet_data_modular.workers.capture_can_file_identity",
            side_effect=lambda path: CanFileIdentity(
                normalized_can_path(path), 100, 200
            ),
        )
        self.identity_patcher.start()
        self.messages = [
            can.Message(timestamp=100.0, channel=0, arbitration_id=0x101, data=b"\x01"),
            can.Message(timestamp=100.1, channel=1, arbitration_id=0x202, data=b"\x02\x03", is_fd=True),
            can.Message(timestamp=100.2, channel=0, arbitration_id=0x101, data=b"\x04"),
        ]

    def tearDown(self):
        self.identity_patcher.stop()

    def test_background_result_is_field_for_field_equal_to_legacy_sync_result(self):
        legacy_reader = FakeReader(self.messages)
        background_reader = FakeReader(self.messages)

        class LegacyTarget:
            def __init__(self):
                self.can_bus_data = {}
                self.bus_loaded = False

            def _print_bus_info(self):
                pass

        target = LegacyTarget()
        with patch(
            "vet_data_modular.workers.can.BLFReader",
            side_effect=[legacy_reader, background_reader],
        ):
            baseline.MDFPlotter.load_can_bus_info(target, "input.blf")
            result = detect_can_bus_info("input.blf")

        self.assertEqual(result, target.can_bus_data)
        self.assertTrue(target.bus_loaded)
        self.assertTrue(legacy_reader.stopped)
        self.assertTrue(background_reader.stopped)
        self.assertEqual(result[1]["original_bus_id"], 0)
        self.assertEqual(result[2]["original_bus_id"], 1)
        self.assertEqual(result[2]["msg_count"], 1)
        self.assertTrue(result[2]["has_fd"])

    def test_cancel_is_cooperative_emits_cancelled_and_finishes(self):
        entered = threading.Event()

        def cancellable(_path, cancel_event, _progress):
            entered.set()
            while not cancel_event.wait(0.002):
                pass
            raise CanBusDetectionCancelled()

        worker = CanBusDetectionWorker("input.blf")
        cancelled = []
        worker.cancelled.connect(lambda: cancelled.append(True))
        with patch("vet_data_modular.workers.detect_can_bus_info", side_effect=cancellable):
            worker.start()
            self.assertTrue(entered.wait(1))
            started = time.perf_counter()
            worker.cancel()
            self.assertTrue(wait_until(lambda: not worker.isRunning()))
        self.assertTrue(cancelled)
        self.assertLess(time.perf_counter() - started, 1.0)
        worker.deleteLater()

    def test_gui_event_loop_keeps_ticking_while_detection_runs(self):
        release = threading.Event()
        worker_thread = []

        def blocked_scan(_path, _cancel_event, _progress):
            worker_thread.append(QThread.currentThread())
            release.wait(0.25)
            return {}

        ticks = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
        worker = CanBusDetectionWorker("input.blf")
        with patch("vet_data_modular.workers.detect_can_bus_info", side_effect=blocked_scan):
            timer.start()
            worker.start()
            self.assertTrue(wait_until(lambda: len(ticks) >= 5, timeout=1.0))
            self.assertTrue(worker.isRunning())
            release.set()
            self.assertTrue(wait_until(lambda: not worker.isRunning()))
        timer.stop()
        self.assertIsNot(worker_thread[0], APP.thread())
        self.assertGreaterEqual(len(ticks), 5)
        max_delay = max(b - a for a, b in zip(ticks, ticks[1:]))
        self.assertLess(max_delay, 0.1)
        worker.deleteLater()

    def test_window_close_requests_cancel_and_waits_for_worker_cleanup(self):
        entered = threading.Event()

        def cancellable(_path, cancel_event, _progress):
            entered.set()
            while not cancel_event.wait(0.002):
                pass
            raise CanBusDetectionCancelled()

        window = MDFPlotter()
        try:
            with patch(
                "vet_data_modular.window.QFileDialog.getOpenFileName",
                return_value=("input.blf", ""),
            ), patch(
                "vet_data_modular.workers.detect_can_bus_info",
                side_effect=cancellable,
            ):
                window.load_file_dialog()
                self.assertTrue(entered.wait(1))
                worker = window._can_bus_detection_worker
                finished = []
                worker.finished.connect(lambda: finished.append(True))
                self.assertTrue(worker.isRunning())
                window.close()
                self.assertTrue(wait_until(lambda: window._can_bus_detection_worker is None))
                self.assertTrue(finished)
        finally:
            window.deleteLater()
            APP.processEvents()


if __name__ == "__main__":
    unittest.main()
