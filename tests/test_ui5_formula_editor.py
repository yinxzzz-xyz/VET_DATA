import os
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QTextCursor
from PyQt6.QtWidgets import QApplication, QDialog

from vet_data_modular.calculated_signal import CalculationStatus, CalculatedSignalResult
from vet_data_modular.formula_editor import FormulaEditorDialog, SIGNAL_KEY_ROLE
from vet_data_modular.theme import DEFAULT_THEME


APP = QApplication.instance() or QApplication([])


def _info(name, display, group=1, channel=0):
    return {
        "name": name,
        "display_name": display,
        "group": group,
        "channel": channel,
        "source": "MDF",
        "comment": "test",
    }


def _result():
    return CalculatedSignalResult(
        timestamps=np.array([0.0, 1.0]),
        samples=np.array([1.0, 2.0]),
        status=CalculationStatus.SUCCESS,
        diagnostics={
            "effective_start": 0.0,
            "effective_end": 1.0,
            "interpolation_policies": {},
        },
    )


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(0.005)
    APP.processEvents()
    if not predicate():
        raise AssertionError("formula background task did not finish")


class FormulaEditorUi5Tests(unittest.TestCase):
    def setUp(self):
        self.long_name = "Vehicle longitudinal acceleration filtered engineering signal"
        self.signals = {
            "speed": _info("VehSpd", "Vehicle Speed", 1, 0),
            "acceleration": _info("Accel", self.long_name, 1, 1),
            "torque": _info("Torque", "Motor Torque", 2, 0),
        }
        self.dialog = FormulaEditorDialog(
            self.signals,
            preview_callback=lambda _definition: _result(),
            background_calculation=False,
        )

    def tearDown(self):
        self.dialog.deleteLater()
        APP.processEvents()

    def _item(self, key):
        return next(
            self.dialog.signal_list.item(index)
            for index in range(self.dialog.signal_list.count())
            if self.dialog.signal_list.item(index).data(SIGNAL_KEY_ROLE) == key
        )

    def test_three_workflow_sections_and_action_hierarchy(self):
        self.assertEqual(self.dialog.basic_section.objectName(), "formulaBasicSection")
        self.assertEqual(self.dialog.editor_section.objectName(), "formulaEditorSection")
        self.assertEqual(
            self.dialog.validation_section.objectName(), "formulaValidationSection"
        )
        for section in (
            self.dialog.basic_section,
            self.dialog.editor_section,
            self.dialog.validation_section,
        ):
            self.assertEqual(section.property("uiFormulaSection"), "true")
        self.assertEqual(self.dialog.generate_button.property("uiRole"), "primary")
        for button in (
            self.dialog.validate_button,
            self.dialog.preview_button,
            self.dialog.cancel_button,
            self.dialog.insert_signal_button,
        ):
            self.assertEqual(button.property("uiRole"), "secondary")
        self.assertIn(DEFAULT_THEME.colors.primary, self.dialog.styleSheet())

    def test_search_select_insert_and_long_name_tooltip(self):
        self.dialog.search_edit.setText("longitudinal")
        visible = [
            self.dialog.signal_list.item(index)
            for index in range(self.dialog.signal_list.count())
            if not self.dialog.signal_list.item(index).isHidden()
        ]
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0].data(SIGNAL_KEY_ROLE), "acceleration")
        self.assertIn(self.long_name, visible[0].toolTip())
        self.dialog.signal_list.setCurrentItem(visible[0])
        self.dialog.insert_signal_button.click()
        self.assertEqual(self.dialog.internal_formula(), "S001")
        self.assertEqual(self.dialog.binding_list.count(), 1)
        self.assertEqual(
            self.dialog.binding_list.item(0).toolTip(), visible[0].toolTip()
        )

    def test_function_validation_preview_and_generate_entries(self):
        self.dialog.name_edit.setText("Speed root")
        self.dialog.function_buttons["sqrt"].click()
        self.dialog.signal_list.setCurrentItem(self._item("speed"))
        self.dialog.insert_signal_button.click()
        self.assertEqual(self.dialog.internal_formula(), "sqrt(S001)")

        self.dialog.validate_button.click()
        self.assertIn("公式有效", self.dialog.diagnostic_output.toPlainText())
        self.dialog.preview_button.click()
        self.assertIn("预览成功", self.dialog.diagnostic_output.toPlainText())
        self.dialog.generate_button.click()
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Accepted)

    def test_resize_keeps_core_controls_usable_without_horizontal_list_scroll(self):
        self.dialog.show()
        self.dialog.resize(self.dialog.minimumSize())
        APP.processEvents()
        small_editor_width = self.dialog.formula_edit.width()
        self.assertTrue(self.dialog.formula_edit.isVisible())
        self.assertTrue(self.dialog.signal_list.isVisible())
        self.assertTrue(self.dialog.binding_list.isVisible())
        self.assertTrue(self.dialog.generate_button.isVisible())
        self.assertTrue(all(size > 0 for size in self.dialog.signal_splitter.sizes()))
        self.assertEqual(
            self.dialog.signal_list.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )

        self.dialog.resize(1100, 850)
        APP.processEvents()
        self.assertGreater(self.dialog.formula_edit.width(), small_editor_width)
        self.assertGreater(self.dialog.editor_section.height(), 0)
        self.assertFalse(
            self.dialog.basic_section.geometry().intersects(
                self.dialog.validation_section.geometry()
            )
        )

    def test_formula_atom_semantic_colors_are_unchanged(self):
        self.dialog.insert_signal_key("speed")
        cursor = QTextCursor(self.dialog.formula_edit.document())
        cursor.setPosition(0)
        cursor.movePosition(QTextCursor.MoveOperation.Right)
        char_format = cursor.charFormat()
        self.assertEqual(char_format.background().color(), QColor("#d9ecff"))
        self.assertEqual(char_format.foreground().color(), QColor("#0b4f87"))

    def test_busy_duplicate_stale_and_close_chain_remain_guarded(self):
        release = threading.Event()
        context = [1]

        def calculate(_definition):
            release.wait(2)
            return _result()

        dialog = FormulaEditorDialog(
            {"speed": self.signals["speed"]},
            preview_callback=calculate,
            context_callback=lambda: context[0],
        )
        try:
            dialog.name_edit.setText("Background")
            dialog.insert_signal_key("speed")
            definition = dialog.build_definition("__generate__")
            first = dialog._start_calculation(definition, "generate")
            second = dialog._start_calculation(definition, "generate")
            self.assertIs(first, second)
            self.assertFalse(dialog.generate_button.isEnabled())
            self.assertFalse(dialog.preview_button.isEnabled())
            context[0] = 2
            dialog.reject()
            release.set()
            _wait(lambda: dialog._calculation_worker is None)
            self.assertTrue(dialog._closed_during_calculation)
            self.assertIsNone(dialog.generated_result)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        finally:
            release.set()
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
