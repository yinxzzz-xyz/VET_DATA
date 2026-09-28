import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QThread, qInstallMessageHandler
from PyQt6.QtWidgets import QApplication

from vet_data_modular import baseline


APP = QApplication.instance() or QApplication([])


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(0.005)
    APP.processEvents()
    if not predicate():
        raise AssertionError("ARXML conversion worker did not finish")


class ArxmlConversionStabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "input.arxml"
        self.source.write_text("<AUTOSAR/>", encoding="utf-8")
        self.target = self.root / "dbc_output" / "input.dbc"
        self.dialog = baseline.ARXMLConverterDialog()
        self.dialog.arxml_path = str(self.source)
        self.dialog.dbc_path = str(self.target)
        self.dialog.arxml_path_edit.setText(str(self.source))
        self.dialog.dbc_path_edit.setText(str(self.target))
        self.dialog.update_convert_button()

    def tearDown(self):
        worker = self.dialog._conversion_worker
        if worker is not None and worker.isRunning():
            worker.wait(5000)
        self.dialog.deleteLater()
        APP.processEvents()
        self.temporary.cleanup()

    @staticmethod
    def _write_success(_source, target, log):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text("BO_ 1 Message: 8 Vector__XXX\n", encoding="utf-8")
        log("worker log")

    def test_worker_runs_core_off_gui_thread_and_widgets_update_on_gui_thread(self):
        worker_threads = []
        gui_threads = []
        original_log = self.dialog.log

        def core(source, target, log):
            worker_threads.append(QThread.currentThread())
            self._write_success(source, target, log)

        def gui_log(message):
            gui_threads.append(QThread.currentThread())
            original_log(message)

        self.dialog.log = gui_log
        with patch.object(baseline, "_convert_arxml_to_dbc", side_effect=core), \
             patch.object(baseline.QMessageBox, "information"):
            self.dialog.start_conversion()
            self.assertIsNotNone(self.dialog._conversion_worker)
            _wait(lambda: self.dialog._conversion_worker is None)
        self.assertIsNot(worker_threads[0], APP.thread())
        self.assertTrue(gui_threads)
        self.assertTrue(all(thread is APP.thread() for thread in gui_threads))
        self.assertEqual(self.dialog.status_label.text(), "转换成功！")
        self.assertTrue(self.dialog.convert_btn.isEnabled())
        self.assertTrue(self.dialog.arxml_select_btn.isEnabled())

    def test_exception_recovers_and_second_conversion_succeeds(self):
        calls = []

        def core(source, target, log):
            calls.append(target)
            if len(calls) == 1:
                log("diagnostic before failure")
                raise RuntimeError("synthetic conversion failure")
            self._write_success(source, target, log)

        with patch.object(baseline, "_convert_arxml_to_dbc", side_effect=core), \
             patch.object(baseline.QMessageBox, "critical"), \
             patch.object(baseline.QMessageBox, "information"):
            self.dialog.start_conversion()
            _wait(lambda: self.dialog._conversion_worker is None)
            self.assertEqual(self.dialog.status_label.text(), "转换失败")
            self.assertIn("synthetic conversion failure", self.dialog.log_text.toPlainText())
            self.assertTrue(self.dialog.convert_btn.isEnabled())
            self.assertFalse(self.dialog.progress_bar.isVisible())

            self.dialog.start_conversion()
            _wait(lambda: self.dialog._conversion_worker is None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.dialog.status_label.text(), "转换成功！")
        self.assertTrue(self.dialog.open_folder_btn.isEnabled())

    def test_two_successive_conversions_have_distinct_tracked_workers(self):
        workers = []
        with patch.object(
            baseline, "_convert_arxml_to_dbc", side_effect=self._write_success
        ), patch.object(baseline.QMessageBox, "information"):
            for _ in range(2):
                self.dialog.start_conversion()
                workers.append(self.dialog._conversion_worker)
                _wait(lambda: self.dialog._conversion_worker is None)
        self.assertIsNot(workers[0], workers[1])
        self.assertIsNone(self.dialog._conversion_worker)
        self.assertTrue(self.dialog.convert_btn.isEnabled())

    def test_close_during_conversion_waits_then_closes_safely(self):
        release = threading.Event()

        def delayed(source, target, log):
            log("conversion is running")
            release.wait(3)
            self._write_success(source, target, log)

        self.dialog.show()
        with patch.object(baseline, "_convert_arxml_to_dbc", side_effect=delayed), \
             patch.object(baseline.QMessageBox, "information"):
            self.dialog.start_conversion()
            _wait(lambda: self.dialog._conversion_worker is not None)
            self.dialog.close()
            APP.processEvents()
            self.assertTrue(self.dialog.isVisible())
            self.assertTrue(self.dialog._close_after_conversion)
            self.assertIn("安全关闭", self.dialog.log_text.toPlainText())
            release.set()
            _wait(lambda: self.dialog._conversion_worker is None)
            _wait(lambda: not self.dialog.isVisible())
        self.assertFalse(self.dialog.is_running)

    def test_completed_conversion_can_close_normally(self):
        self.dialog.show()
        with patch.object(
            baseline, "_convert_arxml_to_dbc", side_effect=self._write_success
        ), patch.object(baseline.QMessageBox, "information"):
            self.dialog.start_conversion()
            _wait(lambda: self.dialog._conversion_worker is None)
        self.dialog.close()
        APP.processEvents()
        self.assertFalse(self.dialog.isVisible())

    def test_theme_never_requests_invalid_qfont_point_size(self):
        messages = []

        def handler(_message_type, _context, message):
            messages.append(message)

        previous = qInstallMessageHandler(handler)
        probe = None
        try:
            probe = baseline.ARXMLConverterDialog()
            probe.show()
            probe.resize(800, 700)
            APP.processEvents()
        finally:
            if probe is not None:
                probe.close()
                probe.deleteLater()
            APP.processEvents()
            qInstallMessageHandler(previous)
        invalid = [message for message in messages if "Point size <= 0" in message]
        self.assertEqual(invalid, [])


if __name__ == "__main__":
    unittest.main()
