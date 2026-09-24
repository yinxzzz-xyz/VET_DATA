import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from vet_data_modular.formula_editor import FormulaEditorDialog


APP = QApplication.instance() or QApplication([])


class AtomicFormulaCursorStage7A1Tests(unittest.TestCase):
    def setUp(self):
        self.signals = {
            "A": {"name": "A", "display_name": "A", "group": 1, "channel": 0},
            "B": {"name": "B", "display_name": "B", "group": 1, "channel": 1},
            "speed": {"name": "VehSpd", "display_name": "VehSpd", "group": 2, "channel": 0},
        }
        self.dialog = FormulaEditorDialog(self.signals)
        self.dialog.show()
        APP.processEvents()

    def tearDown(self):
        self.dialog.hide()
        self.dialog.deleteLater()
        APP.processEvents()

    def _single_signal(self, key="speed"):
        edit = self.dialog.formula_edit
        edit.clear()
        self.dialog.insert_signal_key(key)
        return edit

    @staticmethod
    def _set_position(edit, position):
        cursor = edit.textCursor()
        cursor.setPosition(position)
        edit.setTextCursor(cursor)
        APP.processEvents()

    def test_right_crosses_atom_from_left_and_left_crosses_from_right(self):
        edit = self._single_signal()
        self._set_position(edit, 0)
        QTest.keyClick(edit, Qt.Key.Key_Right)
        self.assertEqual(edit.textCursor().position(), len("VehSpd"))
        QTest.keyClick(edit, Qt.Key.Key_Left)
        self.assertEqual(edit.textCursor().position(), 0)
        self.assertEqual(edit.token_formula(), "S001")

    def test_repeated_arrow_navigation_never_enters_atom(self):
        edit = self._single_signal()
        positions = []
        for key in (Qt.Key.Key_Right, Qt.Key.Key_Left) * 4:
            QTest.keyClick(edit, key)
            positions.append(edit.textCursor().position())
        self.assertTrue(set(positions) <= {0, len("VehSpd")})
        self.assertEqual(edit.token_formula(), "S001")

    def test_mixed_text_and_two_atoms_traverse_naturally(self):
        edit = self.dialog.formula_edit
        edit.insert_normal_text("x + ")
        self.dialog.insert_signal_key("A")
        edit.insert_normal_text(" ")
        self.dialog.insert_signal_key("B")
        edit.insert_normal_text(" * z")
        self.assertEqual(edit.toPlainText(), "x + A B * z")
        self.assertEqual(edit.token_formula(), "x + S001 S002 * z")

        self._set_position(edit, len("x + A B"))
        QTest.keyClick(edit, Qt.Key.Key_Left)
        self.assertEqual(edit.textCursor().position(), len("x + A "))
        QTest.keyClick(edit, Qt.Key.Key_Left)
        self.assertEqual(edit.textCursor().position(), len("x + A"))
        QTest.keyClick(edit, Qt.Key.Key_Left)
        self.assertEqual(edit.textCursor().position(), len("x + "))
        QTest.keyClick(edit, Qt.Key.Key_Left)
        self.assertEqual(edit.textCursor().position(), len("x +"))
        self.assertEqual(edit.token_formula(), "x + S001 S002 * z")

    def test_operator_can_be_reentered_after_deleting_it_at_atom_boundary(self):
        for character in ("+", "-", "*", "/", "^"):
            with self.subTest(character=character):
                edit = self._single_signal()
                QTest.keyClicks(edit, character)
                self.assertEqual(edit.toPlainText(), f"VehSpd{character}")
                QTest.keyClick(edit, Qt.Key.Key_Backspace)
                self.assertEqual(edit.toPlainText(), "VehSpd")
                self.assertFalse(edit.textCursor().hasSelection())
                QTest.keyClicks(edit, character)
                self.assertEqual(edit.toPlainText(), f"VehSpd{character}")
                self.assertEqual(edit.token_formula(), f"S001{character}")

    def test_parentheses_and_numbers_insert_normally_at_atom_boundary(self):
        for text in ("(", ")", "1", "42"):
            with self.subTest(text=text):
                edit = self._single_signal()
                QTest.keyClicks(edit, text)
                self.assertEqual(edit.toPlainText(), f"VehSpd{text}")
                self.assertEqual(edit.token_formula(), f"S001{text}")

    def test_backspace_and_delete_two_step_behavior_remains(self):
        edit = self._single_signal()
        QTest.keyClick(edit, Qt.Key.Key_Backspace)
        self.assertEqual(edit.textCursor().selectedText(), "VehSpd")
        self.assertEqual(edit.toPlainText(), "VehSpd")
        QTest.keyClick(edit, Qt.Key.Key_Backspace)
        self.assertEqual(edit.toPlainText(), "")

        edit = self._single_signal()
        self._set_position(edit, 0)
        QTest.keyClick(edit, Qt.Key.Key_Delete)
        self.assertEqual(edit.textCursor().selectedText(), "VehSpd")
        QTest.keyClick(edit, Qt.Key.Key_Delete)
        self.assertEqual(edit.toPlainText(), "")

    def test_arrow_cancels_pending_delete_and_moves_to_requested_boundary(self):
        edit = self._single_signal()
        QTest.keyClick(edit, Qt.Key.Key_Backspace)
        self.assertTrue(edit.textCursor().hasSelection())
        QTest.keyClick(edit, Qt.Key.Key_Left)
        self.assertFalse(edit.textCursor().hasSelection())
        self.assertEqual(edit.textCursor().position(), 0)
        QTest.keyClick(edit, Qt.Key.Key_Right)
        self.assertEqual(edit.textCursor().position(), len("VehSpd"))
        self.assertEqual(edit.toPlainText(), "VehSpd")

        self._set_position(edit, 0)
        QTest.keyClick(edit, Qt.Key.Key_Delete)
        QTest.keyClick(edit, Qt.Key.Key_Right)
        self.assertFalse(edit.textCursor().hasSelection())
        self.assertEqual(edit.textCursor().position(), len("VehSpd"))
        self.assertEqual(edit.toPlainText(), "VehSpd")

    def test_normal_input_cancels_pending_delete_without_replacing_atom(self):
        edit = self._single_signal()
        QTest.keyClick(edit, Qt.Key.Key_Backspace)
        QTest.keyClicks(edit, "+")
        self.assertEqual(edit.toPlainText(), "VehSpd+")
        self.assertEqual(edit.token_formula(), "S001+")

        edit = self._single_signal()
        self._set_position(edit, 0)
        QTest.keyClick(edit, Qt.Key.Key_Delete)
        QTest.keyClicks(edit, "-")
        self.assertEqual(edit.toPlainText(), "-VehSpd")
        self.assertEqual(edit.token_formula(), "-S001")

    def test_mouse_selection_is_not_already_pending_delete(self):
        edit = self._single_signal()
        probe = QTextCursor(edit.document())
        probe.setPosition(2)
        QTest.mouseClick(
            edit.viewport(), Qt.MouseButton.LeftButton,
            pos=edit.cursorRect(probe).center(),
        )
        self.assertEqual(edit.textCursor().selectedText(), "VehSpd")
        QTest.keyClick(edit, Qt.Key.Key_Backspace)
        self.assertEqual(edit.toPlainText(), "VehSpd")
        self.assertEqual(edit.textCursor().selectedText(), "VehSpd")
        QTest.keyClick(edit, Qt.Key.Key_Backspace)
        self.assertEqual(edit.toPlainText(), "")

    def test_mouse_reposition_clears_pending_delete(self):
        edit = self.dialog.formula_edit
        edit.insert_normal_text("x + ")
        self.dialog.insert_signal_key("speed")
        QTest.keyClick(edit, Qt.Key.Key_Backspace)
        self.assertTrue(edit.textCursor().hasSelection())
        probe = QTextCursor(edit.document())
        probe.setPosition(1)
        point = edit.cursorRect(probe).center()
        QTest.mouseClick(edit.viewport(), Qt.MouseButton.LeftButton, pos=point)
        APP.processEvents()
        self.assertFalse(edit.textCursor().hasSelection())
        QTest.keyClicks(edit, "2")
        self.assertIn("2", edit.toPlainText())
        self.assertEqual(edit.token_formula().count("S001"), 1)


if __name__ == "__main__":
    unittest.main()
