import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QListWidget, QListWidgetItem, QStyle, QStyleOptionViewItem

from vet_data_modular.signal_panel import SELECTED_TEXT_COLOR, SignalItemDelegate


APP = QApplication.instance() or QApplication([])


class SignalPanelSelectionStyleTests(unittest.TestCase):
    def setUp(self):
        self.list_widget = QListWidget()
        self.delegate = SignalItemDelegate(self.list_widget)
        self.item = QListWidgetItem("")
        self.item.setFlags(self.item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        self.item.setCheckState(Qt.CheckState.Unchecked)
        self.list_widget.addItem(self.item)

    def tearDown(self):
        self.list_widget.deleteLater()
        APP.processEvents()

    def _option(self, *states):
        option = QStyleOptionViewItem()
        option.palette.setColor(option.palette.ColorRole.Text, QColor("#334455"))
        option.palette.setColor(option.palette.ColorRole.HighlightedText, QColor("#ffffff"))
        for state in states:
            option.state |= state
        return option

    def test_unselected_and_hover_text_use_normal_palette_color(self):
        expected = QColor("#334455")
        self.assertEqual(self.delegate.text_color(self._option()), expected)
        self.assertEqual(
            self.delegate.text_color(self._option(QStyle.StateFlag.State_MouseOver)),
            expected,
        )

    def test_selected_text_stays_dark_for_hover_and_keyboard_focus(self):
        for extra_state in (
            None,
            QStyle.StateFlag.State_MouseOver,
            QStyle.StateFlag.State_HasFocus,
        ):
            states = [QStyle.StateFlag.State_Selected]
            if extra_state is not None:
                states.append(extra_state)
            self.assertEqual(self.delegate.text_color(self._option(*states)), SELECTED_TEXT_COLOR)

    def test_selection_and_focus_do_not_change_checkbox_state(self):
        for check_state in (Qt.CheckState.Unchecked, Qt.CheckState.Checked):
            self.item.setCheckState(check_state)
            self.item.setSelected(True)
            self.list_widget.setCurrentItem(self.item)
            APP.processEvents()
            self.assertEqual(self.item.checkState(), check_state)
            self.item.setSelected(False)
            self.assertEqual(self.item.checkState(), check_state)


if __name__ == "__main__":
    unittest.main()
