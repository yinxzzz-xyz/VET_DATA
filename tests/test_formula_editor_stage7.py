import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDialog

import vet_data_modular.window as window_module
from vet_data_modular.calculated_signal import CalculationStatus
from vet_data_modular.formula_editor import FormulaEditorDialog, SIGNAL_KEY_ROLE
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


def _info(name, display=None, group=-1, channel=-1, source=None):
    data = {
        "name": name, "display_name": display or name, "group": group,
        "channel": channel, "comment": "test", "bus_id": None,
    }
    if source:
        data["source"] = source
    return data


class FormulaEditorDialogTests(unittest.TestCase):
    def setUp(self):
        self.signals = {
            "speed_G1_C2": _info("speed", "Vehicle Speed", 1, 2, "MDF"),
            "speed_G3_C4": _info("speed", "Vehicle Speed", 3, 4, "MDF"),
            "CAN_bus_rpm": _info("rpm", "Engine RPM", source="CAN"),
            "CALC_existing": _info("CALC_existing", "Calculated Power", source="Calculated"),
        }
        self.dialog = FormulaEditorDialog(self.signals)

    def tearDown(self):
        self.dialog.deleteLater()
        APP.processEvents()

    def test_window_contains_required_editor_controls_and_all_signals(self):
        self.assertEqual(self.dialog.signal_list.count(), 4)
        self.assertTrue(self.dialog.name_edit.isEnabled())
        self.assertTrue(self.dialog.unit_edit.isEnabled())
        self.assertIsNotNone(self.dialog.formula_edit)
        self.assertEqual(self.dialog.windowTitle(), "创建自定义公式计算通道")

    def test_case_insensitive_partial_search_includes_raw_and_calculated(self):
        self.dialog.filter_signals("vEhIcLe")
        visible = [
            self.dialog.signal_list.item(i).data(SIGNAL_KEY_ROLE)
            for i in range(self.dialog.signal_list.count())
            if not self.dialog.signal_list.item(i).isHidden()
        ]
        self.assertEqual(set(visible), {"speed_G1_C2", "speed_G3_C4"})
        self.dialog.filter_signals("power")
        visible = [
            self.dialog.signal_list.item(i).data(SIGNAL_KEY_ROLE)
            for i in range(self.dialog.signal_list.count())
            if not self.dialog.signal_list.item(i).isHidden()
        ]
        self.assertEqual(visible, ["CALC_existing"])

    def test_same_name_signals_are_visibly_disambiguated(self):
        labels = [
            self.dialog.signal_list.item(i).text()
            for i in range(self.dialog.signal_list.count())
            if "Vehicle Speed" in self.dialog.signal_list.item(i).text()
        ]
        self.assertEqual(len(labels), 2)
        self.assertNotEqual(labels[0], labels[1])
        self.assertTrue(any("Group: 1" in label and "Channel: 2" in label for label in labels))
        self.assertTrue(any("Group: 3" in label and "Channel: 4" in label for label in labels))

    def test_button_and_double_click_insert_selected_signal(self):
        item = next(
            self.dialog.signal_list.item(i) for i in range(self.dialog.signal_list.count())
            if self.dialog.signal_list.item(i).data(SIGNAL_KEY_ROLE) == "CAN_bus_rpm"
        )
        self.dialog.signal_list.setCurrentItem(item)
        self.dialog.insert_signal_button.click()
        self.assertEqual(self.dialog.formula_edit.toPlainText(), "S001")
        self.dialog.signal_list.itemDoubleClicked.emit(item)
        self.assertEqual(self.dialog.formula_edit.toPlainText(), "S001S001")

    def test_insert_occurs_at_cursor_and_same_key_reuses_token(self):
        self.dialog.formula_edit.setPlainText("+ 2")
        cursor = self.dialog.formula_edit.textCursor()
        cursor.setPosition(0)
        self.dialog.formula_edit.setTextCursor(cursor)
        first = self.dialog.insert_signal_key("speed_G1_C2")
        cursor = self.dialog.formula_edit.textCursor()
        cursor.setPosition(4)
        self.dialog.formula_edit.setTextCursor(cursor)
        second = self.dialog.insert_signal_key("speed_G1_C2")
        self.assertEqual(first, second)
        self.assertEqual(self.dialog.formula_edit.toPlainText(), "S001S001+ 2")
        self.assertEqual(self.dialog.binding_list.count(), 1)

    def test_distinct_same_name_keys_receive_distinct_tokens(self):
        first = self.dialog.insert_signal_key("speed_G1_C2")
        second = self.dialog.insert_signal_key("speed_G3_C4")
        self.assertEqual((first, second), ("S001", "S002"))
        self.assertEqual(
            self.dialog.signal_tokens,
            {"S001": "speed_G1_C2", "S002": "speed_G3_C4"},
        )

    def test_representative_functions_insert_at_cursor_inside_parentheses(self):
        for function in ("sqrt", "sin", "derivative", "integral"):
            with self.subTest(function=function):
                self.dialog.formula_edit.clear()
                self.dialog.insert_function(function)
                self.dialog.formula_edit.insertPlainText("S001")
                self.assertEqual(self.dialog.formula_edit.toPlainText(), f"{function}(S001)")

    def test_validation_success_and_failure_are_user_readable(self):
        self.dialog.insert_signal_key("speed_G1_C2")
        self.dialog.formula_edit.setPlainText("sqrt(abs(S001))")
        self.assertIsNotNone(self.dialog.validate_formula())
        self.assertIn("公式有效", self.dialog.diagnostic_output.toPlainText())
        self.dialog.formula_edit.setPlainText("(S001+")
        self.assertIsNone(self.dialog.validate_formula())
        self.assertIn("✗", self.dialog.diagnostic_output.toPlainText())

    def test_generate_button_does_not_accept_empty_name_or_invalid_formula(self):
        self.dialog.formula_edit.setPlainText("S999")
        self.dialog.generate_button.click()
        self.assertEqual(self.dialog.result(), 0)
        self.assertIn("名称不能为空", self.dialog.diagnostic_output.toPlainText())
        self.dialog.name_edit.setText("Bad")
        self.dialog.generate_button.click()
        self.assertEqual(self.dialog.result(), 0)
        self.assertIn("✗", self.dialog.diagnostic_output.toPlainText())


class FormulaEditorMainWindowTests(unittest.TestCase):
    def setUp(self):
        self.window = MDFPlotter()
        self.window.mdf_path = "formula.csv"
        self.window.mdf_file = None
        self.window.signals.clear()
        self.window.custom_math_data.clear()
        self.window.calculated_signal_definitions.clear()
        self._set_signal("A", [0, 1, 2], [1, 2, 3])
        self._set_signal("B", [0, 1, 2], [2, 2, 2])
        self.window.refresh_signal_list_ui()

    def tearDown(self):
        self.window.deleteLater()
        APP.processEvents()

    def _set_signal(self, key, timestamps, samples, display=None):
        self.window.signals[key] = _info(key, display)
        self.window.custom_math_data[key] = {
            "timestamps": np.asarray(timestamps, dtype=float),
            "samples": np.asarray(samples, dtype=float),
        }

    def _dialog(self, name, formula, keys, unit=""):
        dialog = FormulaEditorDialog(
            self.window.signals, preview_callback=self.window._preview_formula_definition
        )
        for key in keys:
            dialog.insert_signal_key(key)
        dialog.formula_edit.setPlainText(formula)
        dialog.name_edit.setText(name)
        dialog.unit_edit.setText(unit)
        dialog.exec = lambda: QDialog.DialogCode.Accepted
        return dialog

    def _create(self, dialog, via_button=False):
        with patch.object(window_module, "FormulaEditorDialog", return_value=dialog), \
             patch.object(window_module.QMessageBox, "information"), \
             patch.object(window_module.QMessageBox, "warning"), \
             patch.object(window_module.QMessageBox, "critical"):
            if via_button:
                self.window.math_channel_button.click()
            else:
                self.window.create_math_channel()

    def test_official_button_opens_formula_editor_and_creates_basic_channel(self):
        self.assertIn("自定义公式", self.window.math_channel_button.text())
        dialog = self._dialog("Sum", "S001+S002", ["A", "B"], "kW")
        self._create(dialog, via_button=True)
        created = list(self.window.calculated_signal_definitions)
        self.assertEqual(len(created), 1)
        key = created[0]
        self.assertTrue(key.startswith("CALC_"))
        definition = self.window.calculated_signal_definitions[key]
        self.assertEqual((definition.display_name, definition.result_unit), ("Sum", "kW"))
        np.testing.assert_allclose(self.window.custom_math_data[key]["samples"], [3, 4, 5])
        self.assertTrue(self.window.signal_widgets[key]["checkbox"].isChecked())
        self.assertIn("calculated_definition", self.window.signals[key])

    def test_complex_multisignal_and_time_functions_generate(self):
        cases = [
            ("Square", "(S001+S002)^2", ["A", "B"], [9, 16, 25]),
            ("Rate", "derivative(S001)", ["A"], [1, 1, 1]),
            ("Area", "integral(S001)", ["A"], [0, 1.5, 4]),
        ]
        for name, formula, keys, expected in cases:
            with self.subTest(formula=formula):
                before = set(self.window.calculated_signal_definitions)
                self._create(self._dialog(name, formula, keys))
                key = (set(self.window.calculated_signal_definitions) - before).pop()
                np.testing.assert_allclose(self.window.custom_math_data[key]["samples"], expected)

    def test_preview_uses_engine_diagnostics_without_registration(self):
        dialog = self._dialog("Preview", "S001+S002", ["A", "B"])
        before_signals = set(self.window.signals)
        result = dialog.preview_formula()
        self.assertEqual(result.status, CalculationStatus.SUCCESS)
        self.assertEqual(set(self.window.signals), before_signals)
        self.assertFalse(self.window.calculated_signal_definitions)
        diagnostics = dialog.diagnostic_output.toPlainText()
        self.assertIn("预览成功", diagnostics)
        self.assertIn("输出样本数：3", diagnostics)
        self.assertIn("NaN：0", diagnostics)

    def test_failed_preview_reports_error_without_registration(self):
        self._set_signal("D", [10, 11], [1, 2])
        dialog = self._dialog("No overlap", "S001+S002", ["A", "D"])
        before = set(self.window.signals)
        result = dialog.preview_formula()
        self.assertEqual(result.status, CalculationStatus.ERROR)
        self.assertEqual(set(self.window.signals), before)
        self.assertFalse(self.window.calculated_signal_definitions)
        self.assertIn("预览失败", dialog.diagnostic_output.toPlainText())

    def test_generated_channel_is_visible_searchable_and_reusable(self):
        self._create(self._dialog("First Calc", "S001+S002", ["A", "B"]))
        first_key = next(iter(self.window.calculated_signal_definitions))
        second = FormulaEditorDialog(
            self.window.signals, preview_callback=self.window._preview_formula_definition
        )
        try:
            second.filter_signals("first calc")
            visible = [
                second.signal_list.item(i).data(SIGNAL_KEY_ROLE)
                for i in range(second.signal_list.count())
                if not second.signal_list.item(i).isHidden()
            ]
            self.assertEqual(visible, [first_key])
            second.insert_signal_key(first_key)
            second.formula_edit.setPlainText("derivative(S001)")
            second.name_edit.setText("Second Calc")
            second.exec = lambda: QDialog.DialogCode.Accepted
            self._create(second)
        finally:
            second.deleteLater()
        self.assertEqual(len(self.window.calculated_signal_definitions), 2)
        second_key = next(
            key for key in self.window.calculated_signal_definitions if key != first_key
        )
        np.testing.assert_allclose(self.window.custom_math_data[second_key]["samples"], [1, 1, 1])

    def test_duplicate_empty_invalid_and_engine_failure_do_not_register(self):
        self._create(self._dialog("Duplicate", "S001", ["A"]))
        count = len(self.window.calculated_signal_definitions)
        self._create(self._dialog("Duplicate", "S001*2", ["A"]))
        self.assertEqual(len(self.window.calculated_signal_definitions), count)
        self._create(self._dialog("", "S001", ["A"]))
        self.assertEqual(len(self.window.calculated_signal_definitions), count)
        invalid = self._dialog("Invalid", "S999", [])
        self._create(invalid)
        self.assertEqual(len(self.window.calculated_signal_definitions), count)
        self._set_signal("D", [10, 11], [1, 2])
        no_overlap = self._dialog("No overlap", "S001+S002", ["A", "D"])
        self._create(no_overlap)
        self.assertEqual(len(self.window.calculated_signal_definitions), count)


if __name__ == "__main__":
    unittest.main()
