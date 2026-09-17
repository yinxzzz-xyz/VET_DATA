import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import can

from vet_data_modular.blf_slice_execution import (
    build_slice_execution_plan,
    execute_slice,
)
from vet_data_modular.blf_slice_models import (
    BlfIndexEntry,
    BlfTimeRangeStatus,
    ConditionSpec,
    ConditionStatus,
    InputMode,
    SliceTask,
)
from vet_data_modular.blf_slice_report import build_report_markdown, write_report
from vet_data_modular.blf_slice_service import (
    BlfSourceChain,
    ConditionCandidateMatch,
    SourceChainBuildResult,
)
from vet_data_modular.blf_slice_time import build_condition_time_window


class Stage4EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.table = self.root / "conditions.csv"
        self.table.write_text("记录时间,记录内容\n", encoding="utf-8")
        self.base_dt = datetime(2026, 1, 1)
        self.base = self.base_dt.replace(tzinfo=timezone.utc).timestamp()

    def tearDown(self):
        self.temp.cleanup()

    def _write_blf(self, path, messages):
        writer = can.BLFWriter(path)
        try:
            for message in messages:
                writer.on_message_received(message)
        finally:
            writer.stop()

    def _message(self, offset, arbitration_id, data=b"\x01", **kwargs):
        return can.Message(
            timestamp=self.base + offset,
            arbitration_id=arbitration_id,
            data=data,
            **kwargs,
        )

    def _task(self, source, conditions):
        return SliceTask(
            InputMode.FILE,
            source,
            self.table,
            self.root,
            tuple(conditions),
            datetime(2026, 1, 1, 12),
            blf_utc_offset=timedelta(0),
            table_utc_offset=timedelta(0),
        )

    def _entry(self, path, start, stop):
        return BlfIndexEntry(
            path=path,
            time_range_status=BlfTimeRangeStatus.CONFIRMED,
            effective_start_timestamp=self.base + start,
            effective_stop_timestamp=self.base + stop,
        )

    def test_real_blf_shared_scan_overlap_boundaries_and_frame_semantics(self):
        source = self.root / "source.blf"
        self._write_blf(
            source,
            [
                self._message(0, 0x100, b"\x00"),
                self._message(10, 0x101, b"\x10"),
                self._message(15, 0x200, b"", is_remote_frame=True, dlc=4),
                self._message(16, 0x201, b"", is_error_frame=True),
                self._message(20, 0x102, b"\x20\x21", is_fd=True, bitrate_switch=True),
                self._message(25, 0x103, b"\x25"),
            ],
        )
        first = ConditionSpec(2, "第一", "第一", self.base_dt + timedelta(seconds=10), 10, 10)
        second = ConditionSpec(3, "第二", "第二", self.base_dt + timedelta(seconds=15), 5, 5)
        entry = self._entry(source, 0, 25)
        matches = tuple(
            ConditionCandidateMatch(build_condition_time_window(condition, timedelta(0)), (source,))
            for condition in (first, second)
        )
        plan = build_slice_execution_plan(
            matches, SourceChainBuildResult((BlfSourceChain((entry,), ()),)), (first, second)
        )
        open_count = 0

        def reader_factory(path):
            nonlocal open_count
            open_count += 1
            return can.BLFReader(path)

        result = execute_slice(
            self._task(source, (first, second)),
            plan,
            (entry,),
            reader_factory=reader_factory,
            progress_interval=1,
        )
        self.assertEqual(open_count, 1)
        self.assertEqual([item.status for item in result.condition_results], [ConditionStatus.COMPLETE] * 2)
        self.assertEqual([item.message_count for item in result.condition_results], [3, 2])
        self.assertEqual([item.ignored_remote_frames for item in result.condition_results], [1, 1])
        self.assertEqual([item.ignored_error_frames for item in result.condition_results], [1, 1])

        first_messages = list(can.BLFReader(result.condition_results[0].output_files[0]))
        second_messages = list(can.BLFReader(result.condition_results[1].output_files[0]))
        self.assertEqual([message.arbitration_id for message in first_messages], [0x100, 0x101, 0x102])
        self.assertEqual([message.arbitration_id for message in second_messages], [0x101, 0x102])
        self.assertEqual(first_messages[-1].data, bytearray(b"\x20\x21"))
        self.assertTrue(first_messages[-1].is_fd)
        self.assertTrue(first_messages[-1].bitrate_switch)
        self.assertEqual(first_messages[0].timestamp, self.base)
        self.assertEqual(first_messages[-1].timestamp, self.base + 20)
        self.assertFalse(list(self.root.glob("*.blf.part")))

        report_path = write_report(result, self.root)
        report = report_path.read_text(encoding="utf-8")
        self.assertIn("状态：complete", report)
        self.assertIn("有效帧数量：3", report)
        self.assertIn(str(result.condition_results[0].output_files[0]), report)

    def test_continuous_sources_stream_into_one_output(self):
        first_path, second_path = self.root / "z-first.blf", self.root / "a-second.blf"
        self._write_blf(first_path, [self._message(0, 0x100), self._message(5, 0x105)])
        self._write_blf(second_path, [self._message(6, 0x106), self._message(10, 0x110)])
        condition = ConditionSpec(2, "跨卷", "跨卷", self.base_dt + timedelta(seconds=5), 5, 5)
        first, second = self._entry(first_path, 0, 5), self._entry(second_path, 6, 10)
        match = ConditionCandidateMatch(build_condition_time_window(condition, timedelta(0)), (first_path, second_path))
        plan = build_slice_execution_plan(
            (match,), SourceChainBuildResult((BlfSourceChain((first, second), (1,)),)), (condition,)
        )
        result = execute_slice(self._task(first_path, (condition,)), plan, (first, second))
        item = result.condition_results[0]
        self.assertEqual(item.status, ConditionStatus.PARTIAL)
        self.assertEqual(item.message_count, 4)
        self.assertEqual(len(item.output_files), 1)
        self.assertEqual([m.arbitration_id for m in can.BLFReader(item.output_files[0])], [0x100, 0x105, 0x106, 0x110])

    def test_zero_valid_frames_creates_no_formal_or_part_file(self):
        source = self.root / "remote_only.blf"
        self._write_blf(source, [self._message(5, 0x100, b"", is_remote_frame=True, dlc=1)])
        condition = ConditionSpec(2, "空", "空", self.base_dt + timedelta(seconds=5), 5, 5)
        entry = self._entry(source, 0, 10)
        plan = build_slice_execution_plan(
            (ConditionCandidateMatch(build_condition_time_window(condition, timedelta(0)), (source,)),),
            SourceChainBuildResult((BlfSourceChain((entry,), ()),)),
            (condition,),
        )
        result = execute_slice(self._task(source, (condition,)), plan, (entry,))
        item = result.condition_results[0]
        self.assertEqual(item.status, ConditionStatus.NO_DATA)
        self.assertEqual(item.ignored_remote_frames, 1)
        self.assertEqual(item.output_files, ())
        self.assertFalse(list(self.root.glob("空-*.blf")))
        self.assertFalse(list(self.root.glob("*.part")))

    def test_writer_failure_cleans_part_and_reports_failed(self):
        source = self.root / "source.blf"
        self._write_blf(source, [self._message(5, 0x100)])
        condition = ConditionSpec(2, "失败", "失败", self.base_dt + timedelta(seconds=5), 5, 5)
        entry = self._entry(source, 0, 10)
        plan = build_slice_execution_plan(
            (ConditionCandidateMatch(build_condition_time_window(condition, timedelta(0)), (source,)),),
            SourceChainBuildResult((BlfSourceChain((entry,), ()),)),
            (condition,),
        )

        class FailingWriter:
            def __init__(self, path):
                Path(path).write_bytes(b"partial")

            def on_message_received(self, message):
                raise OSError("disk full")

            def stop(self):
                pass

        result = execute_slice(
            self._task(source, (condition,)), plan, (entry,), writer_factory=FailingWriter
        )
        self.assertEqual(result.condition_results[0].status, ConditionStatus.FAILED)
        self.assertFalse(list(self.root.glob("失败-*.blf")))
        self.assertFalse(list(self.root.glob("*.part")))
        self.assertIn("write_error", build_report_markdown(result))

    def test_existing_formal_output_is_preserved_and_suffix_is_used(self):
        source = self.root / "source.blf"
        self._write_blf(source, [self._message(5, 0x100)])
        condition = ConditionSpec(2, "冲突", "冲突", self.base_dt + timedelta(seconds=5), 5, 5)
        existing = self.root / "冲突-20260101-000005.blf"
        existing.write_bytes(b"keep")
        foreign_part = self.root / "冲突-20260101-000005-1.blf.part"
        foreign_part.write_bytes(b"foreign")
        entry = self._entry(source, 0, 10)
        plan = build_slice_execution_plan(
            (ConditionCandidateMatch(build_condition_time_window(condition, timedelta(0)), (source,)),),
            SourceChainBuildResult((BlfSourceChain((entry,), ()),)),
            (condition,),
        )
        result = execute_slice(self._task(source, (condition,)), plan, (entry,))
        self.assertEqual(existing.read_bytes(), b"keep")
        self.assertEqual(foreign_part.read_bytes(), b"foreign")
        self.assertEqual(result.condition_results[0].output_files[0].name, "冲突-20260101-000005-2.blf")

    def test_read_failure_is_failed_and_creates_no_output(self):
        source = self.root / "broken.blf"
        source.write_bytes(b"broken")
        condition = ConditionSpec(2, "读取失败", "读取失败", self.base_dt + timedelta(seconds=5), 5, 5)
        entry = self._entry(source, 0, 10)
        plan = build_slice_execution_plan(
            (ConditionCandidateMatch(build_condition_time_window(condition, timedelta(0)), (source,)),),
            SourceChainBuildResult((BlfSourceChain((entry,), ()),)),
            (condition,),
        )
        def failing_reader(path):
            raise OSError("corrupt BLF")

        result = execute_slice(
            self._task(source, (condition,)), plan, (entry,), reader_factory=failing_reader
        )
        self.assertEqual(result.condition_results[0].status, ConditionStatus.FAILED)
        self.assertEqual(result.condition_results[0].output_files, ())
        self.assertFalse(list(self.root.glob("*.part")))

    def test_cancel_marks_current_and_not_yet_started_conditions(self):
        first_path, second_path = self.root / "first.blf", self.root / "second.blf"
        self._write_blf(first_path, [self._message(1, 0x101), self._message(2, 0x102)])
        self._write_blf(second_path, [self._message(20, 0x120)])
        first_condition = ConditionSpec(2, "当前", "当前", self.base_dt + timedelta(seconds=1), 1, 2)
        second_condition = ConditionSpec(3, "未开始", "未开始", self.base_dt + timedelta(seconds=20), 1, 1)
        unselected = ConditionSpec(4, "未选择", "未选择", self.base_dt, selected=False)
        first_entry, second_entry = self._entry(first_path, 0, 3), self._entry(second_path, 19, 21)
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

        def cancel_on_progress(progress):
            token.set()

        result = execute_slice(
            self._task(first_path, (first_condition, second_condition, unselected)),
            plan,
            (first_entry, second_entry),
            cancel_event=token,
            progress_callback=cancel_on_progress,
            progress_interval=1,
        )
        self.assertEqual(
            [item.status for item in result.condition_results],
            [ConditionStatus.CANCELLED, ConditionStatus.NOT_PROCESSED, ConditionStatus.NOT_SELECTED],
        )
        self.assertFalse(list(self.root.glob("*.part")))
        self.assertFalse([path for path in self.root.glob("*.blf") if path not in (first_path, second_path)])

    def test_report_name_conflict_is_non_overwriting(self):
        source = self.root / "none.blf"
        condition = ConditionSpec(2, "空", "空", self.base_dt, 1, 1)
        plan = build_slice_execution_plan(
            (ConditionCandidateMatch(build_condition_time_window(condition, timedelta(0))),),
            SourceChainBuildResult(),
            (condition,),
        )
        result = execute_slice(self._task(source, (condition,)), plan)
        first = write_report(result, self.root)
        second = write_report(result, self.root)
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())


if __name__ == "__main__":
    unittest.main()
