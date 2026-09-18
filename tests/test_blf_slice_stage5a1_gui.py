import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QComboBox

from vet_data_modular.blf_slice_dialog import (
    OTHER_OFFSET, SPECIAL_UTC_OFFSET_MINUTES, BlfSliceDialog,
    populate_offset_combo, populate_special_offset_combo, selected_offset,
)


APP = QApplication.instance() or QApplication([])


class Stage5a1TimezoneTests(unittest.TestCase):
    def test_main_list_contains_only_whole_hours_then_other(self):
        main, special = QComboBox(), QComboBox()
        populate_special_offset_combo(special)
        populate_offset_combo(main, special)
        values = [main.itemData(index) for index in range(main.count())]
        self.assertEqual(values[:-1], list(range(-720, 841, 60)))
        self.assertEqual(values[-1], OTHER_OFFSET)
        self.assertEqual(main.itemText(main.count() - 1), "其他...")

    def test_special_list_is_explicit_and_non_hour_only(self):
        combo = QComboBox()
        populate_special_offset_combo(combo)
        values = tuple(combo.itemData(index) for index in range(combo.count()))
        self.assertEqual(values, SPECIAL_UTC_OFFSET_MINUTES)
        self.assertTrue(all(minutes % 60 for minutes in values))
        self.assertIn(345, values)
        self.assertIn(-210, values)

    def test_whole_and_special_offsets_are_selected_without_time_conversion_logic(self):
        main, special = QComboBox(), QComboBox()
        populate_special_offset_combo(special)
        populate_offset_combo(main, special)
        main.setCurrentIndex(main.findData(8 * 60))
        self.assertEqual(selected_offset(main, special), timedelta(hours=8))
        main.setCurrentIndex(main.findData(OTHER_OFFSET))
        special.setCurrentIndex(special.findData(345))
        self.assertEqual(selected_offset(main, special), timedelta(hours=5, minutes=45))

    def test_existing_special_default_is_preserved(self):
        main, special = QComboBox(), QComboBox()
        populate_special_offset_combo(special)
        populate_offset_combo(main, special, timedelta(hours=5, minutes=45))
        self.assertEqual(main.currentData(), OTHER_OFFSET)
        self.assertEqual(special.currentData(), 345)
        self.assertEqual(selected_offset(main, special), timedelta(hours=5, minutes=45))

    def test_blf_and_table_timezone_controls_are_independent(self):
        dialog = BlfSliceDialog()
        dialog.blf_offset_combo.setCurrentIndex(dialog.blf_offset_combo.findData(OTHER_OFFSET))
        dialog.blf_special_offset_combo.setCurrentIndex(dialog.blf_special_offset_combo.findData(-210))
        dialog.table_offset_combo.setCurrentIndex(dialog.table_offset_combo.findData(9 * 60))
        self.assertEqual(selected_offset(dialog.blf_offset_combo, dialog.blf_special_offset_combo), timedelta(hours=-3, minutes=-30))
        self.assertEqual(selected_offset(dialog.table_offset_combo, dialog.table_special_offset_combo), timedelta(hours=9))
        self.assertFalse(dialog.blf_special_offset_combo.isHidden())
        self.assertTrue(dialog.table_special_offset_combo.isHidden())
        dialog.deleteLater()


class Stage5a1BatchWindowTests(unittest.TestCase):
    def _dialog_with_rows(self):
        temporary = tempfile.TemporaryDirectory()
        table = Path(temporary.name) / "conditions.csv"
        table.write_text(
            "记录时间,记录内容,向前秒数,向后秒数\n"
            "2026/9/17 10:30,cut 1,30,60\n"
            "2026/9/17 10:31,cut 2,45,90\n"
            "2026/9/17 10:32,cut 3,60,60\n",
            encoding="utf-8-sig",
        )
        dialog = BlfSliceDialog()
        dialog.table_edit.setText(str(table))
        dialog.load_condition_table(table)
        return temporary, dialog

    def test_apply_overwrites_all_rows_including_unchecked(self):
        temporary, dialog = self._dialog_with_rows()
        try:
            dialog.condition_table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
            dialog.batch_before_spin.setValue(60)
            dialog.batch_after_spin.setValue(120)
            dialog.apply_batch_button.click()
            self.assertEqual(
                [(dialog.condition_table.cellWidget(row, 4).value(), dialog.condition_table.cellWidget(row, 5).value()) for row in range(3)],
                [(60, 120), (60, 120), (60, 120)],
            )
        finally:
            dialog.deleteLater()
            temporary.cleanup()

    def test_batch_values_do_not_bind_and_rows_remain_editable(self):
        temporary, dialog = self._dialog_with_rows()
        try:
            original = dialog.condition_table.cellWidget(0, 4).value()
            dialog.batch_before_spin.setValue(100)
            self.assertEqual(dialog.condition_table.cellWidget(0, 4).value(), original)
            dialog.apply_batch_button.click()
            dialog.condition_table.cellWidget(0, 4).setValue(7)
            self.assertEqual(dialog.condition_table.cellWidget(0, 4).value(), 7)
            self.assertEqual(dialog.condition_table.cellWidget(1, 4).value(), 100)
        finally:
            dialog.deleteLater()
            temporary.cleanup()

    def test_empty_table_is_safe_and_uses_same_range_as_rows(self):
        dialog = BlfSliceDialog()
        self.assertEqual((dialog.batch_before_spin.minimum(), dialog.batch_before_spin.maximum()), (0, 300))
        self.assertEqual((dialog.batch_after_spin.minimum(), dialog.batch_after_spin.maximum()), (0, 300))
        dialog.apply_batch_button.click()
        self.assertEqual(dialog.condition_table.rowCount(), 0)
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
