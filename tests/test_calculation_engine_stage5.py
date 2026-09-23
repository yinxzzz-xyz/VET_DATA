import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from vet_data_modular.calculated_signal import CalculationStatus, CalculatedSignalDefinition
from vet_data_modular.calculation_engine import CalculationEngine
from vet_data_modular.formula_validator import parse_and_validate_formula
from vet_data_modular.signal_resolver import SignalResolver


def _calculate(formula, signals, *, unit=""):
    bindings = {token: token for token in signals}
    validated = parse_and_validate_formula(formula, bindings)
    definition = CalculatedSignalDefinition(
        stable_id="stage5", display_name="Stage 5", user_formula=formula,
        normalized_formula=validated.normalized_formula,
        signal_tokens=bindings, dependencies=validated.dependencies, result_unit=unit,
    )
    catalog = {key: {"name": key, "display_name": key} for key in signals}
    data = {
        key: {"timestamps": np.asarray(timestamps), "samples": np.asarray(samples)}
        for key, (timestamps, samples) in signals.items()
    }
    return CalculationEngine().calculate(
        definition, validated, SignalResolver(catalog, legacy_math_data=data)
    )


class CalculationEngineStage5Tests(unittest.TestCase):
    def assert_samples(self, formula, signals, expected, *, atol=1e-12):
        result = _calculate(formula, signals)
        self.assertEqual(result.status, CalculationStatus.SUCCESS, result.error)
        np.testing.assert_allclose(result.samples, expected, atol=atol, equal_nan=True)
        return result

    def test_derivative_linear_equal_and_nonuniform_timestamps(self):
        for timestamps in ([0, 1, 2, 3], [0, 0.25, 1.5, 4]):
            with self.subTest(timestamps=timestamps):
                result = self.assert_samples(
                    "derivative(S001)", {"S001": (timestamps, timestamps)},
                    np.ones(len(timestamps)),
                )
                np.testing.assert_array_equal(result.timestamps, timestamps)

    def test_derivative_quadratic_has_explicit_first_order_boundaries(self):
        timestamps = np.array([0.0, 0.5, 2.0, 5.0])
        expected = np.gradient(timestamps ** 2, timestamps, edge_order=1)
        result = self.assert_samples(
            "derivative(S001)", {"S001": (timestamps, timestamps ** 2)}, expected
        )
        np.testing.assert_allclose(result.samples[1:-1], 2 * timestamps[1:-1])

    def test_derivative_expression_combinations(self):
        timestamps = np.array([0.0, 0.5, 2.0, 4.0])
        signals = {"S001": (timestamps, timestamps * 3), "S002": (timestamps, timestamps)}
        cases = {
            "derivative(S001-S002)": np.full(4, 2.0),
            "derivative(S001*2)": np.full(4, 6.0),
            "abs(derivative(S001))": np.full(4, 3.0),
            "derivative(S001)+S002": 3 + timestamps,
            "derivative(S001+S002)*2": np.full(4, 8.0),
            "sqrt(abs(derivative(S001)))": np.full(4, np.sqrt(3)),
        }
        for formula, expected in cases.items():
            with self.subTest(formula=formula):
                self.assert_samples(formula, signals, expected)

    def test_integral_constant_equal_and_nonuniform_timestamps(self):
        for timestamps in ([0, 1, 2, 3], [1, 1.25, 2.5, 5]):
            with self.subTest(timestamps=timestamps):
                self.assert_samples(
                    "integral(S001)", {"S001": (timestamps, np.ones(len(timestamps)))},
                    np.asarray(timestamps) - timestamps[0],
                )

    def test_integral_linear_matches_analytic_result(self):
        timestamps = np.array([1.0, 1.5, 3.0, 6.0])
        result = self.assert_samples(
            "integral(S001)", {"S001": (timestamps, timestamps)},
            0.5 * (timestamps ** 2 - timestamps[0] ** 2),
        )
        self.assertEqual(result.samples[0], 0.0)

    def test_integral_expression_combinations(self):
        timestamps = np.array([0.0, 0.5, 2.0, 4.0])
        signals = {
            "S001": (timestamps, np.full(4, 3.0)),
            "S002": (timestamps, np.full(4, 1.0)),
        }
        elapsed = timestamps - timestamps[0]
        cases = {
            "integral(S001-S002)": 2 * elapsed,
            "integral(abs(S001))": 3 * elapsed,
            "integral(S001)*2": 6 * elapsed,
            "integral(S001-S002)/10": 0.2 * elapsed,
            "integral(abs(S001-S002))": 2 * elapsed,
        }
        for formula, expected in cases.items():
            with self.subTest(formula=formula):
                self.assert_samples(formula, signals, expected)

    def test_nested_time_functions_reuse_the_same_timeline(self):
        timestamps = np.array([0.0, 0.5, 1.5, 3.0])
        signals = {"S001": (timestamps, np.full(4, 2.0))}
        self.assert_samples("derivative(integral(S001))", signals, np.full(4, 2.0))
        self.assert_samples("integral(derivative(S001))", signals, np.zeros(4))

    def test_multisignal_time_formula_aligns_once_and_preserves_axis(self):
        signals = {
            "S001": ([0, 1, 3], [0, 1, 3]), "S002": ([0, 2, 3], [0, 2, 3]),
        }
        with patch("vet_data_modular.calculation_engine.align_signals", wraps=__import__(
            "vet_data_modular.time_alignment", fromlist=["align_signals"]
        ).align_signals) as align:
            result = _calculate("derivative(S001+S002)*2", signals)
        self.assertEqual(result.status, CalculationStatus.SUCCESS, result.error)
        align.assert_called_once()
        np.testing.assert_array_equal(result.timestamps, [0, 1, 2, 3])
        np.testing.assert_allclose(result.samples, [4, 4, 4, 4])

    def test_nonfinite_inputs_propagate_and_are_diagnosed(self):
        for value, input_key in ((np.nan, "input_nan_count"), (np.inf, "input_inf_count")):
            with self.subTest(value=value):
                signals = {"S001": ([0, 1, 2], [1, value, 3])}
                for formula in ("derivative(S001)", "integral(S001)"):
                    result = _calculate(formula, signals)
                    self.assertEqual(result.status, CalculationStatus.SUCCESS, result.error)
                    self.assertGreater(result.nan_count, 0)
                    self.assertEqual(result.diagnostics[input_key], 1)

    def test_short_time_axes_have_deterministic_rules(self):
        single = {"S001": ([5], [7])}
        derivative = _calculate("derivative(S001)", single)
        self.assertEqual(derivative.status, CalculationStatus.ERROR)
        self.assertEqual(derivative.diagnostics["error_code"], "insufficient_derivative_samples")
        integral = self.assert_samples("integral(S001)", single, [0])
        np.testing.assert_array_equal(integral.timestamps, [5])
        self.assert_samples("derivative(S001)", {"S001": ([1, 4], [2, 11])}, [3, 3])

    def test_unit_safety_and_gui_boundary_remain_unchanged(self):
        result = _calculate("derivative(S001)", {"S001": ([0, 1], [1, 2])}, unit="rpm/s")
        self.assertEqual(result.unit, "rpm/s")
        path = Path(__file__).parents[1] / "vet_data" / "vet_data_modular" / "calculation_engine.py"
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("PyQt", source)
        self.assertNotIn("eval(", source)
        self.assertNotIn("exec(", source)


if __name__ == "__main__":
    unittest.main()