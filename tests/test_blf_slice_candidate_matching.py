import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from vet_data_modular.blf_slice_models import (
    BlfHeaderStatus,
    BlfIndexEntry,
    BlfTimeRangeStatus,
)
from vet_data_modular.blf_slice_service import select_blf_candidates
from vet_data_modular.blf_slice_time import ConditionTimeWindow


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()


def _message(timestamp):
    return SimpleNamespace(
        timestamp=timestamp,
        is_remote_frame=False,
        is_error_frame=False,
        is_fd=False,
    )


class FakeReader:
    def __init__(self, items):
        self.items = tuple(items)

    def __iter__(self):
        for item in self.items:
            if isinstance(item, Exception):
                raise item
            yield item

    def stop(self):
        pass


class ConservativeCandidateSelectionTests(unittest.TestCase):
    def test_confirmed_ranges_match_closed_boundaries_without_scanning(self):
        entries = [
            BlfIndexEntry(
                path="confirmed.blf",
                time_range_status=BlfTimeRangeStatus.CONFIRMED,
                effective_start_timestamp=BASE + 10,
                effective_stop_timestamp=BASE + 20,
            )
        ]

        def unexpected_reader(path):
            raise AssertionError("confirmed range must not be scanned")

        result = select_blf_candidates(
            entries,
            [
                ConditionTimeWindow(BASE + 10, BASE + 10),
                ConditionTimeWindow(BASE + 20, BASE + 20),
                ConditionTimeWindow(BASE + 20.001, BASE + 21),
            ],
            timedelta(0),
            reader_factory=unexpected_reader,
        )

        self.assertEqual(
            [match.candidate_paths for match in result.matches],
            [
                (Path("confirmed.blf"),),
                (Path("confirmed.blf"),),
                (),
            ],
        )
        self.assertEqual(result.range_confirmation_paths, ())
        self.assertTrue(result.is_complete)

    def test_trusted_header_start_can_safely_exclude_without_scanning(self):
        entry = BlfIndexEntry(
            path="future.blf",
            header_status=BlfHeaderStatus.VALID,
            header_start_timestamp=BASE + 100,
            header_stop_timestamp=BASE + 200,
        )

        result = select_blf_candidates(
            [entry],
            [ConditionTimeWindow(BASE, BASE + 99.999)],
            timedelta(0),
            reader_factory=lambda path: (_ for _ in ()).throw(
                AssertionError("safely excluded file must not be scanned")
            ),
        )

        self.assertEqual(result.matches[0].candidate_paths, ())
        self.assertEqual(result.matches[0].unresolved_paths, ())
        self.assertEqual(result.range_confirmation_paths, ())

    def test_header_stop_is_not_used_to_exclude_a_real_tail_candidate(self):
        entry = BlfIndexEntry(
            path="misleading-name.99.blf",
            header_status=BlfHeaderStatus.VALID,
            header_start_timestamp=BASE,
            header_stop_timestamp=BASE + 50,
        )
        calls = []

        def factory(path):
            calls.append(path)
            return FakeReader([_message(BASE + 60)])

        result = select_blf_candidates(
            [entry],
            [ConditionTimeWindow(BASE + 60, BASE + 60)],
            timedelta(0),
            reader_factory=factory,
        )

        self.assertEqual(calls, [Path("misleading-name.99.blf")])
        self.assertEqual(
            result.matches[0].candidate_paths,
            (Path("misleading-name.99.blf"),),
        )
        self.assertTrue(result.is_complete)

    def test_multiple_windows_build_one_scan_set_and_scan_each_file_once(self):
        entry = BlfIndexEntry(
            path="shared.blf",
            header_status=BlfHeaderStatus.VALID,
            header_start_timestamp=BASE,
        )
        calls = 0

        def factory(path):
            nonlocal calls
            calls += 1
            return FakeReader([_message(BASE + 10), _message(BASE + 20)])

        result = select_blf_candidates(
            [entry],
            [
                ConditionTimeWindow(BASE + 10, BASE + 11),
                ConditionTimeWindow(BASE + 19, BASE + 20),
            ],
            timedelta(0),
            reader_factory=factory,
        )

        self.assertEqual(calls, 1)
        self.assertEqual(
            result.range_confirmation_paths,
            (Path("shared.blf"),),
        )
        self.assertEqual(
            [match.candidate_paths for match in result.matches],
            [(Path("shared.blf"),), (Path("shared.blf"),)],
        )

    def test_shared_recording_header_start_uses_effective_ranges_for_final_match(self):
        names = ("capture.3.blf", "capture.4.blf", "capture.5.blf")
        ranges = (
            (BASE + 10, BASE + 20),
            (BASE + 20.001, BASE + 30),
            (BASE + 30.001, BASE + 40),
        )
        entries = [
            BlfIndexEntry(
                path=name,
                header_status=BlfHeaderStatus.VALID,
                header_start_timestamp=BASE,
                header_stop_timestamp=stop - 0.5,
            )
            for name, (_, stop) in zip(names, ranges)
        ]
        calls = {name: 0 for name in names}

        def factory(path):
            calls[path.name] += 1
            index = names.index(path.name)
            start, stop = ranges[index]
            return FakeReader([_message(start), _message(stop)])

        result = select_blf_candidates(
            entries,
            [
                ConditionTimeWindow(BASE + 15, BASE + 15),
                ConditionTimeWindow(BASE + 25, BASE + 25),
                ConditionTimeWindow(BASE + 35, BASE + 35),
            ],
            timedelta(0),
            reader_factory=factory,
        )

        self.assertEqual(calls, {name: 1 for name in names})
        self.assertEqual(
            [match.candidate_paths for match in result.matches],
            [
                (Path("capture.3.blf"),),
                (Path("capture.4.blf"),),
                (Path("capture.5.blf"),),
            ],
        )
        self.assertTrue(result.is_complete)

    def test_scan_failure_is_reported_as_unresolved_not_silent_no_match(self):
        entry = BlfIndexEntry(
            path="broken.blf",
            header_status=BlfHeaderStatus.UNTRUSTED,
        )
        result = select_blf_candidates(
            [entry],
            [ConditionTimeWindow(BASE, BASE + 1)],
            timedelta(0),
            reader_factory=lambda path: FakeReader([ValueError("broken")]),
        )

        self.assertEqual(result.matches[0].candidate_paths, ())
        self.assertEqual(
            result.matches[0].unresolved_paths,
            (Path("broken.blf"),),
        )
        self.assertFalse(result.matches[0].is_complete)
        self.assertFalse(result.is_complete)

    def test_scan_failure_remains_unresolved_for_every_window(self):
        entry = BlfIndexEntry(
            path="broken-valid-header.blf",
            header_status=BlfHeaderStatus.VALID,
            header_start_timestamp=BASE,
            header_stop_timestamp=BASE + 100,
        )
        result = select_blf_candidates(
            [entry],
            [
                ConditionTimeWindow(BASE - 10, BASE - 1),
                ConditionTimeWindow(BASE + 1, BASE + 2),
            ],
            timedelta(0),
            reader_factory=lambda path: FakeReader([ValueError("broken")]),
        )

        self.assertEqual(
            [match.unresolved_paths for match in result.matches],
            [
                (Path("broken-valid-header.blf"),),
                (Path("broken-valid-header.blf"),),
            ],
        )
        self.assertFalse(result.is_complete)

    def test_known_empty_file_is_complete_no_match_without_rescan(self):
        entry = BlfIndexEntry(
            path="empty.blf",
            time_range_status=BlfTimeRangeStatus.EMPTY,
        )
        result = select_blf_candidates(
            [entry],
            [ConditionTimeWindow(BASE, BASE + 1)],
            timedelta(0),
            reader_factory=lambda path: (_ for _ in ()).throw(
                AssertionError("known empty file must not be rescanned")
            ),
        )
        self.assertTrue(result.is_complete)
        self.assertEqual(result.matches[0].candidate_paths, ())

    def test_duplicate_paths_are_rejected_before_scanning(self):
        with self.assertRaisesRegex(ValueError, "duplicate BLF path"):
            select_blf_candidates(
                [BlfIndexEntry(path="same.blf"), BlfIndexEntry(path="same.blf")],
                [ConditionTimeWindow(BASE, BASE + 1)],
                timedelta(0),
            )


if __name__ == "__main__":
    unittest.main()
