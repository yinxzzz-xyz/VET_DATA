"""Stage 0 compatibility baseline for the legacy binary math channels.

These tests intentionally describe the current implementation.  In particular,
the A-axis/``np.interp`` endpoint-hold semantics are not requirements for the
future formula engine.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asammdf
import numpy as np
import pandas as pd
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDialog
from asammdf import Signal

from vet_data_modular import baseline
from vet_data_modular.math_channel import SearchableMathChannelDialog
from vet_data_modular.window import MDFPlotter
from vet_data_modular.workers import LoadResult


APP = QApplication.instance() or QApplication([])


def _info(name):
    return {
        "name": name,
        "display_name": name,
        "group": -1,
        "channel": -1,
        "comment": "test",
        "bus_id": None,
    }


class _AcceptedMathDialog:
    config = None

    def __init__(self, *_args, **_kwargs):
        pass

    def exec(self):
        return QDialog.DialogCode.Accepted

    def get_config(self):
        return dict(self.config)


class LegacyMathChannelCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.window = MDFPlotter()
        self.window.mdf_path = "baseline.csv"
        self.window.mdf_file = pd.DataFrame(
            {"A": [0.0, 0.0], "B": [0.0, 0.0]}, index=[0.0, 1.0]
        )
        self.window.signals.clear()
        self.window.signals.update({"A": _info("A"), "B": _info("B")})

    def tearDown(self):
        self.window.deleteLater()
        APP.processEvents()

    def _set_signal(self, key, timestamps, samples):
        self.window.signals[key] = _info(key)
        self.window.custom_math_data[key] = {
            "timestamps": np.asarray(timestamps, dtype=float),
            "samples": np.asarray(samples, dtype=float),
        }

    def _rebuild(self, key, key_a="A", key_b="B", op="+", policy="zero", name=None):
        self.window.rebuild_math_channel(
            key,
            {
                "key_a": key_a,
                "key_b": key_b,
                "op": op,
                "policy": policy,
                "display_name": name or key.removeprefix("MATH_"),
            },
        )
        return self.window.custom_math_data.get(key)

    def test_four_arithmetic_operations_use_a_timestamps_and_linear_b(self):
        self._set_signal("A", [0.0, 1.0, 3.0], [10.0, 20.0, 40.0])
        self._set_signal("B", [0.0, 2.0, 4.0], [2.0, 4.0, 8.0])
        expected_b = np.array([2.0, 3.0, 6.0])
        expected = {
            "+": np.array([12.0, 23.0, 46.0]),
            "-": np.array([8.0, 17.0, 34.0]),
            "*": np.array([20.0, 60.0, 240.0]),
            "/": np.array([5.0, 20.0 / 3.0, 40.0 / 6.0]),
        }
        np.testing.assert_allclose(np.interp([0.0, 1.0, 3.0], [0.0, 2.0, 4.0], [2.0, 4.0, 8.0]), expected_b)
        for index, (op, values) in enumerate(expected.items()):
            result = self._rebuild(f"MATH_op_{index}", op=op)
            np.testing.assert_array_equal(result["timestamps"], [0.0, 1.0, 3.0])
            np.testing.assert_allclose(result["samples"], values)

    def test_division_threshold_and_zero_nan_policies(self):
        self._set_signal("A", [0, 1, 2, 3], [1, 1, 1, 1])
        self._set_signal("B", [0, 1, 2, 3], [0, 0.999e-7, 1e-7, -1e-8])
        zero = self._rebuild("MATH_zero", op="/", policy="zero")["samples"]
        nan = self._rebuild("MATH_nan", op="/", policy="nan")["samples"]
        np.testing.assert_allclose(zero, [0.0, 0.0, 1e7, 0.0])
        self.assertTrue(np.isnan(nan[[0, 1, 3]]).all())
        self.assertEqual(nan[2], 1e7)

    def test_different_sampling_rates_ranges_and_endpoint_hold(self):
        self._set_signal("A", [-1, 0, 0.5, 1.5, 2, 3], [0, 0, 0, 0, 0, 0])
        self._set_signal("B", [0, 1, 2], [10, 20, 40])
        result = self._rebuild("MATH_range")["samples"]
        np.testing.assert_allclose(result, [10, 10, 15, 30, 40, 40])

    def test_nonuniform_timestamps_are_preserved_without_union(self):
        self._set_signal("A", [0.0, 0.1, 1.7, 4.2], [1, 1, 1, 1])
        self._set_signal("B", [0.0, 0.4, 2.5, 5.0], [0, 4, 25, 50])
        result = self._rebuild("MATH_irregular")
        np.testing.assert_array_equal(result["timestamps"], [0.0, 0.1, 1.7, 4.2])
        np.testing.assert_allclose(result["samples"], 1 + np.interp(result["timestamps"], [0, 0.4, 2.5, 5], [0, 4, 25, 50]))

    def test_nan_and_inf_follow_numpy_and_interp_behavior(self):
        self._set_signal("A", [0, 1, 2, 3], [1, np.nan, np.inf, -np.inf])
        self._set_signal("B", [0, 1, 2, 3], [2, 2, np.nan, np.inf])
        result = self._rebuild("MATH_nonfinite", op="+")["samples"]
        self.assertEqual(result[0], 3.0)
        self.assertTrue(np.isnan(result[1]))
        self.assertTrue(np.isnan(result[2]))
        self.assertTrue(np.isnan(result[3]))

    def test_calculated_channel_can_be_used_as_later_operand(self):
        self._set_signal("A", [0, 1], [1, 2])
        self._set_signal("B", [0, 1], [10, 20])
        first = self._rebuild("MATH_first", op="+")
        self.assertIsNotNone(first)
        second = self._rebuild("MATH_second", key_a="MATH_first", key_b="B", op="*")
        np.testing.assert_allclose(second["samples"], [110, 440])

    def test_internal_math_key_collision_is_rejected_even_for_raw_entry(self):
        self.window.signals["MATH_taken"] = _info("raw-but-prefixed")
        _AcceptedMathDialog.config = {
            "key_a": "A", "key_b": "B", "op": "+", "policy": "zero", "new_name": "taken"
        }
        with patch.object(baseline, "MathChannelDialog", _AcceptedMathDialog), \
             patch.object(baseline.QMessageBox, "warning") as warning:
            baseline.MDFPlotter.create_math_channel(self.window)
        warning.assert_called_once()
        self.assertNotIn("MATH_taken", self.window.custom_math_data)

    def test_successful_creation_registers_selects_and_exposes_signal(self):
        self._set_signal("A", [0, 1], [1, 2])
        self._set_signal("B", [0, 1], [3, 4])
        self.window.refresh_signal_list_ui()
        _AcceptedMathDialog.config = {
            "key_a": "A", "key_b": "B", "op": "+", "policy": "zero", "new_name": "sum"
        }
        with patch.object(baseline, "MathChannelDialog", _AcceptedMathDialog), \
             patch.object(baseline.QMessageBox, "information"):
            baseline.MDFPlotter.create_math_channel(self.window)
        self.assertIn("MATH_sum", self.window.signals)
        self.assertIn("MATH_sum", self.window.custom_math_data)
        self.assertIn("MATH_sum", self.window.get_selected_signals())
        signal = self.window._get_signal(self.window.signals["MATH_sum"])
        np.testing.assert_allclose(signal.samples, [4, 6])

    def test_calculated_channel_plot_and_cursor_value_path(self):
        self._set_signal("MATH_plot", [0, 1, 2], [10, 20, 30])
        self.window.signals["MATH_plot"].update(display_name="Calc Plot", comment="computed")
        self.window.refresh_signal_list_ui()
        self.window.signal_widgets["MATH_plot"]["checkbox"].setChecked(True)
        self.window.plot_selected_signals()
        self.assertEqual(len(self.window.plot_widgets), 1)
        np.testing.assert_allclose(self.window.plot_widgets[0].signal_data.samples, [10, 20, 30])
        self.window._set_view_mode("selected")
        self.window._mouse_in_plot = True
        self.window._current_cursor_time = 0.5
        self.window._update_signal_list_values()
        item = next(self.window.signal_list_widget.item(i) for i in range(self.window.signal_list_widget.count()) if self.window.signal_list_widget.item(i).data(Qt.ItemDataRole.UserRole) == "MATH_plot")
        self.assertEqual(item.data(Qt.ItemDataRole.UserRole + 2), "20.000")

    def test_save_config_serializes_definitions_not_samples(self):
        self._set_signal("A", [0, 1], [1, 2])
        self._set_signal("B", [0, 1], [3, 4])
        self._rebuild("MATH_sum")
        self.window.refresh_signal_list_ui()
        self.window.signal_widgets["MATH_sum"]["checkbox"].setChecked(True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "config.json")
            with patch.object(baseline.QFileDialog, "getSaveFileName", return_value=(str(path), "")):
                baseline.MDFPlotter.save_signal_config(self.window)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["selected_signals"], ["MATH_sum"])
        self.assertEqual(saved["math_channels"]["MATH_sum"]["op"], "+")
        self.assertNotIn("samples", saved["math_channels"]["MATH_sum"])
        self.assertNotIn("timestamps", saved["math_channels"]["MATH_sum"])

    def test_load_config_rebuilds_chain_only_in_dependency_order(self):
        self._set_signal("A", [0, 1], [1, 2])
        self._set_signal("B", [0, 1], [10, 20])
        first = {"key_a": "A", "key_b": "B", "op": "+", "policy": "zero", "display_name": "first"}
        second = {"key_a": "MATH_first", "key_b": "B", "op": "*", "policy": "zero", "display_name": "second"}
        with tempfile.TemporaryDirectory() as folder:
            ordered = Path(folder, "ordered.json")
            ordered.write_text(json.dumps({"selected_signals": [], "math_channels": {"MATH_first": first, "MATH_second": second}}), encoding="utf-8")
            reversed_path = Path(folder, "reversed.json")
            reversed_path.write_text(json.dumps({"selected_signals": [], "math_channels": {"MATH_second": second, "MATH_first": first}}), encoding="utf-8")
            with patch.object(baseline.QFileDialog, "getOpenFileName", return_value=(str(ordered), "")), \
                 patch.object(self.window, "plot_selected_signals"):
                baseline.MDFPlotter.load_signal_config(self.window)
            self.assertIn("MATH_second", self.window.signals)
            self.window.signals.pop("MATH_first"); self.window.signals.pop("MATH_second")
            self.window.custom_math_data.clear()
            with patch.object(baseline.QFileDialog, "getOpenFileName", return_value=(str(reversed_path), "")), \
                 patch.object(self.window, "plot_selected_signals"):
                baseline.MDFPlotter.load_signal_config(self.window)
        self.assertIn("MATH_first", self.window.signals)
        self.assertNotIn("MATH_second", self.window.signals)

    def test_missing_dependency_is_silently_skipped(self):
        missing = {"key_a": "DOES_NOT_EXIST", "key_b": "B", "op": "+", "display_name": "missing"}
        self.assertIsNone(self.window.rebuild_math_channel("MATH_missing", missing))
        self.assertNotIn("MATH_missing", self.window.signals)
        self.assertNotIn("MATH_missing", self.window.custom_math_data)

    def test_file_switch_clears_calculated_channels(self):
        self._set_signal("MATH_old", [0, 1], [1, 2])
        frame = pd.DataFrame({"new": [3, 4]}, index=[0.0, 1.0])
        result = LoadResult("new.csv", frame, {"new": _info("new")})
        with patch.object(self.window, "load_config"), patch.object(self.window, "_close_load_dialog"):
            self.window._finish_data_load(result)
        self.assertEqual(set(self.window.signals), {"new"})
        self.assertEqual(self.window.custom_math_data, {})

    def test_csv_export_includes_selected_calculated_channel(self):
        self._set_signal("MATH_export", [0, 1, 2], [5, 6, 7])
        self.window.signals["MATH_export"].update(display_name="Calc Export")
        self.window.refresh_signal_list_ui()
        self.window.signal_widgets["MATH_export"]["checkbox"].setChecked(True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "out.csv")
            with patch.object(baseline.QFileDialog, "getSaveFileName", return_value=(str(path), "")), \
                 patch.object(baseline.QInputDialog, "getDouble", return_value=(1.0, True)), \
                 patch.object(baseline.QMessageBox, "information"):
                self.window.export_selected_signals_to_csv()
            exported = pd.read_csv(path)
        self.assertEqual(list(exported.columns), ["timestamp", "Calc Export"])
        np.testing.assert_allclose(exported["Calc Export"], [5, 6, 7])

    def test_mdf_cutout_does_not_append_calculated_channel(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, "source.mf4")
            output = Path(folder, "cut.mf4")
            mdf = asammdf.MDF()
            mdf.append(Signal(samples=np.array([1.0, 2.0]), timestamps=np.array([0.0, 1.0]), name="raw"))
            mdf.save(source, overwrite=True)
            mdf.close()
            self.window.mdf_path = str(source)
            self.window.mdf_file = asammdf.MDF(source)
            self._set_signal("MATH_memory_only", [0, 1], [9, 9])
            self.window.master_viewbox = type("Range", (), {"viewRange": lambda _self: [[0.0, 1.0], [0.0, 1.0]]})()
            with patch.object(baseline.QFileDialog, "getSaveFileName", return_value=(str(output), "")), \
                 patch.object(baseline.QMessageBox, "information"):
                self.window.save_data_cutout()
            cut = asammdf.MDF(output)
            names = [channel.name for channel in cut.iter_channels()]
            cut.close()
            self.window.mdf_file.close()
            self.window.mdf_file = None
        self.assertIn("raw", names)
        self.assertNotIn("MATH_memory_only", names)


class SearchableMathDialogCompatibilityTests(unittest.TestCase):
    def test_completers_remain_contains_case_insensitive_and_keep_keys(self):
        signals = {
            "second": {"display_name": "vehicle SPEED"},
            "first": {"display_name": "Engine Speed"},
        }
        dialog = SearchableMathChannelDialog(signals)
        try:
            self.assertEqual(dialog.combo_a.itemData(0), "first")
            self.assertEqual(dialog.combo_a.itemData(1), "second")
            for combo in (dialog.combo_a, dialog.combo_b):
                completer = combo.completer()
                self.assertTrue(combo.isEditable())
                self.assertEqual(completer.caseSensitivity(), Qt.CaseSensitivity.CaseInsensitive)
                self.assertEqual(completer.filterMode(), Qt.MatchFlag.MatchContains)
        finally:
            dialog.deleteLater()
            APP.processEvents()


if __name__ == "__main__":
    unittest.main()
