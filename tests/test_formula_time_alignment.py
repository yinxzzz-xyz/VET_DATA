import unittest
from pathlib import Path

import numpy as np
from asammdf import Signal

from vet_data_modular.signal_resolver import ResolvedSignal, SignalSource
from vet_data_modular.time_alignment import (
    AlignmentInput, InterpolationPolicy, TimeAlignmentError, align_signals,
)


def _input(key, timestamps, samples, policy=InterpolationPolicy.LINEAR):
    return AlignmentInput(key, np.asarray(timestamps), np.asarray(samples), policy)


class FormulaTimeAlignmentTests(unittest.TestCase):
    def assert_error(self, code, *inputs):
        with self.assertRaises(TimeAlignmentError) as caught:
            align_signals(inputs)
        self.assertEqual(caught.exception.code, code)

    def test_identical_timestamps_require_no_interpolation(self):
        result = align_signals([
            _input("A", [0, 1, 2], [1, 2, 3]),
            _input("B", [0, 1, 2], [4, 5, 6]),
        ])
        np.testing.assert_array_equal(result.timestamps, [0, 1, 2])
        np.testing.assert_array_equal(result.samples_by_key["A"], [1, 2, 3])
        self.assertFalse(result.diagnostics.signals["A"].interpolated)

    def test_different_sampling_rates_use_intersection_union(self):
        result = align_signals([
            _input("A", [0, 1, 2], [0, 10, 20]),
            _input("B", [0, 0.5, 1.5, 2], [0, 5, 15, 20]),
        ])
        np.testing.assert_array_equal(result.timestamps, [0, 0.5, 1, 1.5, 2])
        np.testing.assert_allclose(result.samples_by_key["A"], [0, 5, 10, 15, 20])
        self.assertTrue(result.diagnostics.signals["A"].interpolated)

    def test_three_signals_align_once_on_common_union(self):
        result = align_signals([
            _input("A", [0, 2, 4], [0, 2, 4]),
            _input("B", [1, 3, 5], [10, 30, 50]),
            _input("C", [0.5, 2.5, 4.5], [5, 25, 45]),
        ])
        np.testing.assert_array_equal(result.timestamps, [1, 2, 2.5, 3, 4])
        self.assertEqual(result.diagnostics.input_signal_count, 3)
        self.assertEqual((result.diagnostics.effective_start, result.diagnostics.effective_end), (1, 4))

    def test_different_start_times_clip_to_latest_start(self):
        result = align_signals([
            _input("A", [0, 1, 2, 3], [0, 1, 2, 3]),
            _input("B", [1, 2, 3], [10, 20, 30]),
        ])
        np.testing.assert_array_equal(result.timestamps, [1, 2, 3])
        self.assertEqual(result.diagnostics.effective_start, 1.0)

    def test_different_end_times_clip_to_earliest_end(self):
        result = align_signals([
            _input("A", [0, 1, 2], [0, 1, 2]),
            _input("B", [0, 1, 2, 3], [0, 10, 20, 30]),
        ])
        np.testing.assert_array_equal(result.timestamps, [0, 1, 2])
        self.assertEqual(result.diagnostics.effective_end, 2.0)

    def test_both_start_and_end_are_intersection_only(self):
        result = align_signals([
            _input("A", [0, 2, 4, 6], [0, 2, 4, 6]),
            _input("B", [1, 3, 5], [10, 30, 50]),
        ])
        np.testing.assert_array_equal(result.timestamps, [1, 2, 3, 4, 5])
        self.assertEqual((result.diagnostics.effective_start, result.diagnostics.effective_end), (1, 5))

    def test_no_intersection_fails_explicitly(self):
        self.assert_error("no_overlap", _input("A", [0, 1], [0, 1]), _input("B", [2, 3], [2, 3]))

    def test_single_point_intersection_and_single_point_signal(self):
        result = align_signals([
            _input("A", [0, 1], [0, 10]), _input("B", [1, 2], [20, 30]),
            _input("C", [1], [99]),
        ])
        np.testing.assert_array_equal(result.timestamps, [1])
        np.testing.assert_array_equal(result.samples_by_key["C"], [99])

    def test_nonuniform_timestamps_linear_interpolation(self):
        result = align_signals([
            _input("A", [0, 0.2, 1.7, 3.0], [0, 2, 17, 30]),
            _input("B", [0, 1, 3], [0, 10, 30]),
        ])
        np.testing.assert_array_equal(result.timestamps, [0, 0.2, 1, 1.7, 3])
        np.testing.assert_allclose(result.samples_by_key["B"], [0, 2, 10, 17, 30])

    def test_state_previous_never_creates_intermediate_states(self):
        result = align_signals([
            _input("state", [0, 2, 5], [0, 1, 2], InterpolationPolicy.PREVIOUS),
            _input("clock", [0, 1, 3, 4, 5], [0, 1, 3, 4, 5]),
        ])
        np.testing.assert_array_equal(result.timestamps, [0, 1, 2, 3, 4, 5])
        np.testing.assert_array_equal(result.samples_by_key["state"], [0, 0, 1, 1, 1, 2])

    def test_no_endpoint_hold_or_other_extrapolation_is_emitted(self):
        result = align_signals([
            _input("wide", [-1, 0, 1, 2, 3], [-1, 0, 1, 2, 3]),
            _input("narrow", [0, 1, 2], [10, 20, 30]),
        ])
        np.testing.assert_array_equal(result.timestamps, [0, 1, 2])

    def test_input_order_does_not_change_two_or_three_signal_timeline(self):
        inputs = [
            _input("A", [0, 1, 4], [0, 1, 4]),
            _input("B", [0, 2, 4], [0, 2, 4]),
            _input("C", [0, 3, 4], [0, 3, 4]),
        ]
        np.testing.assert_array_equal(
            align_signals(inputs[:2]).timestamps,
            align_signals([inputs[1], inputs[0]]).timestamps,
        )
        np.testing.assert_array_equal(
            align_signals(inputs).timestamps,
            align_signals([inputs[2], inputs[0], inputs[1]]).timestamps,
        )

    def test_empty_timestamps_and_samples_fail_separately(self):
        self.assert_error("empty_timestamps", _input("A", [], []))
        self.assert_error("empty_samples", _input("A", [0], []))

    def test_length_mismatch_fails(self):
        self.assert_error("length_mismatch", _input("A", [0, 1], [1]))

    def test_nan_and_infinite_timestamps_fail(self):
        self.assert_error("nonfinite_timestamp", _input("nan", [0, np.nan], [1, 2]))
        self.assert_error("nonfinite_timestamp", _input("inf", [0, np.inf], [1, 2]))
        self.assert_error("nonfinite_timestamp", _input("-inf", [-np.inf, 0], [1, 2]))

    def test_nonincreasing_and_duplicate_timestamps_fail_separately(self):
        self.assert_error("nonincreasing_timestamp", _input("down", [0, 2, 1], [1, 2, 3]))
        self.assert_error("duplicate_timestamp", _input("duplicate", [0, 1, 1], [1, 2, 3]))

    def test_nan_samples_remain_nonfinite_and_are_diagnosed(self):
        result = align_signals([
            _input("A", [0, 1, 2], [0, np.nan, 2]),
            _input("B", [0, 0.5, 1.5, 2], [0, 0.5, 1.5, 2]),
        ])
        self.assertTrue(np.isnan(result.samples_by_key["A"][[1, 2, 3]]).all())
        self.assertEqual(result.diagnostics.signals["A"].input_nan_count, 1)
        self.assertTrue(result.diagnostics.warnings)

    def test_inf_samples_remain_nonfinite_and_are_diagnosed(self):
        result = align_signals([
            _input("A", [0, 1, 2], [0, np.inf, 2]),
            _input("B", [0, 1, 2], [0, 1, 2]),
        ])
        self.assertTrue(np.isinf(result.samples_by_key["A"][1]))
        self.assertEqual(result.diagnostics.signals["A"].input_inf_count, 1)

    def test_floating_boundary_is_inside_intersection_without_clamping(self):
        boundary = 0.1 + 0.2
        result = align_signals([
            _input("A", [boundary, 0.5], [3, 5]),
            _input("B", [0.3, 0.4, 0.5], [3, 4, 5]),
        ])
        self.assertEqual(result.timestamps[0], boundary)
        self.assertTrue(np.all(result.timestamps >= result.diagnostics.effective_start))
        self.assertTrue(np.all(result.timestamps <= result.diagnostics.effective_end))

    def test_output_timestamps_are_strictly_increasing_and_unique(self):
        result = align_signals([
            _input("A", [0, 1, 3], [0, 1, 3]),
            _input("B", [0, 2, 3], [0, 2, 3]),
        ])
        self.assertTrue(np.all(np.diff(result.timestamps) > 0))
        self.assertEqual(result.timestamps.size, np.unique(result.timestamps).size)

    def test_inputs_and_resolved_signal_arrays_are_not_modified(self):
        timestamps = np.array([0.0, 1.0, 2.0])
        samples = np.array([0.0, 10.0, 20.0])
        timestamps_before = timestamps.copy()
        samples_before = samples.copy()
        resolved = ResolvedSignal(
            key="A", name="A", display_name="A",
            signal=Signal(samples=samples, timestamps=timestamps, name="A"),
            source=SignalSource.CSV,
        )
        resolved_timestamps_before = resolved.timestamps.copy()
        resolved_samples_before = resolved.samples.copy()
        align_signals([
            AlignmentInput.from_resolved(resolved),
            _input("B", [0, 0.5, 2], [0, 5, 20]),
        ])
        np.testing.assert_array_equal(timestamps, timestamps_before)
        np.testing.assert_array_equal(samples, samples_before)
        np.testing.assert_array_equal(resolved.timestamps, resolved_timestamps_before)
        np.testing.assert_array_equal(resolved.samples, resolved_samples_before)

    def test_service_module_has_no_gui_dependency(self):
        path = Path(__file__).parents[1] / "vet_data" / "vet_data_modular" / "time_alignment.py"
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("PyQt", source)
        self.assertNotIn("QWidget", source)


if __name__ == "__main__":
    unittest.main()
