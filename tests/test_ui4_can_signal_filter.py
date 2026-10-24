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
        self.window.left_tabs.setCurrentWidget(self.window.can_tab_page)
        self.row = self.window.bus_config_widgets[1]

    def tearDown(self):
        self.window.deleteLater()
        APP.processEvents()
        self.temporary.cleanup()

    def test_channel_card_fields_and_actions_remain_locatable(self):
        self.assertIsNone(self.window.bus_header)
        self.assertEqual(self.row.card_group.objectName(), "busConfigCard1")
        self.assertEqual(self.row.id_label.text(), "Bus 1")
        self.assertEqual(self.row.type_label.text(), "CANFD")
        self.assertEqual(self.row.id_count_label.text(), "2 IDs")
        self.assertEqual(self.row.select_protocol_btn.property("uiRole"), "secondary")
        self.assertEqual(self.row.parse_btn.property("uiRole"), "primary")
        self.assertEqual(self.row.protocol_clear_btn.property("uiRole"), "secondary")
        self.assertEqual(self.row.protocol_clear_btn.text(), "清除")
        self.assertFalse(self.row.protocol_clear_btn.isEnabled())
        self.assertGreaterEqual(self.row.layout().count(), 1)
        self.assertEqual(self.row.protocol_label.alignment(), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        requested = []
        renamed = []
        self.row.parse_requested.disconnect()
        self.row.parse_requested.connect(requested.append)
        self.row.bus_id_changed.connect(lambda bus, name: renamed.append((bus, name)))
        self.row.parse_btn.click()
        self.row.name_edit.setText("PT CAN")
        self.assertEqual(requested, [1])
        self.assertEqual(renamed[-1], (1, "PT CAN"))

    def test_reasonable_width_needs_no_horizontal_scroll(self):
        self.window.resize(1280, 820)
        self.window.show()
        self.window.main_splitter.setSizes([440, 840])
        APP.processEvents()
        self.assertGreater(
            self.row.protocol_clear_btn.geometry().x(),
            self.row.parse_btn.geometry().x(),
        )
        self.assertGreaterEqual(self.window.bus_scroll.viewport().width(), 300)
        self.assertEqual(self.window.bus_scroll.horizontalScrollBar().maximum(), 0)

    def test_extreme_width_degrades_to_scroll_without_overlapping_actions(self):
        self.window.resize(850, 700)
        self.window.show()
        self.window.main_splitter.setSizes([320, 530])
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
            ("stopped", "解析停止"),
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

    def test_cancel_stops_worker_and_updates_bus_status(self):
        class Worker:
            def __init__(self):
                self.stopped = False

            def isRunning(self):
                return True

            def stop(self):
                self.stopped = True

        worker = Worker()
        dialog = baseline.ParseProgressDialog(1, self.window)
        self.window.bus_protocols[1] = "bus1.dbc"
        self.window.parse_workers[1] = worker
        self.window._parsing_bus_ids.add(1)
        self.window.progress_dialogs[1] = dialog
        dialog.cancel_requested.connect(self.window.cancel_parse_bus)
        dialog.on_cancel()

        self.assertTrue(worker.stopped)
        self.assertEqual(self.window.bus_config_widgets[1].status_indicator.text(), "解析停止")
        self.assertEqual(self.window.can_bus_data[1]["parse_state"], "stopped")
        self.assertIn(1, self.window._cancelled_parse_buses)
        self.window.on_parse_cancelled(1)
        self.assertEqual(dialog.status_label.text(), "⏹ 解析停止")
        with patch.object(self.window, "register_can_signals") as register:
            self.window.on_parse_finished(1, {"pool": {"late": {"t": [0], "v": [1]}}})
        register.assert_not_called()
        self.assertEqual(self.window.bus_config_widgets[1].status_indicator.text(), "解析停止")
        dialog.deleteLater()

    def test_clear_protocol_removes_only_its_bus_and_stops_active_parse(self):
        class Worker:
            def __init__(self):
                self.stopped = False

            def isRunning(self):
                return True

            def stop(self):
                self.stopped = True

        self.window.bus_protocols.update({1: "bus1.dbc", 2: "bus2.dbc"})
        self.window.db_per_bus.update({1: {"parsed": True}, 2: {"parsed": True}})
        self.window.can_bus_data[2] = {"name": "Bus 2", "parse_state": "complete"}
        self.window.signals.update({
            "CAN_bus1_speed": {"bus_id": 1},
            "CAN_bus2_speed": {"bus_id": 2},
        })
        self.window.can_parsed_data.update({
            "CAN_bus1_speed": {"samples": [1]},
            "CAN_bus2_speed": {"samples": [2]},
        })
        self.window.bus_signal_map.update({"CAN_bus1_speed": 1, "CAN_bus2_speed": 2})
        worker = Worker()
        self.window.parse_workers[1] = worker
        self.row.protocol_path = "bus1.dbc"
        self.row.protocol_label.setText("bus1.dbc")
        self.row.protocol_clear_btn.setEnabled(True)

        self.row.protocol_clear_btn.click()

        self.assertTrue(worker.stopped)
        self.assertNotIn(1, self.window.bus_protocols)
        self.assertEqual(self.row.protocol_label.text(), "未配置")
        self.assertFalse(self.row.protocol_clear_btn.isEnabled())
        self.assertNotIn("CAN_bus1_speed", self.window.signals)
        self.assertIn("CAN_bus2_speed", self.window.signals)
        self.assertNotIn("CAN_bus1_speed", self.window.can_parsed_data)
        self.assertIn("CAN_bus2_speed", self.window.can_parsed_data)
        self.assertEqual(self.window.can_bus_data[1]["parse_state"], "idle")
        self.assertEqual(self.window.db_per_bus[1], {})
        self.assertEqual(self.window.bus_config_widgets[1].status_indicator.text(), "就绪")
        with patch.object(self.window, "register_can_signals") as register:
            self.window.on_parse_finished(1, {"pool": {"late": {"t": [0], "v": [1]}}})
        register.assert_not_called()


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
        self.assertTrue(self.dialog._search_timer.isActive())
        self.assertFalse(self.dialog.frame_items[0x100].isHidden())
        self.dialog._apply_signal_filter()
        self.assertEqual(self.dialog.tree.topLevelItemCount(), 2)
        self.assertTrue(self.dialog.frame_items[0x100].isHidden())
        self.assertIn("BodyFrame", self.dialog.frame_items[0x200].text(0))
        self.assertFalse(self.dialog.frame_items[0x200].isHidden())
        self.dialog.select_all_btn.click()
        selected = self.dialog.get_selected_signals_by_frame()
        self.assertEqual(set(selected), {0x100, 0x200})
        self.assertEqual(sum(map(len, selected.values())), 3)
        self.assertEqual(self.dialog.selection_count_label.text(), "已选: 3 / 3")

        self.dialog.deselect_all_btn.click()
        self.assertEqual(self.dialog.get_selected_signals_by_frame(), {})
        self.dialog.search_box.clear()
        self.dialog._apply_signal_filter()
        first_signal = self.dialog.tree.topLevelItem(0).child(0)
        self.dialog.on_item_clicked(first_signal, 2)
        self.assertEqual(
            self.dialog.get_selected_signals_by_frame(),
            {0x100: [self.long_signal]},
        )
        self.dialog.select_btn.click()
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Accepted)

    def test_cached_filter_handles_large_list_and_preserves_selection(self):
        messages = [
            _message(index, f"Frame{index}", [
                _signal(f"Signal{index}_{signal_index}", f"comment {signal_index}")
                for signal_index in range(20)
            ])
            for index in range(250)
        ]
        dialog = baseline.SignalFilterDialog(
            SimpleNamespace(messages=messages), "Large bus"
        )
        try:
            item = dialog._signal_item_by_key["125_Signal125_7"]
            item.setCheckState(0, Qt.CheckState.Checked)
            dialog.search_box.setText("Signal125_7")
            dialog.search_box.setText("Signal125")
            dialog.search_box.setText("Signal125_7")
            self.assertEqual(dialog._search_pending_text, "Signal125_7")
            dialog._apply_signal_filter()
            self.assertIs(dialog._signal_item_by_key["125_Signal125_7"], item)
            self.assertFalse(item.isHidden())
            self.assertEqual(
                sum(not dialog.frame_items[frame_id].isHidden() for frame_id in dialog.frame_items),
                1,
            )
            self.assertTrue(dialog._selected_state["125_Signal125_7"])

            dialog.search_box.clear()
            self.assertEqual(dialog._search_pending_text, "")
            dialog._apply_signal_filter()
            self.assertFalse(item.isHidden())
            self.assertTrue(dialog._selected_state["125_Signal125_7"])
            self.assertEqual(len(dialog.signal_items), 5000)
        finally:
            dialog.deleteLater()
            APP.processEvents()

    def test_frame_checkbox_only_changes_signals_visible_in_filter(self):
        frame = self.dialog.frame_items[0x100]
        self.dialog.search_box.setText("EngineSpeed")
        self.dialog._apply_signal_filter()
        frame.setCheckState(0, Qt.CheckState.Checked)
        self.assertTrue(self.dialog._selected_state["256_EngineSpeed"])
        self.assertFalse(
            self.dialog._selected_state[
                f"256_{self.long_signal}"
            ]
        )


if __name__ == "__main__":
    unittest.main()
