import unittest
from pathlib import Path

import numpy as np

from vet_data_modular.calculated_signal import (
    CALCULATED_SIGNAL_SCHEMA_VERSION, CalculatedSignalDefinition,
)
from vet_data_modular.calculated_signal_config import (
    CalculatedSignalConfigError, CalculatedSignalRestoreService,
    load_calculated_signal_config, migrate_legacy_definition,
    serialize_calculated_signal_config,
)
from vet_data_modular.dependency_graph import DependencyGraph
from vet_data_modular.formula_validator import parse_and_validate_formula
from vet_data_modular.signal_resolver import SignalResolver


def _definition(stable_id, formula, bindings, **kwargs):
    validated = parse_and_validate_formula(formula, bindings)
    return CalculatedSignalDefinition(
        stable_id=stable_id, display_name=kwargs.pop("display_name", stable_id),
        user_formula=formula, normalized_formula=validated.normalized_formula,
        signal_tokens=dict(bindings), dependencies=validated.dependencies, **kwargs,
    )


def _resolver(signals):
    catalog = {key: {"name": key, "display_name": key} for key in signals}
    data = {
        key: {"timestamps": np.asarray(timestamps), "samples": np.asarray(samples)}
        for key, (timestamps, samples) in signals.items()
    }
    return SignalResolver(catalog, legacy_math_data=data)


def _config(*definitions):
    return serialize_calculated_signal_config(definitions)


class DependencyGraphTests(unittest.TestCase):
    def test_raw_and_calculated_dependencies_are_separate_and_sorted(self):
        a = _definition("Calc_A", "S001+S002", {"S001": "A", "S002": "B"})
        b = _definition("Calc_B", "S001*S002", {"S001": "Calc_A", "S002": "C"})
        c = _definition("Calc_C", "derivative(S001)", {"S001": "Calc_B"})
        graph = DependencyGraph([c, a, b]).analyze()
        self.assertEqual(graph.order, ("Calc_A", "Calc_B", "Calc_C"))
        self.assertEqual(graph.calculated_dependencies["Calc_B"], ("Calc_A",))
        self.assertEqual(graph.raw_dependencies["Calc_B"], ("C",))

    def test_same_level_nodes_use_stable_id_lexical_order(self):
        z = _definition("Calc_Z", "S001+1", {"S001": "A"})
        a = _definition("Calc_A", "S001+1", {"S001": "A"})
        self.assertEqual(DependencyGraph([z, a]).analyze().order, ("Calc_A", "Calc_Z"))

    def test_self_two_node_and_three_node_cycles_are_reported(self):
        cases = [
            [_definition("A", "S001", {"S001": "A"})],
            [_definition("A", "S001", {"S001": "B"}), _definition("B", "S001", {"S001": "A"})],
            [
                _definition("A", "S001", {"S001": "B"}),
                _definition("B", "S001", {"S001": "C"}),
                _definition("C", "S001", {"S001": "A"}),
            ],
        ]
        for definitions in cases:
            with self.subTest(count=len(definitions)):
                graph = DependencyGraph(definitions).analyze()
                self.assertEqual(set(graph.cycle_nodes), {item.stable_id for item in definitions})


class CalculatedSignalConfigTests(unittest.TestCase):
    def test_v2_round_trip_preserves_all_definition_fields_without_arrays(self):
        definition = _definition(
            "calc-001", "S001*2", {"S001": "raw-unique-key"},
            display_name="Power", result_unit="kW", comment="description",
            interpolation_policy={"continuous": "linear", "discrete": "previous"},
            numeric_policy={"invalid": "nan", "divide_by_zero": "nan"},
        )
        serialized = serialize_calculated_signal_config([definition])
        self.assertEqual(serialized["schema_version"], 2)
        self.assertNotIn("samples", repr(serialized))
        self.assertNotIn("timestamps", repr(serialized))
        loaded = load_calculated_signal_config(serialized).definitions[0]
        self.assertEqual(loaded, definition)

    def test_current_missing_and_future_versions_have_explicit_rules(self):
        self.assertEqual(CALCULATED_SIGNAL_SCHEMA_VERSION, 2)
        with self.assertRaisesRegex(CalculatedSignalConfigError, "schema_version") as missing:
            load_calculated_signal_config({"calculated_signals": []})
        self.assertEqual(missing.exception.code, "missing_schema_version")
        with self.assertRaises(CalculatedSignalConfigError) as future:
            load_calculated_signal_config({"schema_version": 999, "calculated_signals": []})
        self.assertEqual(future.exception.code, "unsupported_schema_version")

    def test_legacy_operators_migrate_to_safe_stable_tokens(self):
        expected = {"+": "S001 + S002", "-": "S001 - S002", "*": "S001 * S002", "/": "S001 / S002"}
        for operator, normalized in expected.items():
            with self.subTest(operator=operator):
                item = migrate_legacy_definition("MATH_result", {
                    "key_a": "same_G1_C2", "key_b": "same_G3_C4", "op": operator,
                    "policy": "nan", "new_name": "Result",
                })
                self.assertEqual(item.normalized_formula, normalized)
                self.assertEqual(item.signal_tokens, {"S001": "same_G1_C2", "S002": "same_G3_C4"})
                self.assertEqual(item.stable_id, "MATH_result")

    def test_legacy_container_without_version_is_recognized_only_by_shape(self):
        loaded = load_calculated_signal_config({"math_channels": {
            "MATH_sum": {"key_a": "A", "key_b": "B", "op": "+", "policy": "zero"}
        }})
        self.assertTrue(loaded.migrated_legacy)
        self.assertEqual(loaded.definitions[0].numeric_policy["divide_by_zero"], "zero")


class CalculatedSignalRestoreTests(unittest.TestCase):
    def setUp(self):
        timestamps = np.array([0.0, 1.0, 2.0])
        self.resolver = _resolver({
            "A": (timestamps, [1, 2, 3]), "B": (timestamps, [2, 2, 2]),
            "C": (timestamps, [3, 3, 3]), "ZERO": (timestamps, [0, 1, 0]),
        })
        self.service = CalculatedSignalRestoreService()

    def test_out_of_order_chain_restores_by_dependencies(self):
        a = _definition("Calc_A", "S001+S002", {"S001": "A", "S002": "B"})
        b = _definition("Calc_B", "S001*S002", {"S001": "Calc_A", "S002": "C"})
        c = _definition("Calc_C", "derivative(S001)", {"S001": "Calc_B"})
        report = self.service.restore(_config(c, a, b), self.resolver)
        self.assertFalse(report.failures)
        self.assertEqual(report.restore_order, ("Calc_A", "Calc_B", "Calc_C"))
        np.testing.assert_allclose(report.results["Calc_C"].samples, [3, 3, 3])

    def test_derivative_integral_chain_survives_config_round_trip(self):
        first = _definition("Calc1", "S001+S002", {"S001": "A", "S002": "B"})
        second = _definition("Calc2", "derivative(S001)", {"S001": "Calc1"})
        third = _definition("Calc3", "integral(S001)", {"S001": "Calc2"})
        report = self.service.restore(_config(third, first, second), self.resolver)
        self.assertFalse(report.failures)
        np.testing.assert_allclose(report.results["Calc2"].samples, [1, 1, 1])
        np.testing.assert_allclose(report.results["Calc3"].samples, [0, 1, 2])

    def test_cycles_fail_structurally_without_calculation(self):
        a = _definition("Calc_A", "S001", {"S001": "Calc_B"})
        b = _definition("Calc_B", "S001", {"S001": "Calc_A"})
        report = self.service.restore(_config(a, b), self.resolver)
        self.assertFalse(report.results)
        self.assertEqual(report.failures["Calc_A"].code, "dependency_cycle")
        self.assertEqual(report.failures["Calc_B"].code, "dependency_cycle")

    def test_missing_raw_and_calculated_dependencies_are_explicit(self):
        missing_raw = _definition("Calc_Raw", "S001+1", {"S001": "NO_RAW"})
        missing_calc = _definition("Calc_Down", "S001+1", {"S001": "NO_CALC"})
        report = self.service.restore(_config(missing_raw, missing_calc), self.resolver)
        self.assertEqual(report.failures["Calc_Raw"].code, "missing_dependency")
        self.assertEqual(report.failures["Calc_Down"].code, "missing_dependency")

    def test_upstream_failure_propagates_but_independent_branch_succeeds(self):
        broken = _definition("Calc_Broken", "S001+1", {"S001": "MISSING"})
        downstream = _definition("Calc_Down", "S001*2", {"S001": "Calc_Broken"})
        healthy = _definition("Calc_OK", "S001+1", {"S001": "A"})
        report = self.service.restore(_config(downstream, healthy, broken), self.resolver)
        self.assertEqual(report.failures["Calc_Broken"].code, "missing_dependency")
        self.assertEqual(report.failures["Calc_Down"].code, "dependency_failed")
        np.testing.assert_allclose(report.results["Calc_OK"].samples, [2, 3, 4])

    def test_all_legacy_binary_operators_restore_with_equivalent_results(self):
        expected = {
            "+": [3, 4, 5], "-": [-1, 0, 1],
            "*": [2, 4, 6], "/": [0.5, 1, 1.5],
        }
        for operator, samples in expected.items():
            with self.subTest(operator=operator):
                config = {"math_channels": {"MATH_value": {
                    "key_a": "A", "key_b": "B", "op": operator, "policy": "nan",
                }}}
                report = self.service.restore(config, self.resolver)
                self.assertFalse(report.failures)
                np.testing.assert_allclose(report.results["MATH_value"].samples, samples)

    def test_legacy_nan_and_zero_division_policies_remain_distinct(self):
        common = {"key_a": "A", "key_b": "ZERO", "op": "/"}
        for policy, expected in (("nan", [np.nan, 2, np.nan]), ("zero", [0, 2, 0])):
            with self.subTest(policy=policy):
                config = {"math_channels": {"MATH_div": {**common, "policy": policy}}}
                report = self.service.restore(config, self.resolver)
                self.assertTrue(report.migrated_legacy)
                np.testing.assert_allclose(
                    report.results["MATH_div"].samples, expected, equal_nan=True
                )

    def test_definition_dependency_mismatch_is_rejected(self):
        valid = _definition("Calc", "S001+1", {"S001": "A"})
        tampered = CalculatedSignalDefinition(
            **{**valid.__dict__, "dependencies": ("B",)}
        )
        report = self.service.restore(_config(tampered), self.resolver)
        self.assertEqual(report.failures["Calc"].code, "dependency_mismatch")

    def test_malicious_formula_cannot_bypass_validator_or_execute(self):
        marker = Path("stage6-should-not-exist")
        malicious = CalculatedSignalDefinition(
            stable_id="CalcBad", display_name="Bad",
            user_formula=f'open("{marker}", "w")',
            normalized_formula=f'open("{marker}", "w")',
            signal_tokens={}, dependencies=(),
        )
        report = self.service.restore(_config(malicious), self.resolver)
        self.assertEqual(report.failures["CalcBad"].code, "invalid_formula")
        self.assertFalse(marker.exists())

    def test_core_modules_are_gui_free_and_do_not_use_dynamic_execution(self):
        root = Path(__file__).parents[1] / "vet_data" / "vet_data_modular"
        for name in ("dependency_graph.py", "calculated_signal_config.py"):
            source = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("PyQt", source)
            self.assertNotIn("eval(", source)
            self.assertNotIn("exec(", source)


if __name__ == "__main__":
    unittest.main()