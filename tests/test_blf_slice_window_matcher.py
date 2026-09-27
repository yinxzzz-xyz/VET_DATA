import math
import random
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import can

from vet_data_modular.blf_slice_execution import (
    SlicePlanTarget,
    _TargetState,
    _WindowMatcher,
    _dispatch_message,
    build_slice_execution_plan,
    execute_slice,
)
from vet_data_modular.blf_slice_models import (
    BlfIndexEntry,
    BlfTimeRangeStatus,
    ConditionSpec,
    InputMode,
    SliceTask,
)
from vet_data_modular.blf_slice_report import (
    OutputNameAllocator,
    OwnedTemporaryFiles,
    build_report_markdown,
)
from vet_data_modular.blf_slice_service import (
    BlfSourceChain,
    ConditionCandidateMatch,
    SourceChainBuildResult,
)
from vet_data_modular.blf_slice_time import ConditionTimeWindow


BASE_DATETIME = datetime(2026, 1, 1)
BASE_TIMESTAMP = BASE_DATETIME.replace(tzinfo=timezone.utc).timestamp()


def _state(index, start, end):
    condition = ConditionSpec(index + 2, f"condition-{index}", f"condition-{index}", BASE_DATETIME)
    target = SlicePlanTarget(
        f"target-{index}",
        condition,
        ConditionTimeWindow(start, end),
        (),
    )
    return _TargetState(target, object())


def _reference(states, timestamp):
    return [
        state
        for state in states
        if state.plan.window.start <= timestamp <= state.plan.window.end
    ]


class WindowMatcherTests(unittest.TestCase):
    def assert_matches(self, matcher, states, timestamp):
        self.assertEqual(
            matcher.matching_states(timestamp),
            _reference(states, timestamp),
        )

    def test_closed_boundaries_and_zero_length_window(self):
        states = [_state(0, 10, 20), _state(1, 15, 15)]
        matcher = _WindowMatcher(states)
        for timestamp in (9, 10, 14, 15, 20, 21):
            with self.subTest(timestamp=timestamp):
                self.assert_matches(matcher, states, timestamp)

    def test_overlap_equal_nested_and_unsorted_windows_preserve_identity_order(self):
        states = [
            _state(0, 15, 25),
            _state(1, 10, 20),
            _state(2, 10, 20),
            _state(3, 10, 30),
            _state(4, 5, 20),
        ]
        matcher = _WindowMatcher(states)
        self.assert_matches(matcher, states, 17)
        self.assertEqual(matcher.matching_states(17), states)

    def test_one_hundred_highly_overlapping_windows_remain_distinct(self):
        states = [_state(index, 0, 200) for index in range(100)]
        self.assertEqual(_WindowMatcher(states).matching_states(100), states)

    def test_equal_timestamps_and_multiple_reversals_rebuild_locally(self):
        states = [
            _state(0, 0, 5),
            _state(1, 3, 10),
            _state(2, 8, 12),
            _state(3, 1, 20),
        ]
        matcher = _WindowMatcher(states)
        for timestamp in (0, 3, 3, 9, 2, 4, 11, 1, 21, 10):
            with self.subTest(timestamp=timestamp):
                self.assert_matches(matcher, states, timestamp)

    def test_failed_and_cancelled_states_are_not_removed_from_matching(self):
        states = [_state(0, 0, 10), _state(1, 0, 10)]
        states[0].failed = True
        states[1].cancelled = True
        self.assertEqual(_WindowMatcher(states).matching_states(5), states)

    def test_randomized_ordered_identity_differential(self):
        randomizer = random.Random(20260926)
        for case in range(100):
            count = randomizer.randint(1, 100)
            windows = []
            for index in range(count):
                if windows and randomizer.random() < 0.2:
                    start, end = randomizer.choice(windows)
                else:
                    start = randomizer.randint(-20, 100)
                    end = start + randomizer.randint(0, 40)
                windows.append((start, end))
            states = [_state(index, start, end) for index, (start, end) in enumerate(windows)]
            randomizer.shuffle(states)
            timestamps = [randomizer.randint(-30, 150) for _ in range(200)]
            timestamps.extend(value for window in windows for value in window)
            matcher = _WindowMatcher(states)
            for timestamp in timestamps:
                actual = matcher.matching_states(timestamp)
                expected = _reference(states, timestamp)
                self.assertEqual(actual, expected, msg=f"case={case}, timestamp={timestamp}")

    def test_invalid_timestamp_bypasses_matcher_and_keeps_existing_statistics(self):
        states = [_state(0, 0, 10), _state(1, 20, 30)]

        class MatcherMustNotRun:
            def matching_states(self, timestamp):
                raise AssertionError("invalid timestamp reached matcher")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = SliceTask(
                InputMode.FILE,
                root / "source.blf",
                root / "conditions.csv",
                root,
                tuple(state.plan.condition for state in states),
                BASE_DATETIME,
                blf_utc_offset=timedelta(0),
                table_utc_offset=timedelta(0),
            )
            message = type("InvalidMessage", (), {"timestamp": math.nan})()
            _dispatch_message(
                task,
                message,
                states,
                MatcherMustNotRun(),
                lambda path: None,
                OwnedTemporaryFiles(),
                OutputNameAllocator(root),
            )

        self.assertEqual([state.ignored_other for state in states], [1, 1])
        self.assertEqual([state.warnings for state in states], [["ignored_invalid_timestamp"]] * 2)


class WindowMatcherExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.table = self.root / "conditions.csv"
        self.table.write_text("记录时间,记录内容\n", encoding="utf-8")

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
            timestamp=BASE_TIMESTAMP + offset,
            arbitration_id=arbitration_id,
            data=data,
            **kwargs,
        )

    def _entry(self, path, start, end):
        return BlfIndexEntry(
            path,
            time_range_status=BlfTimeRangeStatus.CONFIRMED,
            effective_start_timestamp=BASE_TIMESTAMP + start,
            effective_stop_timestamp=BASE_TIMESTAMP + end,
        )

    def _task(self, source, output, conditions):
        output.mkdir(parents=True, exist_ok=True)
        return SliceTask(
            InputMode.FILE,
            source,
            self.table,
            output,
            tuple(conditions),
            BASE_DATETIME,
            blf_utc_offset=timedelta(0),
            table_utc_offset=timedelta(0),
        )

    def test_each_source_scan_gets_a_new_matcher_while_writer_spans_chain(self):
        first_path = self.root / "first.blf"
        second_path = self.root / "second.blf"
        self._write_blf(first_path, [self._message(0, 0x100), self._message(5, 0x105)])
        self._write_blf(second_path, [self._message(6, 0x106), self._message(10, 0x110)])
        condition = ConditionSpec(2, "跨卷", "跨卷", BASE_DATETIME + timedelta(seconds=5), 5, 5)
        first = self._entry(first_path, 0, 5)
        second = self._entry(second_path, 6, 10)
        match = ConditionCandidateMatch(
            ConditionTimeWindow(BASE_TIMESTAMP, BASE_TIMESTAMP + 10),
            (first_path, second_path),
        )
        plan = build_slice_execution_plan(
            (match,),
            SourceChainBuildResult((BlfSourceChain((first, second), (1,)),)),
            (condition,),
        )
        created = []
        implementation = _WindowMatcher

        class CountingMatcher(implementation):
            def __init__(self, states):
                created.append(tuple(states))
                super().__init__(states)

        with patch("vet_data_modular.blf_slice_execution._WindowMatcher", CountingMatcher):
            result = execute_slice(self._task(first_path, self.root / "output", (condition,)), plan)

        self.assertEqual(len(created), 2)
        item = result.condition_results[0]
        self.assertEqual(item.message_count, 4)
        self.assertEqual(len(item.output_files), 1)
        self.assertEqual(
            [message.arbitration_id for message in can.BLFReader(item.output_files[0])],
            [0x100, 0x105, 0x106, 0x110],
        )

    def test_real_blf_output_and_report_match_linear_reference(self):
        source = self.root / "source.blf"
        self._write_blf(
            source,
            [
                self._message(0, 0x100, b"\x00", channel=0),
                self._message(10, 0x101, b"\x10", channel=1),
                self._message(15, 0x200, b"", is_remote_frame=True, dlc=4),
                self._message(16, 0x201, b"", is_error_frame=True),
                self._message(
                    20,
                    0x102,
                    b"\x20\x21",
                    channel=2,
                    is_fd=True,
                    bitrate_switch=True,
                    error_state_indicator=True,
                ),
                self._message(25, 0x103, b"\x25", channel=3),
            ],
        )
        conditions = (
            ConditionSpec(2, "第一", "第一", BASE_DATETIME + timedelta(seconds=10), 10, 10),
            ConditionSpec(3, "第二", "第二", BASE_DATETIME + timedelta(seconds=15), 5, 5),
        )
        entry = self._entry(source, 0, 25)
        matches = tuple(
            ConditionCandidateMatch(
                ConditionTimeWindow(
                    BASE_TIMESTAMP + condition.recorded_at.second - condition.before_seconds,
                    BASE_TIMESTAMP + condition.recorded_at.second + condition.after_seconds,
                ),
                (source,),
            )
            for condition in conditions
        )
        plan = build_slice_execution_plan(
            matches,
            SourceChainBuildResult((BlfSourceChain((entry,), ()),)),
            conditions,
        )

        class LinearReferenceMatcher:
            def __init__(self, states):
                self.states = states

            def matching_states(self, timestamp):
                return _reference(self.states, timestamp)

        reference_output = self.root / "reference"
        candidate_output = self.root / "candidate"
        with patch("vet_data_modular.blf_slice_execution._WindowMatcher", LinearReferenceMatcher):
            reference = execute_slice(self._task(source, reference_output, conditions), plan, (entry,))
        candidate = execute_slice(self._task(source, candidate_output, conditions), plan, (entry,))

        def result_snapshot(result):
            return [
                (
                    item.status,
                    item.message_count,
                    item.ignored_remote_frames,
                    item.ignored_error_frames,
                    item.ignored_other_objects,
                    item.actual_first_timestamp,
                    item.actual_last_timestamp,
                    tuple(path.name for path in item.output_files),
                )
                for item in result.condition_results
            ]

        def message_snapshot(path):
            return [
                (
                    message.timestamp,
                    message.arbitration_id,
                    bytes(message.data),
                    message.channel,
                    message.is_fd,
                    message.bitrate_switch,
                    message.error_state_indicator,
                    message.is_remote_frame,
                    message.is_error_frame,
                )
                for message in can.BLFReader(path)
            ]

        self.assertEqual(result_snapshot(candidate), result_snapshot(reference))
        self.assertEqual(len(candidate.condition_results), len(reference.condition_results))
        for candidate_item, reference_item in zip(candidate.condition_results, reference.condition_results):
            self.assertEqual(len(candidate_item.output_files), len(reference_item.output_files))
            for candidate_path, reference_path in zip(candidate_item.output_files, reference_item.output_files):
                self.assertEqual(message_snapshot(candidate_path), message_snapshot(reference_path))

        candidate_report = build_report_markdown(candidate).replace(str(candidate_output), "<OUTPUT>")
        reference_report = build_report_markdown(reference).replace(str(reference_output), "<OUTPUT>")
        candidate_report = re.sub(r"^- 结束时间：.*$", "- 结束时间：<FINISHED>", candidate_report, flags=re.MULTILINE)
        reference_report = re.sub(r"^- 结束时间：.*$", "- 结束时间：<FINISHED>", reference_report, flags=re.MULTILINE)
        self.assertEqual(candidate_report, reference_report)


if __name__ == "__main__":
    unittest.main()
