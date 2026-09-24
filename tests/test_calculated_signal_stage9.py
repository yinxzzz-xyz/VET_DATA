import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import QThread
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QDialog

import vet_data_modular.calculation_engine as engine_module
import vet_data_modular.main as app_main
import vet_data_modular.window as window_module
from vet_data_modular.calculated_signal import (
    CalculationStatus, CalculatedSignalDefinition, CalculatedSignalResult,
)
from vet_data_modular.calculated_signal_config import (
    CalculatedSignalRestoreService, serialize_calculated_signal_config,
)
from vet_data_modular.calculation_engine import CalculationEngine
from vet_data_modular.formula_editor import FormulaEditorDialog
from vet_data_modular.formula_validator import parse_and_validate_formula
from vet_data_modular.signal_resolver import SignalResolver
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(0.005)
    APP.processEvents()
    if not predicate():
        raise AssertionError("background task did not finish")


def _result(status=CalculationStatus.SUCCESS, error=None):
    return CalculatedSignalResult(
        timestamps=np.array([0.0, 1.0]), samples=np.array([1.0, 2.0]),
        status=status, error=error,
        diagnostics={"effective_start": 0.0, "effective_end": 1.0,
                     "interpolation_policies": {}},
    )


def _dialog(callback, context=lambda: 1):
    dialog = FormulaEditorDialog(
        {"A": {"name": "A", "display_name": "A"}},
        preview_callback=callback, context_callback=context,
    )
    dialog.name_edit.setText("Calc")
    dialog.formula_edit.setPlainText("S001+1")
    dialog.signal_tokens = {"S001": "A"}
    return dialog


def _definition(stable_id, formula, bindings):
    validated = parse_and_validate_formula(formula, bindings)
    return CalculatedSignalDefinition(
        stable_id=stable_id, display_name=stable_id, user_formula=formula,
        normalized_formula=validated.normalized_formula,
        signal_tokens=dict(bindings), dependencies=validated.dependencies,
    )


class FormulaBackgroundWorkerTests(unittest.TestCase):
    def tearDown(self):
        APP.processEvents()

    def test_preview_runs_off_gui_thread_and_restores_buttons(self):
        callback_threads = []
        dialog = _dialog(lambda _definition: (callback_threads.append(QThread.currentThread()), _result())[1])
        try:
            worker = dialog.preview_formula()
            self.assertIsNotNone(worker)
            self.assertFalse(dialog.preview_button.isEnabled())
            _wait(lambda: dialog._calculation_worker is None)
            self.assertIsNot(callback_threads[0], APP.thread())
            self.assertIn("预览成功", dialog.diagnostic_output.toPlainText())
            self.assertTrue(dialog.preview_button.isEnabled())
            self.assertTrue(dialog.generate_button.isEnabled())
        finally:
            dialog.deleteLater()

    def test_generate_background_success_and_duplicate_submission_protection(self):
        release = threading.Event()
        calls = []
        def calculate(_definition):
            calls.append(QThread.currentThread())
            release.wait(2)
            return _result()
        dialog = _dialog(calculate)
        try:
            first = dialog._start_calculation(dialog.build_definition("__generate__"), "generate")
            second = dialog._start_calculation(dialog.build_definition("__generate__"), "generate")
            self.assertIs(first, second)
            self.assertFalse(dialog.generate_button.isEnabled())
            release.set()
            _wait(lambda: dialog.result() == QDialog.DialogCode.Accepted)
            _wait(lambda: dialog._calculation_worker is None)
            self.assertEqual(len(calls), 1)
            self.assertEqual(dialog.generated_result.status, CalculationStatus.SUCCESS)
        finally:
            dialog.deleteLater()

    def test_structured_failure_restores_state_without_accepting(self):
        dialog = _dialog(lambda _definition: _result(CalculationStatus.ERROR, "engine failed"))
        try:
            dialog._accept_if_valid()
            _wait(lambda: dialog._calculation_worker is None)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            self.assertIn("engine failed", dialog.diagnostic_output.toPlainText())
            self.assertTrue(dialog.generate_button.isEnabled())
        finally:
            dialog.deleteLater()

    def test_editor_close_during_task_ignores_late_result(self):
        release = threading.Event()
        dialog = _dialog(lambda _definition: (release.wait(2), _result())[1])
        dialog.preview_formula()
        dialog.reject()
        release.set()
        _wait(lambda: dialog._calculation_worker is None)
        self.assertIsNone(dialog.generated_result)
        dialog.deleteLater()

    def test_file_context_change_rejects_stale_generate_result(self):
        release = threading.Event()
        context = [1]
        dialog = _dialog(lambda _definition: (release.wait(2), _result())[1], lambda: context[0])
        try:
            dialog._accept_if_valid()
            context[0] = 2
            release.set()
            _wait(lambda: dialog._calculation_worker is None)
            self.assertIsNone(dialog.generated_result)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        finally:
            dialog.deleteLater()


class FormulaPerformanceAndRestoreTests(unittest.TestCase):
    def test_large_vectorized_multisignal_time_formula_aligns_once(self):
        count = 200_000
        timestamps = np.linspace(0.0, 100.0, count)
        catalog = {key: {"name": key, "display_name": key} for key in ("A", "B", "C")}
        runtime = {
            "A": {"timestamps": timestamps, "samples": np.sin(timestamps)},
            "B": {"timestamps": timestamps, "samples": np.cos(timestamps)},
            "C": {"timestamps": timestamps, "samples": np.full(count, 2.0)},
        }
        resolver = SignalResolver(catalog, legacy_math_data=runtime)
        definition = _definition(
            "Large", "sqrt(abs(derivative(S001+S002)))/S003",
            {"S001": "A", "S002": "B", "S003": "C"},
        )
        validated = parse_and_validate_formula(
            definition.normalized_formula, definition.signal_tokens
        )
        original = engine_module.align_signals
        calls = []
        def counted(inputs):
            calls.append(1)
            return original(inputs)
        with patch.object(engine_module, "align_signals", side_effect=counted):
            result = CalculationEngine().calculate(definition, validated, resolver)
        self.assertEqual(result.status, CalculationStatus.SUCCESS)
        self.assertEqual(result.samples.size, count)
        self.assertEqual(len(calls), 1)

    def test_integral_large_array_is_vectorized_and_aligns_once(self):
        count = 200_000
        timestamps = np.linspace(0.0, 20.0, count)
        resolver = SignalResolver(
            {"A": {"name": "A", "display_name": "A"}},
            legacy_math_data={"A": {"timestamps": timestamps, "samples": timestamps}},
        )
        definition = _definition("Integral", "integral(S001)", {"S001": "A"})
        validated = parse_and_validate_formula(definition.normalized_formula, definition.signal_tokens)
        with patch.object(engine_module, "align_signals", wraps=engine_module.align_signals) as aligned:
            result = CalculationEngine().calculate(definition, validated, resolver)
        self.assertEqual(result.status, CalculationStatus.SUCCESS)
        self.assertEqual(aligned.call_count, 1)
        self.assertAlmostEqual(result.samples[-1], 200.0, places=4)

    def test_config_restore_runs_in_background_and_registers_on_gui_thread(self):
        window = MDFPlotter()
        window.mdf_path = "stage9.csv"
        window.mdf_file = None
        window.signals.clear()
        window.custom_math_data.clear()
        window.signals["A"] = {"name": "A", "display_name": "A"}
        window.custom_math_data["A"] = {
            "timestamps": np.array([0.0, 1.0]), "samples": np.array([1.0, 2.0])
        }
        config = serialize_calculated_signal_config([
            _definition("Calc", "S001+1", {"S001": "A"})
        ])
        config.update({"selected_signals": ["Calc"]})
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder, "config.json")
                path.write_text(json.dumps(config), encoding="utf-8")
                with patch.object(window_module.QFileDialog, "getOpenFileName", return_value=(str(path), "")), \
                     patch.object(window_module.QMessageBox, "information"), \
                     patch.object(window, "plot_selected_signals"):
                    window.load_signal_config()
                    self.assertIsNotNone(window._formula_restore_worker)
                    self.assertFalse(window.load_config_button.isEnabled())
                    _wait(lambda: window._formula_restore_worker is None)
            self.assertIn("Calc", window.signals)
            self.assertTrue(window.load_config_button.isEnabled())
        finally:
            window.deleteLater()

    def test_stale_background_restore_does_not_register(self):
        window = MDFPlotter()
        window.mdf_path = "old.csv"
        window.mdf_file = None
        window.signals.clear()
        window.custom_math_data.clear()
        window.signals["A"] = {"name": "A", "display_name": "A"}
        window.custom_math_data["A"] = {
            "timestamps": np.array([0.0, 1.0]), "samples": np.array([1.0, 2.0])
        }
        config = serialize_calculated_signal_config([
            _definition("Stale", "S001+1", {"S001": "A"})
        ])
        original = CalculatedSignalRestoreService.restore
        release = threading.Event()
        def delayed(service, config_data, resolver):
            release.wait(2)
            return original(service, config_data, resolver)
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder, "config.json")
                path.write_text(json.dumps(config), encoding="utf-8")
                with patch.object(window_module.QFileDialog, "getOpenFileName", return_value=(str(path), "")), \
                     patch.object(CalculatedSignalRestoreService, "restore", delayed), \
                     patch.object(window_module.QMessageBox, "information"):
                    window.load_signal_config()
                    window._formula_context_generation += 1
                    window.mdf_path = "new.csv"
                    release.set()
                    _wait(lambda: window._formula_restore_worker is None)
            self.assertNotIn("Stale", window.signals)
        finally:
            window.deleteLater()


class ApplicationIconTests(unittest.TestCase):
    def test_source_icon_path_exists_and_qt_can_load_it(self):
        icon_path = app_main.application_icon_path()
        self.assertEqual(icon_path.name, "VET_DATA.ico")
        self.assertTrue(icon_path.is_file())
        self.assertFalse(QIcon(str(icon_path)).isNull())

    def test_frozen_resource_path_and_spec_include_same_icon(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(app_main.sys, "_MEIPASS", folder, create=True):
            self.assertEqual(
                app_main.application_icon_path(), Path(folder) / "assets" / "VET_DATA.ico"
            )
        spec = (Path(__file__).parents[1] / "VET_DATA.spec").read_text(encoding="utf-8")
        self.assertIn('("assets/VET_DATA.ico", "assets")', spec)
        self.assertIn('icon="assets/VET_DATA.ico"', spec)


if __name__ == "__main__":
    unittest.main()