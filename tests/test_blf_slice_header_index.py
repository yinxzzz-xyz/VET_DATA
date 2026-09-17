import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vet_data_modular.blf_slice_models import BlfHeaderStatus, BlfTimeRangeStatus
from vet_data_modular.blf_slice_service import build_blf_header_index


def _wall_timestamp(year, month=1, day=1, hour=8):
    return datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp()


class FakeReader:
    def __init__(self, start, stop):
        self.start_timestamp = start
        self.stop_timestamp = stop
        self.iterated = False
        self.stopped = False

    def __iter__(self):
        self.iterated = True
        raise AssertionError("normal Header indexing must not iterate messages")

    def stop(self):
        self.stopped = True


class BlfHeaderIndexTests(unittest.TestCase):
    def test_valid_header_is_normalized_once_without_iteration(self):
        reader = FakeReader(_wall_timestamp(2026), _wall_timestamp(2026) + 60)
        result = build_blf_header_index(
            [Path("valid.blf")],
            timedelta(hours=8),
            reader_factory=lambda path: reader,
        )

        self.assertEqual(len(result), 1)
        entry = result[0]
        self.assertEqual(entry.header_status, BlfHeaderStatus.VALID)
        self.assertEqual(entry.header_start_timestamp_raw, _wall_timestamp(2026))
        self.assertEqual(entry.header_stop_timestamp_raw, _wall_timestamp(2026) + 60)
        self.assertEqual(entry.header_start_timestamp, _wall_timestamp(2026) - 8 * 3600)
        self.assertEqual(
            entry.header_stop_timestamp,
            _wall_timestamp(2026) + 60 - 8 * 3600,
        )
        self.assertEqual(entry.header_untrusted_reasons, ())
        self.assertEqual(entry.time_range_status, BlfTimeRangeStatus.UNCONFIRMED)
        self.assertIsNone(entry.effective_start_timestamp)
        self.assertIsNone(entry.effective_stop_timestamp)
        self.assertFalse(reader.iterated)
        self.assertTrue(reader.stopped)

    def test_missing_nonpositive_nonfinite_reversed_and_year_rules(self):
        cases = (
            (None, _wall_timestamp(2026), "start_timestamp_missing"),
            (_wall_timestamp(2026), None, "stop_timestamp_missing"),
            (0, _wall_timestamp(2026), "start_timestamp_not_positive"),
            (_wall_timestamp(2026), 0, "stop_timestamp_not_positive"),
            (float("nan"), _wall_timestamp(2026), "start_timestamp_not_finite"),
            (_wall_timestamp(2026), float("inf"), "stop_timestamp_not_finite"),
            (
                _wall_timestamp(2026) + 1,
                _wall_timestamp(2026),
                "stop_timestamp_before_start_timestamp",
            ),
            (
                _wall_timestamp(1999),
                _wall_timestamp(2026),
                "start_timestamp_year_out_of_range",
            ),
            (
                _wall_timestamp(2026),
                _wall_timestamp(2101),
                "stop_timestamp_year_out_of_range",
            ),
        )
        for start, stop, reason in cases:
            with self.subTest(reason=reason):
                entry = build_blf_header_index(
                    [Path("sample.blf")],
                    timedelta(0),
                    reader_factory=lambda path, s=start, e=stop: FakeReader(s, e),
                )[0]
                self.assertEqual(entry.header_status, BlfHeaderStatus.UNTRUSTED)
                self.assertIn(reason, entry.header_untrusted_reasons)

    def test_reader_failure_is_isolated_and_later_files_continue(self):
        good_reader = FakeReader(_wall_timestamp(2026), _wall_timestamp(2026) + 1)

        def factory(path):
            if path.name == "broken.blf":
                raise ValueError("bad header")
            return good_reader

        entries = build_blf_header_index(
            [Path("broken.blf"), Path("good.blf")],
            timedelta(0),
            reader_factory=factory,
        )

        self.assertEqual(entries[0].header_status, BlfHeaderStatus.READ_FAILED)
        self.assertEqual(
            entries[0].header_untrusted_reasons,
            ("reader_or_header_error",),
        )
        self.assertIn("ValueError: bad header", entries[0].error)
        self.assertEqual(entries[1].header_status, BlfHeaderStatus.VALID)

    def test_boolean_header_value_is_not_accepted_as_a_number(self):
        entry = build_blf_header_index(
            [Path("sample.blf")],
            timedelta(0),
            reader_factory=lambda path: FakeReader(True, _wall_timestamp(2026)),
        )[0]
        self.assertEqual(entry.header_status, BlfHeaderStatus.UNTRUSTED)
        self.assertIn("start_timestamp_not_numeric", entry.header_untrusted_reasons)


if __name__ == "__main__":
    unittest.main()
