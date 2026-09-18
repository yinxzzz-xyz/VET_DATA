import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import can
from PyQt6.QtWidgets import QApplication

from vet_data_modular.blf_slice_execution import build_slice_execution_plan, execute_slice
from vet_data_modular.blf_slice_models import (
    BlfIndexEntry, BlfTimeRangeStatus, ConditionResult, ConditionSpec,
    ConditionStatus, InputMode, SliceTask, TaskResult, TaskStatus,
)
from vet_data_modular.blf_slice_progress import (
    BlfSliceProgressDialog, BlfSliceResultDialog, BlfSliceWorker,
)
from vet_data_modular.blf_slice_service import (
    BlfSourceChain, ConditionCandidateMatch, SourceChainBuildResult,
)
from vet_data_modular.blf_slice_table import TableParseResult
from vet_data_modular.blf_slice_time import build_condition_time_window


APP = QApplication.instance() or QApplication([])


class Stage5bCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.table = self.root / "conditions.csv"
        self.table.write_text("记录时间,记录内容\n2026/1/1 0:00,测试\n", encoding="utf-8-sig")
        self.base_dt = datetime(2026, 1, 1)
        self.base = self.base_dt.replace(tzinfo=timezone.utc).timestamp()

    def tearDown(self):
        self.temp.cleanup()

    def _write_blf(self, path, offsets):
        writer = can.BLFWriter(path)
        try:
            for offset in offsets:
                writer.on_message_received(can.Message(timestamp=self.base + offset, arbitration_id=0x100 + offset, data=[offset]))
        finally:
            writer.stop()

    def _condition(self, row, name, second):
        return ConditionSpec(row, name, name, self.base_dt + timedelta(seconds=second), 1, 1)

    def _entry(self, path, start, stop):
        return BlfIndexEntry(
            path, time_range_status=BlfTimeRangeStatus.CONFIRMED,
            effective_start_timestamp=self.base + start,
            effective_stop_timestamp=self.base + stop,
        )

    def _task(self, source, conditions):
        return SliceTask(
            InputMode.FILE, source, self.table, self.root, tuple(conditions),
            datetime(2026, 1, 1, 1), timedelta(0), timedelta(0),
        )

    def test_cancel_during_write_cleans_part_and_never_commits(self):
        source = self.root / "source.blf"
        self._write_blf(source, (0, 1, 2))
        condition = self._condition(2, "写入取消", 1)
        entry = self._entry(source, 0, 2)
        plan = build_slice_execution_plan(
            (ConditionCandidateMatch(build_condition_time_window(condition, timedelta(0)), (source,)),),
            SourceChainBuildResult((BlfSourceChain((entry,), ()),)), (condition,),
        )
        token = Event()

        class CancellingWriter:
            def __init__(self, path):
                self.path = Path(path)
                self.path.write_bytes(b"partial")
            def on_message_received(self, _message):
                token.set()
            def stop(self):
                pass

        result = execute_slice(
            self._task(source, (condition,)), plan, (entry,),
            writer_factory=CancellingWriter, cancel_event=token,
            progress_interval=100000,
        )
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        self.assertEqual(result.condition_results[0].status, ConditionStatus.CANCELLED)
        self.assertFalse(list(self.root.glob("*.part")))
        self.assertFalse(list(self.root.glob("写入取消-*.blf")))
        self.assertTrue(source.is_file())

    def test_cancel_preserves_target_committed_before_later_source(self):
        first_path, second_path = self.root / "first.blf", self.root / "second.blf"
        self._write_blf(first_path, (1,))
        self._write_blf(second_path, (20,))
        first_condition, second_condition = self._condition(2, "已完成", 1), self._condition(3, "未开始", 20)
        first_entry, second_entry = self._entry(first_path, 0, 2), self._entry(second_path, 19, 21)
        matches = (
            ConditionCandidateMatch(build_condition_time_window(first_condition, timedelta(0)), (first_path,)),
            ConditionCandidateMatch(build_condition_time_window(second_condition, timedelta(0)), (second_path,)),
        )
        plan = build_slice_execution_plan(
            matches,
            SourceChainBuildResult((BlfSourceChain((first_entry,), ()), BlfSourceChain((second_entry,), ()))),
            (first_condition, second_condition),
        )
        token = Event()
        def cancel_after_first(progress):
            if progress.path == first_path:
                token.set()
        result = execute_slice(
            self._task(first_path, (first_condition, second_condition)), plan,
            (first_entry, second_entry), cancel_event=token,
            progress_callback=cancel_after_first, progress_interval=100000,
        )
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        self.assertEqual(result.condition_results[0].status, ConditionStatus.COMPLETE)
        self.assertTrue(result.condition_results[0].output_files[0].is_file())
        self.assertEqual(result.condition_results[1].status, ConditionStatus.NOT_PROCESSED)
        self.assertFalse(list(self.root.glob("*.part")))

    def test_worker_early_cancel_safely_exits_with_report_and_states(self):
        source = self.root / "source.blf"
        source.write_bytes(b"not opened because cancellation is already requested")
        selected = self._condition(2, "待处理", 0)
        unselected = ConditionSpec(3, "未选择", "未选择", self.base_dt, selected=False)
        task = self._task(source, (selected, unselected))
        parsed = TableParseResult(self.table, "utf-8-sig", (selected, unselected))
        worker = BlfSliceWorker(task, parsed)
        received = []
        worker.cancelled.connect(received.append)
        worker.cancel()
        worker.start()
        self.assertTrue(worker.wait(3000))
        APP.processEvents()
        self.assertEqual(len(received), 1)
        result = received[0]
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        self.assertEqual([item.status for item in result.condition_results], [ConditionStatus.NOT_PROCESSED, ConditionStatus.NOT_SELECTED])
        self.assertIsNotNone(result.report_path)
        self.assertTrue(result.report_path.is_file())
        report = result.report_path.read_text(encoding="utf-8")
        self.assertIn("## 运行事件", report)
        self.assertIn("safe_cleanup", report)
        self.assertFalse(worker.isRunning())
        worker.deleteLater()

    def test_worker_failure_returns_failed_result_and_report(self):
        missing = self.root / "missing.blf"
        condition = self._condition(2, "失败", 0)
        task = self._task(missing, (condition,))
        worker = BlfSliceWorker(task, TableParseResult(self.table, "utf-8-sig", (condition,)))
        received = []
        worker.failed.connect(lambda message, result: received.append((message, result)))
        worker.start()
        self.assertTrue(worker.wait(3000))
        APP.processEvents()
        self.assertEqual(len(received), 1)
        message, result = received[0]
        self.assertIn("BLF 输入路径不存在", message)
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.condition_results[0].status, ConditionStatus.FAILED)
        self.assertTrue(result.report_path.is_file())
        report = result.report_path.read_text(encoding="utf-8")
        self.assertIn("任务状态：failed", report)
        self.assertIn("error|-|ValueError", report)
        self.assertFalse(worker.isRunning())
        worker.deleteLater()

    def test_progress_cancel_is_idempotent_and_explicit(self):
        source = self.root / "source.blf"
        source.write_bytes(b"x")
        condition = self._condition(2, "取消", 0)
        worker = BlfSliceWorker(self._task(source, (condition,)), TableParseResult(self.table, "utf-8-sig", (condition,)))
        dialog = BlfSliceProgressDialog(worker)
        dialog.request_cancel()
        dialog.request_cancel()
        self.assertTrue(worker.cancel_event.is_set())
        self.assertFalse(dialog.cancel_button.isEnabled())
        self.assertEqual(dialog.cancel_button.text(), "正在取消…")
        dialog.deleteLater()
        worker.deleteLater()


class Stage5bResultDialogTests(unittest.TestCase):
    def test_result_dialog_displays_stage4_status_counts_outputs_warnings_and_errors(self):
        root = Path("output")
        conditions = (
            ConditionSpec(2, "完整", "完整", datetime(2026, 1, 1)),
            ConditionSpec(3, "无数据", "无数据", datetime(2026, 1, 1)),
            ConditionSpec(4, "部分", "部分", datetime(2026, 1, 1)),
            ConditionSpec(5, "失败", "失败", datetime(2026, 1, 1)),
            ConditionSpec(6, "取消", "取消", datetime(2026, 1, 1)),
        )
        task = SliceTask(InputMode.FILE, Path("input.blf"), Path("table.csv"), root, conditions, datetime(2026, 1, 1))
        statuses = (ConditionStatus.COMPLETE, ConditionStatus.NO_DATA, ConditionStatus.PARTIAL, ConditionStatus.FAILED, ConditionStatus.CANCELLED)
        results = tuple(
            ConditionResult(
                condition, status, output_files=(root / "result.blf",) if index == 0 else (),
                message_count=10 if index == 0 else 0,
                warnings=("warning",) if index == 2 else (), errors=("error",) if index == 3 else (),
            )
            for index, (condition, status) in enumerate(zip(conditions, statuses))
        )
        result = TaskResult(task, TaskStatus.PARTIAL_COMPLETED, results, report_path=root / "report.md")
        dialog = BlfSliceResultDialog(result)
        self.assertEqual(dialog.table.rowCount(), 5)
        self.assertEqual([dialog.table.item(row, 1).text() for row in range(5)], [status.value for status in statuses])
        self.assertEqual(dialog.table.item(0, 2).text(), "10")
        self.assertIn("result.blf", dialog.table.item(0, 3).text())
        self.assertEqual(dialog.table.item(2, 4).text(), "warning")
        self.assertEqual(dialog.table.item(3, 5).text(), "error")
        dialog.close()
        dialog.deleteLater()


class Stage5bPackagingConfigTests(unittest.TestCase):
    def test_runtime_and_build_dependencies_are_version_locked(self):
        requirements = Path("requirements.txt").read_text(encoding="utf-8").splitlines()
        expected = {
            "PyQt6", "PyQt6-WebEngine", "python-can", "openpyxl", "pandas",
            "numpy", "asammdf", "pyqtgraph", "cantools", "canmatrix", "pyinstaller",
        }
        names = {line.split("==", 1)[0] for line in requirements if line.strip()}
        self.assertEqual(names, expected)
        self.assertTrue(all("==" in line for line in requirements if line.strip()))

    def test_spec_uses_supported_launcher_and_covers_dynamic_features(self):
        spec = Path("VET_DATA.spec").read_text(encoding="utf-8")
        self.assertIn('"vet_data/VET_DATA_merged.py"', spec)
        self.assertIn('pathex=["vet_data"]', spec)
        self.assertIn('collect_submodules("can.io")', spec)
        self.assertIn('collect_submodules("canmatrix.formats")', spec)
        self.assertIn('"PyQt6.QtWebEngineWidgets"', spec)
        self.assertIn('"openpyxl.cell._writer"', spec)
        self.assertIn("console=False", spec)


if __name__ == "__main__":
    unittest.main()
