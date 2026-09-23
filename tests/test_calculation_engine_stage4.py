import ast
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from vet_data_modular.calculated_signal import (
    CalculationStatus,
    CalculatedSignalDefinition,
)
from vet_data_modular.calculation_engine import (
    NEAR_ZERO_THRESHOLD,
    CalculationEngine,
)
from vet_data_modular.formula_validator import ValidatedFormula, parse_and_validate_formula
from vet_data_modular.signal_resolver import SignalResolver
from vet_data_modular.time_alignment import InterpolationPolicy


def _calculate(formula, signals, *, unit="", policies=None):
    bindings = {token: token for token in signals}
    validated = parse_and_validate_formula(formula, bindings)
    definition = CalculatedSignalDefinition(
        stable_id="calc-test",
        display_name="Calculated",
        user_formula=formula,
        normalized_formula=validated.normalized_formula,
        signal_tokens=bindings,
        dependencies=validated.dependencies,
        result_unit=unit,
    )
    catalog = {
        token: {"name": token, "display_name": token, "comment": "test"}
        for token in signals
    }
    math_data = {
        token: {"timestamps": np.asarray(timestamps), "samples": np.asarray(samples)}
        for token, (timestamps, samples) in signals.items()
    }
    resolver = SignalResolver(catalog, legacy_math_data=math_data)
    return CalculationEngine().calculate(definition, validated, resolver, policies)


class CalculationEngineTests(unittest.TestCase):
    def test_basic_binary_power_and_unary_operations(self):
        signals = {
            "S001": ([0, 1], [6, 8]),
            "S002": ([0, 1], [2, 4]),
        }
        cases = {
            "S001+S002": [8, 12], "S001-S002": [4, 4],
            "S001*S002": [12, 32], "S001/S002": [3, 2],
            "S001^2": [36, 64], "-S001": [-6, -8],
            "+S001": [6, 8], "S001*-2": [-12, -16],
        }
        for formula, expected in cases.items():
            with self.subTest(formula=formula):
                self.assert_success_samples(formula, signals, expected)

    def test_complex_four_signal_formula(self):
        signals = {
            "S001": ([0, 1], [10, 20]), "S002": ([0, 1], [2, 4]),
            "S003": ([0, 1], [1, 2]), "S004": ([0, 1], [11, 22]),
        }
        self.assert_success_samples("(S001+S002-S003)^2/S004", signals, [11, 22])

    def test_constants_broadcast_with_signal_arrays(self):
        signals = {"S001": ([0, 1], [2, 3])}
        cases = {
            "S001+5": [7, 8], "S001*9500": [19000, 28500],
            "S001/100": [0.02, 0.03], "2*S001": [4, 6],
            "2^S001": [4, 8], "(S001+5)*2": [14, 16],
        }
        for formula, expected in cases.items():
            with self.subTest(formula=formula):
                self.assert_success_samples(formula, signals, expected)

    def test_pure_constant_formula_is_rejected_without_fake_timeline(self):
        result = _calculate("2+3", {})
        self.assertEqual(result.status, CalculationStatus.ERROR)
        self.assertEqual(result.diagnostics["error_code"], "no_signal_dependencies")
        self.assertEqual(result.timestamps.size, 0)

    def test_sqrt_abs_and_nested_functions(self):
        signals = {"S001": ([0, 1, 2], [-4, 0, 9])}
        self.assert_success_samples("abs(S001)", signals, [4, 0, 9])
        self.assert_success_samples("sqrt(abs(S001))", signals, [2, 0, 3])
        self.assert_success_samples("sqrt(S001)", signals, [np.nan, 0, 3])

    def test_radian_trigonometric_functions(self):
        signals = {"S001": ([0, 1, 2], [0, np.pi / 4, np.pi])}
        self.assert_success_samples("sin(S001)", signals, np.sin(signals["S001"][1]))
        self.assert_success_samples("cos(S001)", signals, np.cos(signals["S001"][1]))
        self.assert_success_samples("tan(S001)", signals, np.tan(signals["S001"][1]))

    def test_degree_trigonometric_functions_and_singularity(self):
        signals = {"S001": ([0, 1, 2], [0, 90, 180])}
        self.assert_success_samples("sind(S001)", signals, [0, 1, 0], atol=1e-15)
        self.assert_success_samples("cosd(S001)", signals, [1, 0, -1], atol=1e-15)
        result = self.assert_success_samples("tand(S001)", signals, [0, np.nan, 0], atol=1e-15)
        self.assertEqual(result.diagnostics["trigonometric_singularity_count"], 1)

    def assert_success_samples(self, formula, signals, expected, atol=1e-7, **kwargs):
        result = _calculate(formula, signals, **kwargs)
        self.assertEqual(result.status, CalculationStatus.SUCCESS, result.error)
        np.testing.assert_allclose(result.samples, expected, equal_nan=True, atol=atol)
        return result

    def test_log_log10_and_exp(self):
        signals = {"S001": ([0, 1, 2], [1, 10, 100])}
        self.assert_success_samples("log(S001)", signals, np.log([1, 10, 100]))
        self.assert_success_samples("log10(S001)", signals, [0, 1, 2])
        self.assert_success_samples("exp(S001)", signals, np.exp([1, 10, 100]))

    def test_different_rates_ranges_and_nonuniform_timestamps_use_stage2_axis(self):
        signals = {
            "S001": ([0, 0.5, 2, 4], [0, 5, 20, 40]),
            "S002": ([1, 3], [10, 30]),
        }
        result = self.assert_success_samples("S001+S002", signals, [20, 40, 60])
        np.testing.assert_array_equal(result.timestamps, [1, 2, 3])
        self.assertEqual((result.diagnostics["effective_start"], result.diagnostics["effective_end"]), (1, 3))

    def test_three_signal_formula_aligns_all_dependencies_once(self):
        signals = {
            "S001": ([0, 2, 4], [0, 2, 4]),
            "S002": ([1, 3, 5], [10, 30, 50]),
            "S003": ([0.5, 2.5, 4.5], [5, 25, 45]),
        }
        with patch("vet_data_modular.calculation_engine.align_signals", wraps=__import__(
            "vet_data_modular.time_alignment", fromlist=["align_signals"]
        ).align_signals) as align:
            result = _calculate("(S001+S002)*S003", signals)
        self.assertEqual(result.status, CalculationStatus.SUCCESS)
        align.assert_called_once()
        self.assertEqual(len(align.call_args.args[0]), 3)

    def test_commutative_formula_order_has_identical_timeline(self):
        signals = {
            "S001": ([0, 1, 2], [0, 1, 2]),
            "S002": ([0, 0.5, 2], [0, 0.5, 2]),
        }
        first = _calculate("S001+S002", signals)
        second = _calculate("S002+S001", signals)
        np.testing.assert_array_equal(first.timestamps, second.timestamps)
        np.testing.assert_allclose(first.samples, second.samples)

    def test_explicit_previous_and_default_linear_policies(self):
        signals = {
            "S001": ([0, 2], [0, 2]),
            "S002": ([0, 1, 2], [0, 1, 2]),
        }
        linear = _calculate("S001+S002", signals)
        previous = _calculate(
            "S001+S002", signals,
            policies={"S001": InterpolationPolicy.PREVIOUS},
        )
        np.testing.assert_allclose(linear.samples, [0, 2, 4])
        np.testing.assert_allclose(previous.samples, [0, 1, 4])
        self.assertEqual(previous.diagnostics["interpolation_policies"]["S001"], "previous")

    def test_divide_by_zero_and_near_zero_use_central_threshold(self):
        self.assertEqual(NEAR_ZERO_THRESHOLD, 1e-7)
        signals = {
            "S001": ([0, 1, 2, 3], [1, 1, 1, 1]),
            "S002": ([0, 1, 2, 3], [0, 0.5e-7, 1e-7, 2]),
        }
        result = self.assert_success_samples("S001/S002", signals, [np.nan, np.nan, 1e7, 0.5])
        self.assertEqual(result.diagnostics["divide_by_zero_count"], 2)

    def test_domain_errors_and_overflow_become_nan_with_diagnostics(self):
        cases = [
            ("sqrt(S001)", [-1, 4], [np.nan, 2]),
            ("log(S001)", [0, -1], [np.nan, np.nan]),
            ("log10(S001)", [0, -10], [np.nan, np.nan]),
            ("S001^0.5", [-1, 4], [np.nan, 2]),
            ("exp(S001)", [1000, 0], [np.nan, 1]),
        ]
        for formula, samples, expected in cases:
            with self.subTest(formula=formula):
                result = self.assert_success_samples(formula, {"S001": ([0, 1], samples)}, expected)
                self.assertGreater(result.nan_count, 0)
                self.assertEqual(result.inf_count, 0)

    def test_input_nan_inf_and_final_counts_are_reported(self):
        result = self.assert_success_samples(
            "S001+1", {"S001": ([0, 1, 2], [1, np.nan, np.inf])},
            [2, np.nan, np.nan],
        )
        self.assertEqual(result.nan_count, 2)
        self.assertEqual(result.inf_count, 0)
        self.assertEqual(result.diagnostics["input_nan_count"], 1)
        self.assertEqual(result.diagnostics["input_inf_count"], 1)
        self.assertEqual(result.diagnostics["raw_result_inf_count"], 1)

    def test_result_preserves_unit_status_timeline_and_diagnostics(self):
        result = _calculate("S001+1", {"S001": ([2, 3], [4, 5])}, unit="kW")
        self.assertEqual(result.unit, "kW")
        self.assertEqual(result.status, CalculationStatus.SUCCESS)
        self.assertIsNone(result.error)
        np.testing.assert_array_equal(result.timestamps, [2, 3])
        self.assertEqual(result.diagnostics["output_sample_count"], 2)
        self.assertEqual(result.diagnostics["functions_used"], [])

    def test_missing_dependency_and_no_overlap_are_structured_errors(self):
        validated = parse_and_validate_formula("S001", {"S001": "missing"})
        definition = CalculatedSignalDefinition(
            "id", "name", "S001", signal_tokens={"S001": "missing"},
            dependencies=validated.dependencies,
        )
        missing = CalculationEngine().calculate(definition, validated, SignalResolver({}))
        self.assertEqual(missing.status, CalculationStatus.ERROR)
        self.assertIn("missing", missing.error)
        no_overlap = _calculate(
            "S001+S002",
            {"S001": ([0, 1], [0, 1]), "S002": ([2, 3], [2, 3])},
        )
        self.assertEqual(no_overlap.status, CalculationStatus.ERROR)
        self.assertEqual(no_overlap.diagnostics["error_code"], "no_overlap")

    def test_derivative_and_integral_are_explicitly_unsupported(self):
        for formula in ("derivative(S001)", "integral(S001)"):
            with self.subTest(formula=formula):
                result = _calculate(formula, {"S001": ([0, 1], [1, 2])})
                self.assertEqual(result.status, CalculationStatus.ERROR)
                self.assertEqual(result.diagnostics["error_code"], "unsupported_time_function")

    def test_defensive_invalid_ast_is_rejected_without_execution(self):
        tree = ast.parse("S001[0]", mode="eval")
        formula = ValidatedFormula("S001[0]", "S001[0]", tree, ("S001",), ("S001",), ())
        definition = CalculatedSignalDefinition(
            "id", "name", "S001[0]", signal_tokens={"S001": "S001"}, dependencies=("S001",)
        )
        resolver = SignalResolver(
            {"S001": {"name": "S001", "display_name": "S001"}},
            legacy_math_data={"S001": {"timestamps": [0], "samples": [1]}},
        )
        result = CalculationEngine().calculate(definition, formula, resolver)
        self.assertEqual(result.status, CalculationStatus.ERROR)
        self.assertEqual(result.diagnostics["error_code"], "unsupported_ast")

    def test_engine_is_gui_free_and_does_not_use_dynamic_execution(self):
        path = Path(__file__).parents[1] / "vet_data" / "vet_data_modular" / "calculation_engine.py"
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("PyQt", source)
        self.assertNotIn("QWidget", source)
        self.assertNotIn("eval(", source)
        self.assertNotIn("exec(", source)


if __name__ == "__main__":
    unittest.main()
