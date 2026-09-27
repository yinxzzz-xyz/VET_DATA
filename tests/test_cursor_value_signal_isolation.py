import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QListWidgetItem

from vet_data_modular.signal_panel import (
    COMMENT_ROLE,
    KEY_ROLE,
    NAME_ROLE,
    VALUE_ROLE,
)
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


def numeric_signal(timestamps, samples):
    return SimpleNamespace(
        timestamps=np.asarray(timestamps, dtype=float),
        samples=np.asarray(samples, dtype=float),
        is_text_signal=False,
    )


class RaisingValueItem(QListWidgetItem):
    def __init__(self):
        super().__init__("")
        self.raise_value_write = False

    def setData(self, role, value):
        if role == VALUE_ROLE and self.raise_value_write:
            raise RuntimeError("value write failed")
        return super().setData(role, value)


class CursorValueSignalIsolationTests(unittest.TestCase):
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
            f"sig_{index}": {
                "name": f"sig_{index}",
                "display_name": f"Signal {index}",
                "comment": f"comment {index}",
                "bus_id": None,
            }
            for index in range(count)
        })
        self.window.refresh_signal_list_ui()
        for row in range(count):
            self.window.signal_list_widget.item(row).setCheckState(
                Qt.CheckState.Checked
            )
        self.window._set_view_mode("selected")
        self.window._mouse_in_plot = True

    def install_spies(self):
        original_selection = self.window._selection_changed
        original_filter = self.window._apply_signal_filter
        self.window._selection_changed = Mock(wraps=original_selection)
        self.window._apply_signal_filter = Mock(wraps=original_filter)

    def test_numeric_text_nan_and_timestamp_rules_are_unchanged(self):
        self.populate(3)
        numeric = numeric_signal([1, 2, 2, 3], [10, 20, 21, 30])
        text = SimpleNamespace(
            timestamps=np.asarray([1, 2, 3], dtype=float),
            samples=np.asarray([0, 1, 0], dtype=float),
            is_text_signal=True,
            raw_text_values=["OFF", "ON", "OFF"],
        )
        nan_signal = numeric_signal([1, 2, 3], [1, np.nan, 3])
        self.window._sig_value_cache.update({
            "sig_0": numeric,
            "sig_1": text,
            "sig_2": nan_signal,
        })
        self.install_spies()

        expectations = (
            (0.0, "10.000"),
            (2.0, "20.000"),
            (2.5, "30.000"),
            (9.0, "30.000"),
        )
        for cursor_time, expected in expectations:
            with self.subTest(cursor_time=cursor_time):
                self.window._current_cursor_time = cursor_time
                self.window._update_signal_list_values()
                self.assertEqual(
                    self.window.signal_widgets["sig_0"]["checkbox"].item.data(VALUE_ROLE),
                    expected,
                )
        self.window._current_cursor_time = 2.0
        self.window._update_signal_list_values()
        self.assertEqual(
            self.window.signal_widgets["sig_1"]["checkbox"].item.data(VALUE_ROLE),
            "ON",
        )
        self.assertEqual(
            self.window.signal_widgets["sig_2"]["checkbox"].item.data(VALUE_ROLE),
            "NaN",
        )
        self.window._selection_changed.assert_not_called()
        self.window._apply_signal_filter.assert_not_called()

    def test_value_update_preserves_selection_and_active_filter(self):
        self.populate(4)
        for key in self.window.signals:
            self.window._sig_value_cache[key] = numeric_signal([0, 1], [0, 1])
        self.window._search_pending_text = "signal 1"
        self.window._apply_signal_filter()
        hidden_before = [
            self.window.signal_list_widget.item(row).isHidden()
            for row in range(self.window.signal_list_widget.count())
        ]
        checked_before = self.window.get_selected_signals()
        count_text = self.window.view_selected_btn.text()
        self.install_spies()
        viewport = self.window.signal_list_widget.viewport()
        viewport.update = Mock(wraps=viewport.update)

        self.window._current_cursor_time = 1.0
        self.window._update_signal_list_values()

        self.assertEqual(viewport.update.call_count, 1)
        self.window._selection_changed.assert_not_called()
        self.window._apply_signal_filter.assert_not_called()
        self.assertEqual(self.window.get_selected_signals(), checked_before)
        self.assertEqual(self.window.view_selected_btn.text(), count_text)
        self.assertEqual(
            [self.window.signal_list_widget.item(row).isHidden()
             for row in range(self.window.signal_list_widget.count())],
            hidden_before,
        )

    def test_all_view_keeps_existing_no_value_update_behavior(self):
        self.populate(1)
        item = self.window.signal_list_widget.item(0)
        self.window._sig_value_cache["sig_0"] = numeric_signal([0, 1], [0, 1])
        self.window._set_view_mode("all")
        hidden = item.isHidden()
        self.install_spies()
        self.window._current_cursor_time = 1.0

        self.window._update_signal_list_values()

        self.assertIsNone(item.data(VALUE_ROLE))
        self.assertEqual(item.isHidden(), hidden)
        self.window._selection_changed.assert_not_called()
        self.window._apply_signal_filter.assert_not_called()

    def test_clear_values_is_isolated_and_clears_cache(self):
        self.populate(10)
        for row in range(10):
            self.window.signal_list_widget.item(row).setData(VALUE_ROLE, "1.000")
        self.window._sig_value_cache["sig_0"] = numeric_signal([0], [1])
        checked_before = self.window.get_selected_signals()
        self.install_spies()
        viewport = self.window.signal_list_widget.viewport()
        viewport.update = Mock(wraps=viewport.update)

        self.window._clear_signal_values()

        self.assertEqual(viewport.update.call_count, 1)
        self.window._selection_changed.assert_not_called()
        self.window._apply_signal_filter.assert_not_called()
        self.assertEqual(self.window.get_selected_signals(), checked_before)
        self.assertTrue(all(
            self.window.signal_list_widget.item(row).data(VALUE_ROLE) is None
            for row in range(10)
        ))
        self.assertEqual(self.window._sig_value_cache, {})

    def test_signal_blocker_restores_after_value_write_exception(self):
        self.populate(2)
        old = self.window.signal_list_widget.takeItem(0)
        bad = RaisingValueItem()
        for role in (KEY_ROLE, NAME_ROLE, COMMENT_ROLE):
            bad.setData(role, old.data(role))
        bad.setFlags(old.flags())
        bad.setCheckState(Qt.CheckState.Checked)
        self.window.signal_list_widget.insertItem(0, bad)
        key = bad.data(KEY_ROLE)
        self.window.signal_widgets[key]["checkbox"].item = bad
        self.window._sig_value_cache[key] = numeric_signal([0, 1], [0, 1])
        other_key = self.window.signal_list_widget.item(1).data(KEY_ROLE)
        self.window._sig_value_cache[other_key] = numeric_signal([0, 1], [0, 1])
        self.window._current_cursor_time = 1.0
        self.install_spies()
        bad.raise_value_write = True

        with self.assertRaisesRegex(RuntimeError, "value write failed"):
            self.window._update_signal_list_values()
        self.assertFalse(self.window.signal_list_widget.signalsBlocked())

        bad.raise_value_write = False
        self.window._update_signal_list_values()
        self.assertEqual(bad.data(VALUE_ROLE), "1.000")
        self.window._selection_changed.reset_mock()
        self.window.signal_list_widget.item(1).setCheckState(Qt.CheckState.Unchecked)
        self.assertGreater(self.window._selection_changed.call_count, 0)
        self.assertIn("(1)", self.window.view_selected_btn.text())

    def test_user_checkbox_still_runs_selection_and_filter_business(self):
        self.populate(2)
        self.install_spies()
        item = self.window.signal_list_widget.item(0)

        item.setCheckState(Qt.CheckState.Unchecked)

        self.assertGreater(self.window._selection_changed.call_count, 0)
        self.assertGreater(self.window._apply_signal_filter.call_count, 0)
        self.assertIn("(1)", self.window.view_selected_btn.text())


if __name__ == "__main__":
    unittest.main()
