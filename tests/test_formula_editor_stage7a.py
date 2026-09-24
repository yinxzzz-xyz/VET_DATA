import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from vet_data_modular.formula_editor import FormulaEditorDialog


APP = QApplication.instance() or QApplication([])


def _info(name, display=None, group=-1, channel=-1, source="Data"):
    return {
        "name": name, "display_name": display or name, "group": group,
        "channel": channel, "source": source, "comment": "test",
    }


class FormulaEditorStage7ATests(unittest.TestCase):
    def setUp(self):
        self.signals = {
            "accel_G1_C0": _info("isAccelActuPos", "isAccelActuPos", 1, 0),
            "speed_G1_C0": _info("VehSpd", "VehSpd", 1, 0),
            "speed_G8_C2": _info("VehSpd", "VehSpd", 8, 2),
            "torque": _info("Torque", "Torque", source="CSV"),
        }
        self.dialog = FormulaEditorDialog(self.signals)
        self.dialog.show()
        APP.processEvents()

    def tearDown(self):
        self.dialog.hide()
        self.dialog.deleteLater()
        APP.processEvents()

    def _insert_expression(self, parts):
        self.dialog.formula_edit.clear()
        for part in parts:
            if isinstance(part, tuple):
                self.dialog.insert_signal_key(part[0])
            else:
                self.dialog.formula_edit.insert_normal_text(part)

    def test_visible_name_is_atomic_while_internal_formula_uses_token(self):
        self.dialog.insert_signal_key("accel_G1_C0")
        self.assertEqual(self.dialog.formula_edit.toPlainText(), "isAccelActuPos")
        self.assertNotIn("S001", self.dialog.formula_edit.toPlainText())
        self.assertEqual(self.dialog.internal_formula(), "S001")
        validated = self.dialog.validate_formula()
        self.assertEqual(validated.normalized_formula, "S001")

    def test_cursor_cannot_remain_inside_signal(self):
        self.dialog.insert_signal_key("accel_G1_C0")
        cursor = self.dialog.formula_edit.textCursor()
        cursor.setPosition(5)
        self.dialog.formula_edit.setTextCursor(cursor)
        APP.processEvents()
        self.assertIn(
            self.dialog.formula_edit.textCursor().position(),
            (0, len("isAccelActuPos")),
        )

    def test_backspace_requires_select_then_delete_whole_signal(self):
        self.dialog.insert_signal_key("speed_G1_C0")
        edit = self.dialog.formula_edit
        cursor = edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        edit.setTextCursor(cursor)
        QTest.keyClick(edit, Qt.Key.Key_Backspace)
        self.assertEqual(edit.textCursor().selectedText(), "VehSpd")
        self.assertEqual(edit.toPlainText(), "VehSpd")
        QTest.keyClick(edit, Qt.Key.Key_Backspace)
        self.assertEqual(edit.toPlainText(), "")
        self.assertEqual(self.dialog.binding_list.count(), 0)

    def test_delete_requires_select_then_delete_whole_signal(self):
        self.dialog.insert_signal_key("speed_G1_C0")
        edit = self.dialog.formula_edit
        cursor = edit.textCursor()
        cursor.setPosition(0)
        edit.setTextCursor(cursor)
        QTest.keyClick(edit, Qt.Key.Key_Delete)
        self.assertEqual(edit.textCursor().selectedText(), "VehSpd")
        self.assertEqual(edit.toPlainText(), "VehSpd")
        QTest.keyClick(edit, Qt.Key.Key_Delete)
        self.assertEqual(edit.toPlainText(), "")

    def test_mouse_click_selects_the_whole_signal(self):
        self.dialog.insert_signal_key("accel_G1_C0")
        edit = self.dialog.formula_edit
        probe = QTextCursor(edit.document())
        probe.setPosition(5)
        rect = edit.cursorRect(probe)
        QTest.mouseClick(edit.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        APP.processEvents()
        self.assertEqual(edit.textCursor().selectedText(), "isAccelActuPos")

    def test_repeated_signal_reuses_token_and_selected_panel_tracks_occurrences(self):
        self._insert_expression([("speed_G1_C0",), " + ", ("speed_G1_C0",)])
        self.assertEqual(self.dialog.formula_edit.toPlainText(), "VehSpd + VehSpd")
        self.assertEqual(self.dialog.internal_formula(), "S001 + S001")
        self.assertEqual(self.dialog.signal_tokens, {"S001": "speed_G1_C0"})
        self.assertEqual(self.dialog.binding_list.count(), 1)

        edit = self.dialog.formula_edit
        cursor = edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        edit.setTextCursor(cursor)
        QTest.keyClick(edit, Qt.Key.Key_Backspace)
        QTest.keyClick(edit, Qt.Key.Key_Backspace)
        self.assertEqual(self.dialog.binding_list.count(), 1)
        cursor = edit.textCursor()
        cursor.setPosition(len("VehSpd"))
        edit.setTextCursor(cursor)
        QTest.keyClick(edit, Qt.Key.Key_Backspace)
        QTest.keyClick(edit, Qt.Key.Key_Backspace)
        self.assertEqual(self.dialog.binding_list.count(), 0)

    def test_same_name_signals_get_minimal_disambiguation_and_distinct_tokens(self):
        self._insert_expression([("speed_G1_C0",), " + ", ("speed_G8_C2",)])
        visible = self.dialog.formula_edit.toPlainText()
        self.assertEqual(visible, "VehSpd [G1:C0] + VehSpd [G8:C2]")
        self.assertEqual(self.dialog.internal_formula(), "S001 + S002")
        self.assertEqual(self.dialog.binding_list.count(), 2)
        selected = [self.dialog.binding_list.item(i).text() for i in range(2)]
        self.assertTrue(any("Group: 1" in item and "Key: speed_G1_C0" in item for item in selected))
        self.assertTrue(any("Group: 8" in item and "Key: speed_G8_C2" in item for item in selected))

    def test_selected_signals_title_metadata_dedup_and_synchronization(self):
        self.assertEqual(self.dialog.binding_title.text(), "已选信号：")
        self._insert_expression([("accel_G1_C0",), " + ", ("accel_G1_C0",), " * ", ("torque",)])
        self.assertEqual(self.dialog.binding_list.count(), 2)
        details = [self.dialog.binding_list.item(i).text() for i in range(2)]
        self.assertTrue(any("Group: 1" in item and "Channel: 0" in item for item in details))
        self.assertTrue(any("Source: CSV" in item and "Key: torque" in item for item in details))

    def test_visual_formulas_convert_and_validate_through_existing_parser(self):
        cases = [
            ([ ("accel_G1_C0",), "+", ("torque",) ], "S001+S002"),
            ([ "(", ("accel_G1_C0",), "+", ("torque",), ")^2/", ("speed_G8_C2",) ], "(S001+S002)^2/S003"),
            ([ "sqrt(abs(", ("accel_G1_C0",), "-", ("torque",), "))" ], "sqrt(abs(S001-S002))"),
            ([ "derivative(", ("accel_G1_C0",), ")" ], "derivative(S001)"),
            ([ "integral(", ("accel_G1_C0",), "-", ("torque",), ")" ], "integral(S001-S002)"),
        ]
        for parts, internal in cases:
            with self.subTest(internal=internal):
                self.dialog = FormulaEditorDialog(self.signals)
                self._insert_expression(parts)
                self.assertEqual(self.dialog.internal_formula(), internal)
                self.assertIsNotNone(self.dialog.validate_formula())

    def test_internal_copy_paste_preserves_signal_binding(self):
        self.dialog.insert_signal_key("speed_G1_C0")
        edit = self.dialog.formula_edit
        edit.selectAll()
        QTest.keyClick(edit, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        cursor = edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.clearSelection()
        edit.setTextCursor(cursor)
        edit.insert_normal_text(" + ")
        QTest.keyClick(edit, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        APP.processEvents()
        self.assertEqual(edit.toPlainText(), "VehSpd + VehSpd")
        self.assertEqual(edit.token_formula(), "S001 + S001")
        self.assertEqual(self.dialog.binding_list.count(), 1)

    def test_plain_formula_text_remains_normally_editable(self):
        edit = self.dialog.formula_edit
        QTest.keyClicks(edit, "sqrt() * 2")
        cursor = edit.textCursor()
        cursor.setPosition(5)
        edit.setTextCursor(cursor)
        QTest.keyClicks(edit, "3")
        self.assertEqual(edit.toPlainText(), "sqrt(3) * 2")
        edit.undo()
        self.assertEqual(edit.toPlainText(), "sqrt() * 2")
        edit.redo()
        self.assertEqual(edit.toPlainText(), "sqrt(3) * 2")


if __name__ == "__main__":
    unittest.main()
