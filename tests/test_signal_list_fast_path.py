import os
import time
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from vet_data_modular.signal_panel import COMMENT_ROLE, KEY_ROLE, NAME_ROLE
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


def signal_info(index, *, bus_id=None, display_name=None, comment=None):
    return {
        "name": f"raw_{index:05d}",
        "display_name": display_name or f"Signal {index:05d}",
        "comment": comment if comment is not None else f"comment {index % 10}",
        "bus_id": bus_id,
    }


class SignalListFastPathTests(unittest.TestCase):
    def setUp(self):
        self.window = MDFPlotter()
        self.window.resize(1000, 700)
        self.window.show()
        APP.processEvents()

    def tearDown(self):
        self.window.deleteLater()
        APP.processEvents()

    def populate(self, count):
        self.window.signals.update({
            f"sig_{index:05d}": signal_info(index)
            for index in range(count)
        })
        self.window.refresh_signal_list_ui()
        APP.processEvents()

    def items(self):
        return {
            self.window.signal_list_widget.item(row).data(KEY_ROLE):
            self.window.signal_list_widget.item(row)
            for row in range(self.window.signal_list_widget.count())
        }

    def test_empty_refresh_is_safe_and_second_refresh_is_noop(self):
        self.window.refresh_signal_list_ui()
        snapshot = self.window._last_signal_list_snapshot
        self.window.refresh_signal_list_ui()
        self.assertEqual(self.window.signal_list_widget.count(), 0)
        self.assertIs(self.window._last_signal_list_snapshot, snapshot)

    def test_noop_preserves_identity_at_one_and_one_hundred_signals(self):
        for count in (1, 100):
            with self.subTest(count=count):
                self.window.signals.clear()
                self.window.signals.update({
                    f"sig_{index:05d}": signal_info(index)
                    for index in range(count)
                })
                self.window.refresh_signal_list_ui()
                before = self.items()
                self.window.refresh_signal_list_ui()
                after = self.items()
                self.assertEqual(set(after), set(before))
                self.assertTrue(all(after[key] is item for key, item in before.items()))

    def test_noop_preserves_all_ui_state_and_indexes(self):
        self.populate(1000)
        items = self.items()
        target = items["sig_00500"]
        target.setCheckState(Qt.CheckState.Checked)
        self.window.signal_list_widget.setCurrentItem(target)
        bar = self.window.signal_list_widget.verticalScrollBar()
        bar.setValue(bar.maximum() // 2)
        scroll = bar.value()
        self.window.lat_combo.setCurrentIndex(self.window.lat_combo.findData("sig_00400"))
        self.window.lon_combo.setCurrentIndex(self.window.lon_combo.findData("sig_00600"))
        self.window._search_pending_text = "comment 1"
        self.window._apply_signal_filter()
        hidden = {key: item.isHidden() for key, item in items.items()}
        search_index = self.window.search_index
        lat_count = self.window.lat_combo.count()
        lon_count = self.window.lon_combo.count()
        snapshot = self.window._last_signal_list_snapshot

        self.window.refresh_signal_list_ui()

        after = self.items()
        self.assertEqual(set(after), set(items))
        self.assertTrue(all(after[key] is item for key, item in items.items()))
        self.assertIs(self.window.search_index, search_index)
        self.assertIs(self.window._last_signal_list_snapshot, snapshot)
        self.assertEqual(self.window.lat_combo.count(), lat_count)
        self.assertEqual(self.window.lon_combo.count(), lon_count)
        self.assertEqual(self.window.get_selected_signals(), ["sig_00500"])
        self.assertIs(self.window.signal_list_widget.currentItem(), target)
        self.assertEqual(bar.value(), scroll)
        self.assertEqual(self.window.lat_combo.currentData(), "sig_00400")
        self.assertEqual(self.window.lon_combo.currentData(), "sig_00600")
        self.assertEqual(
            {key: item.isHidden() for key, item in after.items()}, hidden
        )

    def test_noop_scales_without_recreating_ten_thousand_items(self):
        self.populate(10000)
        before = tuple(
            self.window.signal_list_widget.item(row)
            for row in range(self.window.signal_list_widget.count())
        )
        started = time.perf_counter()
        self.window.refresh_signal_list_ui()
        elapsed = time.perf_counter() - started
        after = tuple(
            self.window.signal_list_widget.item(row)
            for row in range(self.window.signal_list_widget.count())
        )
        self.assertTrue(all(left is right for left, right in zip(before, after)))
        self.assertLess(elapsed, 0.1)

    def test_single_metadata_update_preserves_identity_state_and_filter(self):
        self.window.signals.update({
            "a": signal_info(1, display_name="Alpha", comment="old comment"),
            "b": signal_info(2, display_name="Beta"),
            "c": signal_info(3, display_name="Charlie"),
        })
        self.window.refresh_signal_list_ui()
        items = self.items()
        target = items["c"]
        target.setCheckState(Qt.CheckState.Checked)
        self.window.signal_list_widget.setCurrentItem(target)
        self.window.lat_combo.setCurrentIndex(self.window.lat_combo.findData("c"))
        self.window.lon_combo.setCurrentIndex(self.window.lon_combo.findData("c"))
        self.window._search_pending_text = "renamed"
        self.window._apply_signal_filter()

        self.window.signals["c"]["display_name"] = "Aardvark Renamed"
        self.window.signals["c"]["comment"] = "new tooltip"
        self.assertTrue(self.window.update_signal_metadata_ui("c"))

        after = self.items()
        self.assertIs(after["c"], target)
        self.assertTrue(all(after[key] is items[key] for key in items))
        self.assertEqual(target.data(NAME_ROLE), "Aardvark Renamed")
        self.assertEqual(target.data(COMMENT_ROLE), "new tooltip")
        self.assertEqual(target.toolTip(), "new tooltip")
        self.assertEqual(target.checkState(), Qt.CheckState.Checked)
        self.assertIs(self.window.signal_list_widget.currentItem(), target)
        self.assertEqual(self.window.signal_list_widget.item(0).data(KEY_ROLE), "c")
        self.assertIn("aardvark renamed", self.window.search_index["c"])
        self.assertNotIn("charlie", self.window.search_index["c"])
        self.assertFalse(target.isHidden())
        self.assertEqual(self.window.lat_combo.currentData(), "c")
        self.assertEqual(self.window.lon_combo.currentData(), "c")
        self.assertEqual(
            self.window.lat_combo.itemText(self.window.lat_combo.findData("c")),
            "Aardvark Renamed",
        )

    def test_full_collection_changes_still_rebuild(self):
        self.populate(3)
        old = self.items()
        self.window.signals["added"] = signal_info(10, display_name="Added")
        self.window.refresh_signal_list_ui()
        after_add = self.items()
        self.assertIn("added", after_add)
        self.assertIsNot(after_add["sig_00000"], old["sig_00000"])

        del self.window.signals["sig_00001"]
        previous = after_add["sig_00000"]
        self.window.refresh_signal_list_ui()
        after_delete = self.items()
        self.assertNotIn("sig_00001", after_delete)
        self.assertIsNot(after_delete["sig_00000"], previous)

    def test_bus_rename_batch_scales_from_one_to_many_signals(self):
        for count in (1, 1000):
            with self.subTest(count=count):
                self.window.signals.clear()
                self.window.can_bus_data[1] = {"name": "Old"}
                self.window.signals.update({
                    f"CAN_1_S{index:05d}": signal_info(
                        index, bus_id=1, display_name=f"S{index:05d}"
                    )
                    for index in range(count)
                })
                self.window.refresh_signal_list_ui()
                before = self.items()
                original_refresh = self.window.refresh_signal_list_ui
                self.window.refresh_signal_list_ui = Mock(wraps=original_refresh)
                self.window.on_bus_name_changed(1, "Renamed")
                self.window.refresh_signal_list_ui.assert_not_called()
                self.window.refresh_signal_list_ui = original_refresh
                after = self.items()
                self.assertTrue(all(after[key] is item for key, item in before.items()))
                self.assertTrue(all("renamed" in item.data(NAME_ROLE).lower() for item in after.values()))

    def test_bus_rename_batch_updates_only_target_bus_without_full_refresh(self):
        self.window.can_bus_data.update({
            1: {"name": "Front"},
            2: {"name": "Rear"},
        })
        for index in range(100):
            key = f"CAN_1_S{index:03d}"
            self.window.signals[key] = signal_info(index, bus_id=1, display_name=f"S{index:03d}")
        for index in range(20):
            key = f"CAN_2_T{index:03d}"
            self.window.signals[key] = signal_info(index, bus_id=2, display_name=f"T{index:03d}")
        self.window.refresh_signal_list_ui()
        before = self.items()
        checked = before["CAN_1_S010"]
        checked.setCheckState(Qt.CheckState.Checked)
        self.window.signal_list_widget.setCurrentItem(checked)
        self.window.lat_combo.setCurrentIndex(self.window.lat_combo.findData("CAN_1_S010"))
        self.window.lon_combo.setCurrentIndex(self.window.lon_combo.findData("CAN_2_T010"))
        self.window._search_pending_text = "newbus"
        original_refresh = self.window.refresh_signal_list_ui
        self.window.refresh_signal_list_ui = Mock(wraps=original_refresh)

        for name in ("N", "Ne", "New", "NewBus"):
            self.window.on_bus_name_changed(1, name)

        self.window.refresh_signal_list_ui.assert_not_called()
        after = self.items()
        self.assertEqual(len(after), 120)
        self.assertTrue(all(after[key] is item for key, item in before.items()))
        self.assertEqual(checked.checkState(), Qt.CheckState.Checked)
        self.assertIs(self.window.signal_list_widget.currentItem(), checked)
        self.assertEqual(self.window.lat_combo.currentData(), "CAN_1_S010")
        self.assertEqual(self.window.lon_combo.currentData(), "CAN_2_T010")
        self.assertIn("newbus", checked.data(NAME_ROLE).lower())
        self.assertFalse(checked.isHidden())
        other = after["CAN_2_T010"]
        self.assertNotIn("newbus", other.data(NAME_ROLE).lower())
        self.assertEqual(
            len({self.window.lat_combo.itemData(i) for i in range(self.window.lat_combo.count())}),
            120,
        )
        self.assertEqual(
            len({self.window.lon_combo.itemData(i) for i in range(self.window.lon_combo.count())}),
            120,
        )


if __name__ == "__main__":
    unittest.main()
