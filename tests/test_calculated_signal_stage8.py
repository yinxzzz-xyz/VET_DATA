import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd
from PyQt6.QtWidgets import QApplication

import vet_data_modular.window as window_module
from vet_data_modular.calculated_signal import CalculatedSignalDefinition
from vet_data_modular.calculated_signal_config import serialize_calculated_signal_config
from vet_data_modular.formula_editor import FormulaEditorDialog, SIGNAL_KEY_ROLE
from vet_data_modular.formula_validator import parse_and_validate_formula
from vet_data_modular.legacy import baseline
from vet_data_modular.window import MDFPlotter
from vet_data_modular.workers import LoadResult


APP = QApplication.instance() or QApplication([])


def _info(name):
    return {
        "name": name, "display_name": name, "group": -1, "channel": -1,
        "comment": "raw", "bus_id": None,
    }


def _definition(stable_id, formula, bindings, name=None, unit=""):
    validated = parse_and_validate_formula(formula, bindings)
    return CalculatedSignalDefinition(
        stable_id=stable_id, display_name=name or stable_id,
        user_formula=formula, normalized_formula=validated.normalized_formula,
        signal_tokens=dict(bindings), dependencies=validated.dependencies,
        result_unit=unit, comment=f"definition {stable_id}",
    )


class FormulaConfigLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.window = MDFPlotter()
        self.window.mdf_path = "stage8.csv"
        self.window.mdf_file = None
        self.window.signals.clear()
        self.window.custom_math_data.clear()
        self.window.calculated_signal_definitions.clear()
        self._raw("A", [1, 2, 3])
        self._raw("B", [2, 2, 2])
        self._raw("C", [2, 2, 2])
        self._raw("ZERO", [0, 1, 0])
        self.window.refresh_signal_list_ui()

    def tearDown(self):
        self.window.deleteLater()
        APP.processEvents()

    def _raw(self, key, samples):
        self.window.signals[key] = _info(key)
        self.window.custom_math_data[key] = {
            "timestamps": np.array([0.0, 1.0, 2.0]),
            "samples": np.asarray(samples, dtype=float),
        }

    def _restore(self, *definitions):
        return self.window._restore_calculated_channels(
            serialize_calculated_signal_config(definitions)
        )

    def test_v2_save_clear_load_round_trip_preserves_values_ids_and_definitions(self):
        first = _definition("Calc1", "S001+S002", {"S001": "A", "S002": "B"}, "Sum", "kW")
        result = self._restore(first).results["Calc1"]
        self.window.refresh_signal_list_ui()
        self.window.signal_widgets["Calc1"]["checkbox"].setChecked(True)
        before = result.samples.copy()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "formula.json")
            with patch.object(window_module.QFileDialog, "getSaveFileName", return_value=(str(path), "")):
                self.window.save_signal_config()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], 2)
            self.assertEqual(saved["calculated_signals"][0]["stable_id"], "Calc1")
            self.assertNotIn("samples", path.read_text(encoding="utf-8"))
            self.assertNotIn("timestamps", path.read_text(encoding="utf-8"))
            self.window.signals.pop("Calc1")
            self.window.custom_math_data.pop("Calc1")
            self.window.calculated_signal_definitions.clear()
            with patch.object(window_module.QFileDialog, "getOpenFileName", return_value=(str(path), "")), \
                 patch.object(window_module.QMessageBox, "information"), \
                 patch.object(self.window, "plot_selected_signals"):
                self.window.load_signal_config()
        np.testing.assert_allclose(self.window.custom_math_data["Calc1"]["samples"], before)
        self.assertEqual(set(self.window.calculated_signal_definitions), {"Calc1"})
        self.assertTrue(self.window.signal_widgets["Calc1"]["checkbox"].isChecked())
        resolved = self.window._formula_resolver().resolve("Calc1")
        self.assertEqual((resolved.key, resolved.unit), ("Calc1", "kW"))
        self.assertEqual(self.window._formula_resolver().resolve("A").unit, "Math")

    def test_out_of_order_four_level_chain_is_registered_topologically(self):
        calc1 = _definition("Calc1", "S001+S002", {"S001": "A", "S002": "B"})
        calc2 = _definition("Calc2", "derivative(S001)", {"S001": "Calc1"})
        calc3 = _definition("Calc3", "integral(S001)/S002", {"S001": "Calc2", "S002": "C"})
        calc4 = _definition("Calc4", "sqrt(abs(S001))", {"S001": "Calc3"})
        report = self._restore(calc4, calc3, calc2, calc1)
        self.assertFalse(report.failures)
        self.assertEqual(report.restore_order, ("Calc1", "Calc2", "Calc3", "Calc4"))
        self.assertEqual(set(report.results), {"Calc1", "Calc2", "Calc3", "Calc4"})
        np.testing.assert_allclose(report.results["Calc4"].samples, np.sqrt([0, 0.5, 1]))

    def test_duplicate_load_replaces_managed_ids_without_id_or_binding_drift(self):
        first = _definition("Calc1", "S001+1", {"S001": "A"}, "First")
        config = serialize_calculated_signal_config([first])
        one = self.window._restore_calculated_channels(config)
        original_array = self.window.custom_math_data["Calc1"]["samples"]
        two = self.window._restore_calculated_channels(config)
        self.assertEqual(set(self.window.calculated_signal_definitions), {"Calc1"})
        self.assertEqual([key for key in self.window.signals if key == "Calc1"], ["Calc1"])
        self.assertEqual(one.definitions["Calc1"], two.definitions["Calc1"])
        self.assertIsNot(original_array, self.window.custom_math_data["Calc1"]["samples"])

    def test_legacy_operators_and_division_policies_restore_through_window(self):
        expected = {"+": [3, 4, 5], "-": [-1, 0, 1], "*": [2, 4, 6], "/": [0.5, 1, 1.5]}
        for index, (operator, samples) in enumerate(expected.items()):
            with self.subTest(operator=operator):
                key = f"MATH_{index}"
                report = self.window._restore_calculated_channels({"math_channels": {key: {
                    "key_a": "A", "key_b": "B", "op": operator,
                    "policy": "nan", "new_name": key,
                }}})
                self.assertTrue(report.migrated_legacy)
                np.testing.assert_allclose(report.results[key].samples, samples)
        for policy, expected_samples in (("zero", [0, 2, 0]), ("nan", [np.nan, 2, np.nan])):
            with self.subTest(policy=policy):
                report = self.window._restore_calculated_channels({"math_channels": {"MATH_div": {
                    "key_a": "A", "key_b": "ZERO", "op": "/", "policy": policy,
                }}})
                np.testing.assert_allclose(report.results["MATH_div"].samples, expected_samples, equal_nan=True)

    def test_missing_upstream_cycle_and_invalid_formula_report_partial_success(self):
        missing = _definition("Missing", "S001+1", {"S001": "NO_RAW"})
        downstream = _definition("Down", "S001+1", {"S001": "Missing"})
        cycle_a = _definition("CycleA", "S001", {"S001": "CycleB"})
        cycle_b = _definition("CycleB", "S001", {"S001": "CycleA"})
        healthy = _definition("Healthy", "S001+1", {"S001": "A"})
        invalid = CalculatedSignalDefinition(
            stable_id="Invalid", display_name="Invalid", user_formula="S999+",
            normalized_formula="S999+", signal_tokens={}, dependencies=(),
        )
        report = self._restore(downstream, cycle_b, healthy, invalid, missing, cycle_a)
        self.assertEqual(set(report.results), {"Healthy"})
        self.assertEqual(report.failures["Missing"].code, "missing_dependency")
        self.assertEqual(report.failures["Down"].code, "dependency_failed")
        self.assertEqual(report.failures["CycleA"].code, "dependency_cycle")
        self.assertEqual(report.failures["Invalid"].code, "invalid_formula")
        summary = self.window._restore_summary(report)
        self.assertIn("成功恢复：1", summary)
        self.assertIn("失败：5", summary)

    def test_bad_json_and_unsupported_schema_are_caught_without_destroying_state(self):
        self._restore(_definition("Existing", "S001+1", {"S001": "A"}))
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder, "bad.json"), Path(folder, "future.json")]
            paths[0].write_text("{broken", encoding="utf-8")
            paths[1].write_text(json.dumps({"schema_version": 999, "calculated_signals": []}), encoding="utf-8")
            for path in paths:
                with self.subTest(path=path.name), \
                     patch.object(window_module.QFileDialog, "getOpenFileName", return_value=(str(path), "")), \
                     patch.object(window_module.QMessageBox, "critical") as critical:
                    self.window.load_signal_config()
                    critical.assert_called_once()
                    self.assertIn("Existing", self.window.signals)

    def test_file_switch_clears_runtime_and_same_config_recomputes_new_file(self):
        definition = _definition("Calc", "S001*2", {"S001": "A"})
        old = self._restore(definition).results["Calc"].samples.copy()
        frame = pd.DataFrame({"A": [10.0, 20.0, 30.0]}, index=[0.0, 1.0, 2.0])
        result = LoadResult("new.csv", frame, {"A": _info("A")})
        with patch.object(self.window, "load_config"), patch.object(self.window, "_close_load_dialog"):
            self.window._finish_data_load(result)
        self.assertNotIn("Calc", self.window.signals)
        self.assertEqual(self.window.custom_math_data, {})
        self.assertEqual(self.window.calculated_signal_definitions, {})
        new = self._restore(definition).results["Calc"].samples
        np.testing.assert_allclose(old, [2, 4, 6])
        np.testing.assert_allclose(new, [20, 40, 60])
        self.assertFalse(np.shares_memory(old, new))

    def test_restored_channel_is_listed_searchable_reusable_and_plottable(self):
        first = _definition("Calc1", "S001+S002", {"S001": "A", "S002": "B"}, "First Calc")
        self._restore(first)
        self.window.refresh_signal_list_ui()
        self.assertIn("Calc1", self.window.signal_widgets)
        dialog = FormulaEditorDialog(self.window.signals)
        try:
            dialog.filter_signals("first calc")
            visible = [
                dialog.signal_list.item(i).data(SIGNAL_KEY_ROLE)
                for i in range(dialog.signal_list.count())
                if not dialog.signal_list.item(i).isHidden()
            ]
            self.assertEqual(visible, ["Calc1"])
            dialog.insert_signal_key("Calc1")
            self.assertEqual(dialog.internal_formula(), "S001")
            self.assertEqual(dialog.signal_tokens, {"S001": "Calc1"})
        finally:
            dialog.deleteLater()
        second = _definition("Calc2", "S001*2", {"S001": "Calc1"}, "Second")
        self._restore(first, second)
        self.window.refresh_signal_list_ui()
        self.window.signal_widgets["Calc2"]["checkbox"].setChecked(True)
        self.window.plot_selected_signals()
        self.assertEqual(len(self.window.plot_widgets), 1)
        np.testing.assert_allclose(self.window.plot_widgets[0].signal_data.samples, [6, 8, 10])

    def test_restored_channel_uses_existing_csv_export_path(self):
        self._restore(_definition("CalcCSV", "S001+1", {"S001": "A"}, "Calc Export", "V"))
        self.window.refresh_signal_list_ui()
        self.window.signal_widgets["CalcCSV"]["checkbox"].setChecked(True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "out.csv")
            with patch.object(baseline.QFileDialog, "getSaveFileName", return_value=(str(path), "")), \
                 patch.object(baseline.QInputDialog, "getDouble", return_value=(1.0, True)), \
                 patch.object(baseline.QMessageBox, "information"):
                self.window.export_selected_signals_to_csv()
            exported = pd.read_csv(path)
        self.assertEqual(list(exported.columns), ["timestamp", "🧮 Calc Export"])
        np.testing.assert_allclose(exported["🧮 Calc Export"], [2, 3, 4])


if __name__ == "__main__":
    unittest.main()