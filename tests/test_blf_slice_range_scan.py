import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from threading import Event

from vet_data_modular.blf_slice_models import (
    BlfHeaderStatus,
    BlfIndexEntry,
    BlfTimeRangeStatus,
)
from vet_data_modular.blf_slice_service import confirm_blf_effective_ranges


def _timestamp(year=2026, hour=8):
    return datetime(year, 1, 1, hour, tzinfo=timezone.utc).timestamp()


def _message(
    timestamp,
    *,
    remote=False,
    error=False,
    is_fd=False,
):
    return SimpleNamespace(
        timestamp=timestamp,
        is_remote_frame=remote,
        is_error_frame=error,
        is_fd=is_fd,
    )


class FakeReader:
    def __init__(self, items):
        self.items = tuple(items)
        self.stopped = False

    def __iter__(self):
        for item in self.items:
            if isinstance(item, Exception):
                raise item
            yield item

    def stop(self):
        self.stopped = True


class EffectiveRangeScanTests(unittest.TestCase):
    def test_stream_scan_confirms_range_and_keeps_header_evidence_separate(self):
        base = _timestamp()
        entry = BlfIndexEntry(
            path="volume.blf",
            header_status=BlfHeaderStatus.VALID,
            header_start_timestamp_raw=base - 100,
            header_stop_timestamp_raw=base + 9,
            header_start_timestamp=base - 100 - 8 * 3600,
            header_stop_timestamp=base + 9 - 8 * 3600,
        )
        reader = FakeReader(
            [
                _message(base - 10, remote=True),
                _message(base, is_fd=False),
                _message(base + 5, is_fd=True),
                _message(base + 10),
                _message(base + 11, error=True),
            ]
        )

        result = confirm_blf_effective_ranges(
            [entry],
            timedelta(hours=8),
            reader_factory=lambda path: reader,
        )[0]

        self.assertEqual(result.header_status, BlfHeaderStatus.VALID)
        self.assertEqual(result.header_start_timestamp_raw, base - 100)
        self.assertEqual(result.header_stop_timestamp_raw, base + 9)
        self.assertEqual(result.time_range_status, BlfTimeRangeStatus.CONFIRMED)
        self.assertEqual(result.effective_start_timestamp, base - 8 * 3600)
        self.assertEqual(result.effective_stop_timestamp, base + 10 - 8 * 3600)
        self.assertEqual(result.scanned_message_count, 5)
        self.assertEqual(result.valid_frame_count, 3)
        self.assertEqual(result.ignored_remote_frames, 1)
        self.assertEqual(result.ignored_error_frames, 1)
        self.assertTrue(reader.stopped)

    def test_invalid_timestamps_are_skipped_and_reported(self):
        base = _timestamp()
        entry = BlfIndexEntry(path="mixed.blf")
        result = confirm_blf_effective_ranges(
            [entry],
            timedelta(0),
            reader_factory=lambda path: FakeReader(
                [
                    _message(float("nan")),
                    _message(_timestamp(1999)),
                    _message(base),
                    _message(base + 2),
                    _message(base + 1),
                    _message(base + 3),
                ]
            ),
        )[0]

        self.assertEqual(result.time_range_status, BlfTimeRangeStatus.CONFIRMED)
        self.assertEqual(
            (result.effective_start_timestamp, result.effective_stop_timestamp),
            (base, base + 3),
        )
        self.assertEqual(result.invalid_timestamp_count, 2)
        self.assertEqual(result.timestamp_inversion_count, 1)
        self.assertIn("ignored_invalid_timestamps:2", result.warnings)
        self.assertIn("timestamp_inversions:1", result.warnings)

    def test_no_ordinary_frames_is_empty(self):
        result = confirm_blf_effective_ranges(
            [BlfIndexEntry(path="empty.blf")],
            timedelta(0),
            reader_factory=lambda path: FakeReader(
                [
                    _message(_timestamp(), remote=True),
                    _message(_timestamp(), error=True),
                ]
            ),
        )[0]

        self.assertEqual(result.time_range_status, BlfTimeRangeStatus.EMPTY)
        self.assertIsNone(result.effective_start_timestamp)
        self.assertEqual(result.valid_frame_count, 0)

    def test_only_invalid_timestamps_is_failed_not_empty(self):
        result = confirm_blf_effective_ranges(
            [BlfIndexEntry(path="invalid.blf")],
            timedelta(0),
            reader_factory=lambda path: FakeReader([_message(None)]),
        )[0]

        self.assertEqual(result.time_range_status, BlfTimeRangeStatus.FAILED)
        self.assertEqual(result.invalid_timestamp_count, 1)
        self.assertIn("no valid data-frame timestamps", result.error)

    def test_read_failure_is_isolated_and_next_file_is_scanned(self):
        base = _timestamp()

        def factory(path):
            if path.name == "broken.blf":
                return FakeReader([_message(base), ValueError("damaged container")])
            return FakeReader([_message(base + 10)])

        broken, good = confirm_blf_effective_ranges(
            [BlfIndexEntry(path="broken.blf"), BlfIndexEntry(path="good.blf")],
            timedelta(0),
            reader_factory=factory,
        )

        self.assertEqual(broken.time_range_status, BlfTimeRangeStatus.FAILED)
        self.assertIn("ValueError: damaged container", broken.error)
        self.assertEqual(good.time_range_status, BlfTimeRangeStatus.CONFIRMED)
        self.assertEqual(good.effective_start_timestamp, base + 10)

    def test_cancel_stops_current_scan_and_does_not_open_later_files(self):
        base = _timestamp()
        token = Event()
        opened = []
        progress = []

        def factory(path):
            opened.append(path.name)
            return FakeReader([_message(base), _message(base + 1), _message(base + 2)])

        def on_progress(update):
            progress.append(update)
            if update.scanned_message_count >= 2:
                token.set()

        first, second = confirm_blf_effective_ranges(
            [BlfIndexEntry(path="first.blf"), BlfIndexEntry(path="second.blf")],
            timedelta(0),
            reader_factory=factory,
            cancel_event=token,
            progress_callback=on_progress,
            progress_interval=2,
        )

        self.assertEqual(first.time_range_status, BlfTimeRangeStatus.CANCELLED)
        self.assertEqual(second.time_range_status, BlfTimeRangeStatus.CANCELLED)
        self.assertEqual(opened, ["first.blf"])
        self.assertGreaterEqual(len(progress), 1)

    def test_progress_and_valid_frame_callback_support_single_pass_reuse(self):
        base = _timestamp()
        progress = []
        consumed = []

        result = confirm_blf_effective_ranges(
            [BlfIndexEntry(path="reuse.blf")],
            timedelta(hours=8),
            reader_factory=lambda path: FakeReader(
                [_message(base), _message(base + 1, is_fd=True)]
            ),
            progress_callback=progress.append,
            valid_frame_callback=lambda path, message, timestamp: consumed.append(
                (path.name, message.is_fd, timestamp)
            ),
            progress_interval=1,
        )[0]

        self.assertEqual(result.time_range_status, BlfTimeRangeStatus.CONFIRMED)
        self.assertEqual(
            consumed,
            [
                ("reuse.blf", False, base - 8 * 3600),
                ("reuse.blf", True, base + 1 - 8 * 3600),
            ],
        )
        self.assertEqual(progress[-1].scanned_message_count, 2)
        self.assertEqual(progress[-1].valid_frame_count, 2)

    def test_invalid_progress_interval_is_rejected_before_opening_files(self):
        with self.assertRaises(ValueError):
            confirm_blf_effective_ranges(
                [BlfIndexEntry(path="sample.blf")],
                timedelta(0),
                progress_interval=0,
            )


if __name__ == "__main__":
    unittest.main()
