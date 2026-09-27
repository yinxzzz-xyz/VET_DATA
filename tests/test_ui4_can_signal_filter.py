import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QApplication, QDialog, QLabel

from vet_data_modular import baseline
from vet_data_modular.theme import DEFAULT_THEME
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


def _signal(name, comment=""):
    return SimpleNamespace(
        name=name,
        comment=comment,
        is_little_endian=True,
        length=16,
    )


def _message(frame_id, name, signals):
    return SimpleNamespace(frame_id=frame_id, name=name, signals=signals)


class CanConfigurationUi4Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = QSettings(
            os.path.join(self.temporary.name, "ui4.ini"),
            QSettings.Format.IniFormat,
        )
        self.window = MDFPlotter(gui_settings=self.settings)
        self.window.can_bus_data = {
            1: {
                "name": "Powertrain bus with a deliberately long name",
                "actual_ids": [0x100, 0x101],
                "id_count": 2,
                "msg_count": 3,
                "has_fd": True,
            }
        }
        with patch.object(baseline.QTimer, "singleShot"):
            self.window.create_bus_config_ui()
        self.row = self.window.bus_config_widgets[1]

    def tearDown(self):
        self.window.deleteLater()
        APP.processEvents()
        self.temporary.cleanup()

    def test_column_structure_and_actions_remain_locatable(self):
        headers = [
            item.text() for item in self.window.bus_header.findChildren(QLabel)
        ]
        self.assertEqual(headers, ["Bus", "CAN类型", "ID数量", "协议状态", "操作", "状态"])
        self.assertEqual(self.row.id_label.text(), "Bus 1")
        self.assertEqual(self.row.type_label.text(), "CANFD")
        self.assertEqual(self.row.id_count_label.text(), "2 IDs")
        self.assertEqual(self.row.select_protocol_btn.property("uiRole"), "secondary")
        self.assertEqual(self.row.parse_btn.property("uiRole"), "primary")
        self.assertEqual(self.row.protocol_clear_btn.property("uiRole"), "secondary")

        requested = []
        renamed = []
        self.row.parse_requested.disconnect()
        self.row.parse_requested.connect(requested.append)
        self.row.bus_id_changed.connect(lambda bus, name: renamed.append((bus, name)))
        self.window.collapsible_groups["CAN通道配置"].setChecked(True)
        self.row.parse_btn.click()
        self.row.name_edit.setText("PT CAN")
        self.assertEqual(requested, [1])
        self.assertEqual(renamed[-1], (1, "PT CAN"))

    def test_reasonable_width_needs_no_horizontal_scroll(self):
        self.window.resize(1280, 820)
        self.window.main_splitter.setSizes([440, 840])
        self.window.collapsible_groups["CAN通道配置"].setChecked(True)
        self.window.show()
        APP.processEvents()
        self.assertGreaterEqual(self.window.bus_scroll.viewport().width(), 300)
        self.assertEqual(self.window.bus_scroll.horizontalScrollBar().maximum(), 0)

    def test_extreme_width_degrades_to_scroll_without_overlapping_actions(self):
        self.window.resize(850, 700)
        self.window.main_splitter.setSizes([320, 530])
        self.window.collapsible_groups["CAN通道配置"].setChecked(True)
        self.window.show()
        APP.processEvents()
        buttons = (
            self.row.select_protocol_btn,
            self.row.protocol_clear_btn,
            self.row.parse_btn,
        )
        self.assertTrue(all(button.width() > 0 for button in buttons))
        self.assertFalse(buttons[0].geometry().intersects(buttons[2].geometry()))
        self.assertEqual(
            self.window.bus_scroll.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAsNeeded,
        )

    def test_semantic_status_changes_only_presentation(self):
        self.row.protocol_path = "stable.dbc"
        for state, text in (
            ("idle", "就绪"),
            ("loading", "解析中"),
            ("success", "完成"),
            ("error", "失败"),
        ):
            self.row.set_status(state)
            self.assertEqual(self.row.status_indicator.text(), text)
            self.assertEqual(self.row.status_indicator.property("statusKind"), state)
            self.assertEqual(self.row.protocol_path, "stable.dbc")
        self.row.set_protocol_display("a_very_long_protocol_database_name.dbc")
        self.assertEqual(self.row.protocol_label.property("statusKind"), "configured")
        self.assertEqual(
            self.row.protocol_label.toolTip(),
            "a_very_long_protocol_database_name.dbc",
        )
        self.assertEqual(self.window.arxml2dbc_btn.property("uiRole"), "secondary")


class SignalFilterDialogUi4Tests(unittest.TestCase):
    def setUp(self):
        self.long_signal = "VehicleSpeedFilteredWithAnExtremelyLongEngineeringName"
        self.long_comment = "A complete diagnostic comment that must remain accessible in a tooltip"
        self.db = SimpleNamespace(messages=[
            _message(0x100, "PowertrainStatusFrame", [
                _signal(self.long_signal, self.long_comment),
                _signal("EngineSpeed", "rpm"),
            ]),
            _message(0x200, "BodyFrame", [_signal("DoorOpen", "door")]),
        ])
        self.dialog = baseline.SignalFilterDialog(
            self.db, "Bus 1", existing_ids={0x100}
        )

    def tearDown(self):
        self.dialog.deleteLater()
        APP.processEvents()

    def test_theme_roles_and_long_values_are_accessible(self):
        self.assertEqual(self.dialog.select_btn.property("uiRole"), "primary")
        self.assertEqual(self.dialog.cancel_btn.property("uiRole"), "secondary")
        self.assertEqual(self.dialog.select_all_btn.property("uiRole"), "secondary")
        self.assertEqual(self.dialog.deselect_all_btn.property("uiRole"), "secondary")
        self.assertNotEqual(self.dialog.deselect_all_btn.property("uiRole"), "danger")
        self.assertIn(DEFAULT_THEME.colors.primary, self.dialog.styleSheet())
        item = self.dialog.tree.topLevelItem(0).child(0)
        self.assertEqual(item.toolTip(0), self.long_signal)
        self.assertEqual(item.toolTip(2), "PowertrainStatusFrame")
        self.assertEqual(item.toolTip(5), self.long_comment)

    def test_filter_expand_select_cancel_and_confirm_semantics(self):
        frame = self.dialog.tree.topLevelItem(0)
        self.assertFalse(frame.isExpanded())
        self.dialog.on_item_clicked(frame, 2)
        self.assertTrue(frame.isExpanded())

        self.dialog.search_box.setText("DoorOpen")
        self.assertEqual(self.dialog.tree.topLevelItemCount(), 1)
        self.assertIn("BodyFrame", self.dialog.tree.topLevelItem(0).text(0))
        self.dialog.select_all_btn.click()
        selected = self.dialog.get_selected_signals_by_frame()
        self.assertEqual(set(selected), {0x100, 0x200})
        self.assertEqual(sum(map(len, selected.values())), 3)
        self.assertEqual(self.dialog.selection_count_label.text(), "已选: 3 / 3")

        self.dialog.deselect_all_btn.click()
        self.assertEqual(self.dialog.get_selected_signals_by_frame(), {})
        self.dialog.search_box.clear()
        first_signal = self.dialog.tree.topLevelItem(0).child(0)
        self.dialog.on_item_clicked(first_signal, 2)
        self.assertEqual(
            self.dialog.get_selected_signals_by_frame(),
            {0x100: [self.long_signal]},
        )
        self.dialog.select_btn.click()
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Accepted)


if __name__ == "__main__":
    unittest.main()
