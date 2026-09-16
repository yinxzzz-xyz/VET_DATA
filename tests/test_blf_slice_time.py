import unittest
from datetime import datetime, timedelta, timezone

from vet_data_modular.blf_slice_models import ConditionSpec, DEFAULT_UTC_OFFSET
from vet_data_modular.blf_slice_time import (
    ConditionTimeWindow,
    blf_timestamp_to_utc_timestamp,
    build_condition_time_window,
    build_time_window,
    table_datetime_to_utc_timestamp,
    timestamp_in_window,
    validate_fixed_utc_offset,
)


class FixedUtcOffsetTests(unittest.TestCase):
    def test_default_utc_plus_eight_converts_to_utc_basis(self):
        actual = table_datetime_to_utc_timestamp(
            datetime(2026, 6, 29, 8, 0), DEFAULT_UTC_OFFSET
        )
        expected = datetime(2026, 6, 29, tzinfo=timezone.utc).timestamp()
        self.assertEqual(actual, expected)

    def test_different_offsets_can_represent_the_same_instant(self):
        plus_eight = table_datetime_to_utc_timestamp(
            datetime(2026, 6, 29, 8, 0), timedelta(hours=8)
        )
        minus_five = table_datetime_to_utc_timestamp(
            datetime(2026, 6, 28, 19, 0), timedelta(hours=-5)
        )
        self.assertEqual(plus_eight, minus_five)

    def test_positive_negative_and_fractional_offsets(self):
        utc = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp()
        cases = (
            (timedelta(hours=5, minutes=15), datetime(2026, 1, 2, 8, 19, 5)),
            (timedelta(hours=-3, minutes=-30), datetime(2026, 1, 1, 23, 34, 5)),
            (timedelta(hours=9, minutes=45), datetime(2026, 1, 2, 12, 49, 5)),
        )
        for offset, wall_time in cases:
            with self.subTest(offset=offset):
                self.assertEqual(table_datetime_to_utc_timestamp(wall_time, offset), utc)

    def test_offset_range_boundaries_are_accepted(self):
        self.assertEqual(validate_fixed_utc_offset(timedelta(hours=-12)), timedelta(hours=-12))
        self.assertEqual(validate_fixed_utc_offset(timedelta(hours=14)), timedelta(hours=14))

    def test_out_of_range_and_non_quarter_hour_offsets_are_rejected(self):
        invalid = (
            timedelta(hours=-12, minutes=-15),
            timedelta(hours=14, minutes=15),
            timedelta(minutes=1),
            timedelta(minutes=30, seconds=1),
        )
        for offset in invalid:
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                validate_fixed_utc_offset(offset)

    def test_aware_table_datetime_is_rejected_to_prevent_double_offset(self):
        with self.assertRaisesRegex(ValueError, "must be naive"):
            table_datetime_to_utc_timestamp(
                datetime(2026, 1, 1, tzinfo=timezone.utc), timedelta(hours=8)
            )

    def test_python_can_blf_wall_clock_offset_is_applied_once(self):
        python_can_value = datetime(2026, 6, 29, 8, 0, tzinfo=timezone.utc).timestamp()
        normalized = blf_timestamp_to_utc_timestamp(python_can_value, timedelta(hours=8))
        expected = datetime(2026, 6, 29, tzinfo=timezone.utc).timestamp()
        self.assertEqual(normalized, expected)
        self.assertEqual(
            normalized,
            table_datetime_to_utc_timestamp(
                datetime(2026, 6, 29, 8, 0), timedelta(hours=8)
            ),
        )


class ConditionTimeWindowTests(unittest.TestCase):
    def test_before_zero_and_after_positive(self):
        center = table_datetime_to_utc_timestamp(datetime(2026, 1, 1, 8), timedelta(hours=8))
        window = build_time_window(datetime(2026, 1, 1, 8), 0, 20, timedelta(hours=8))
        self.assertEqual((window.start, window.end), (center, center + 20))

    def test_before_positive_and_after_zero(self):
        center = table_datetime_to_utc_timestamp(datetime(2026, 1, 1, 8), timedelta(hours=8))
        window = build_time_window(datetime(2026, 1, 1, 8), 20, 0, timedelta(hours=8))
        self.assertEqual((window.start, window.end), (center - 20, center))

    def test_closed_boundaries_hit_and_tiny_outside_differences_miss(self):
        window = ConditionTimeWindow(100.0, 200.0)
        self.assertTrue(timestamp_in_window(100.0, window))
        self.assertTrue(timestamp_in_window(200.0, window))
        self.assertFalse(timestamp_in_window(100.0 - 0.000001, window))
        self.assertFalse(timestamp_in_window(200.0 + 0.000001, window))

    def test_builds_window_from_stage_one_condition_without_mutating_it(self):
        condition = ConditionSpec(
            original_row_number=2,
            original_name="工况",
            name="工况",
            recorded_at=datetime(2026, 1, 1, 8),
            before_seconds=10,
            after_seconds=15,
        )
        window = build_condition_time_window(condition, timedelta(hours=8))
        center = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        self.assertEqual((window.start, window.end), (center - 10, center + 15))
        self.assertEqual(condition.recorded_at, datetime(2026, 1, 1, 8))


if __name__ == "__main__":
    unittest.main()
