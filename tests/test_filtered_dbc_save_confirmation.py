import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from vet_data_modular import baseline


APP = QApplication.instance() or QApplication([])


class FilteredDbcSaveConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.widget = baseline.BusConfigWidget(1)
        self.source_path = "C:/data/PTCAN.dbc"
        self.filtered_db = object()

    def tearDown(self):
        self.widget.deleteLater()
        APP.processEvents()

    def test_no_keeps_filtered_database_in_memory_without_save_dialog(self):
        with patch.object(
            baseline.QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.No,
        ) as question, patch.object(
            baseline.QFileDialog,
            "getSaveFileName",
            side_effect=AssertionError("save dialog must not open"),
        ):
            self.widget._offer_save_filtered_dbc(
                self.filtered_db, self.source_path
            )

        question.assert_called_once()
        self.assertEqual(self.widget.protocol_path, self.source_path)
        self.assertIn("已筛选-内存", self.widget.protocol_label.text())
        self.assertIsNone(self.widget._filtered_dbc_path)

    def test_yes_opens_existing_filtered_dbc_save_flow(self):
        with patch.object(
            baseline.QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ), patch.object(
            self.widget, "_save_filtered_dbc_with_custom_name"
        ) as save_filtered:
            self.widget._offer_save_filtered_dbc(
                self.filtered_db, self.source_path
            )

        save_filtered.assert_called_once_with(
            self.filtered_db, self.source_path
        )

    def test_confirmation_defaults_to_not_saving(self):
        with patch.object(
            baseline.QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.No,
        ) as question:
            self.widget._offer_save_filtered_dbc(
                self.filtered_db, self.source_path
            )

        args = question.call_args.args
        self.assertEqual(args[4], QMessageBox.StandardButton.No)
        self.assertIn("不会修改原 DBC", args[2])


if __name__ == "__main__":
    unittest.main()
