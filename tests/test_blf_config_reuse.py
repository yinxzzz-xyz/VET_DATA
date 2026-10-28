import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from vet_data_modular import baseline
from vet_data_modular.window import MDFPlotter
import vet_data_modular.window as window_module


APP = QApplication.instance() or QApplication([])


class BlfConfigReuseTests(unittest.TestCase):
    def setUp(self):
        self.window = MDFPlotter()

    def tearDown(self):
        self.window.deleteLater()
        APP.processEvents()

    def test_switch_clears_file_bound_signals_samples_and_curves(self):
        self.window._can_raw_message_cache[1] = object()
        self.window.signals["CAN_Bus 1_speed"] = {
            "name": "CAN_Bus 1_speed", "display_name": "speed", "bus_id": 1,
            "signal_type": "can",
        }
        self.window.can_parsed_data["CAN_Bus 1_speed"] = {
            "timestamps": [0.0], "samples": [42.0], "bus_id": 1,
        }
        self.window.plot_widgets.append(object())
        # Plot widget cleanup requires QWidget methods; exercise the actual method
        # with no concrete plot while still checking its destructive boundary.
        self.window.plot_widgets.clear()
        self.window._clear_current_can_state()
        self.assertEqual(self.window.signals, {})
        self.assertEqual(self.window.can_parsed_data, {})
        self.assertEqual(self.window.bus_signal_map, {})
        self.assertEqual(self.window.plot_widgets, [])
        self.assertFalse(self.window._can_raw_message_cache)

    def test_old_parse_callback_is_rejected_after_file_switch(self):
        from types import SimpleNamespace
        from vet_data_modular.workers import CanFileIdentity

        old_identity = CanFileIdentity("old.blf", 1, 1)
        new_identity = CanFileIdentity("new.blf", 2, 2)
        old_worker = SimpleNamespace(
            _task_generation=1,
            _file_identity=old_identity,
            _bus_config_generation=3,
        )
        current_worker = SimpleNamespace()
        target = SimpleNamespace(
            sender=lambda: old_worker,
            parse_workers={1: current_worker},
            _can_task_generation=2,
            can_detection_snapshot=SimpleNamespace(file_identity=new_identity),
            _bus_config_generations={1: 3},
            bus_protocols={},
        )
        self.assertFalse(baseline.MDFPlotter._is_current_parse_callback(target, 1))

    def test_config_is_read_and_staged_before_can_signal_parse(self):
        self.window.mdf_path = r"D:\captures\next.blf"
        self.window.can_detection_snapshot = None
        config = {
            "selected_signals": ["CAN_Bus 1_speed"],
            "bus_config": {"1": {"name": "Bus 1", "protocol": "D:/dbc/car.dbc"}},
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "saved.json")
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch("vet_data_modular.window.QFileDialog.getOpenFileName", return_value=(str(path), "")), \
                 patch("vet_data_modular.window.QMessageBox.information") as info:
                self.window.load_signal_config()
        self.assertEqual(self.window._pending_can_config, config)
        info.assert_called_once()

    def test_config_restore_restores_selection_without_starting_parse(self):
        self.window.mdf_path = r"D:\captures\next.blf"
        self.window.can_detection_snapshot = object()
        self.window.can_bus_data[1] = {"name": "Bus 1"}
        self.window.bus_protocols[1] = "old.dbc"
        with tempfile.TemporaryDirectory() as folder:
            dbc = Path(folder, "car.dbc")
            dbc.write_text(
                'VERSION ""\n\nNS_ :\n\nBS_:\n\nBU_: Vector__XXX\n\n'
                'BO_ 256 Powertrain: 1 Vector__XXX\n'
                ' SG_ speed : 0|8@1+ (1,0) [0|255] "" Vector__XXX\n',
                encoding="utf-8",
            )
            config_path = Path(folder, "saved.json")
            config_path.write_text(json.dumps({
                "selected_signals": ["CAN_Bus 1_speed"],
                "bus_config": {"1": {"name": "Bus 1", "protocol": str(dbc)}},
                "math_channels": {},
            }), encoding="utf-8")
            with patch.object(window_module.QFileDialog, "getOpenFileName", return_value=(str(config_path), "")), \
                 patch.object(window_module.QTimer, "singleShot") as single_shot, \
                 patch.object(window_module.QMessageBox, "warning"), \
                 patch.object(window_module.QMessageBox, "information"), \
                 patch.object(window_module.QMessageBox, "critical") as critical, \
                 patch.object(self.window, "_add_pending_selection_rows"), \
                 patch.object(self.window, "plot_selected_signals") as plot, \
                 patch.object(self.window, "start_parse_batch_with_progress") as start_batch, \
                 patch("vet_data_modular.window.os.path.isfile", return_value=True), \
                 patch("vet_data_modular.window.os.access", return_value=True):
                self.window.load_signal_config()
        critical.assert_not_called()
        single_shot.assert_not_called()
        plot.assert_not_called()
        start_batch.assert_not_called()
        self.assertIsNone(self.window._pending_can_config)
        self.assertEqual(self.window._pending_signal_restore["expected_buses"], {1})

    def test_selection_restore_waits_only_for_buses_with_dbc(self):
        identity = {
            "bus_id": 1, "frame_id": 0x100, "is_extended": False,
            "message_name": "Powertrain", "signal_name": "speed",
        }
        self.window.can_bus_data.update({1: {}, 2: {}})
        self.window.signals["CAN_Bus 1_speed"] = {
            "name": "CAN_Bus 1_speed", "display_name": "speed", "bus_id": 1,
            "signal_identity": identity,
        }
        self.window._pending_signal_restore = {
            "expected_buses": {1}, "selected": ["CAN_Bus 1_speed"],
            "identities": {"CAN_Bus 1_speed": identity}, "config": {},
        }
        with patch.object(self.window, "_restore_calculated_channels", return_value=SimpleNamespace(failures={})), \
             patch.object(self.window, "plot_selected_signals"):
            self.window._complete_pending_signal_restore(1)
        self.assertIsNone(self.window._pending_signal_restore)
        self.assertEqual(self.window.get_selected_signals(), ["CAN_Bus 1_speed"])

    def test_config_restore_does_not_parse_unselected_buses(self):
        self.window._pending_can_config = {
            "selected_signals": [],
            "signal_identities": {},
            "bus_config": {"1": {"protocol": "one.dbc"}, "2": {"protocol": "two.dbc"}},
        }
        self.window.can_bus_data.update({1: {}, 2: {}})
        self.window.bus_protocols.update({1: "one.dbc", 2: "two.dbc"})
        with patch.object(self.window, "_apply_saved_bus_config", return_value=[]), \
             patch.object(self.window, "_add_pending_selection_rows"), \
             patch.object(self.window, "start_parse_batch_with_progress") as start_batch, \
             patch("vet_data_modular.window.os.path.isfile", return_value=True), \
             patch("vet_data_modular.window.os.access", return_value=True):
            self.window._restore_pending_can_config()
        start_batch.assert_not_called()
        self.assertTrue(self.window._auto_parse_suppressed_for_restore)
        self.assertEqual(self.window._pending_signal_restore["expected_buses"], set())

    def test_demand_parse_uses_bus_widget_filter_for_missing_signals_only(self):
        speed = SimpleNamespace(name="speed")
        rpm = SimpleNamespace(name="rpm")
        db = SimpleNamespace(messages=[SimpleNamespace(frame_id=0x100, signals=[speed, rpm])])
        widget = SimpleNamespace(_cached_filtered_db=None)
        self.window.db_per_bus[1] = {"catalog_db": db}
        self.window.bus_config_widgets[1] = widget
        self.window.can_bus_data[1] = {"name": "Bus 1"}
        self.window.signals.update({
            "CAN_Bus 1_speed": {"bus_id": 1},
            "CAN_Bus 1_rpm": {"bus_id": 1},
        })
        self.window.can_parsed_data["CAN_Bus 1_speed"] = {"samples": [42.0]}
        filtered = object()
        with patch.object(
            baseline.BusConfigWidget, "_build_filtered_database",
            return_value=(filtered, 0),
        ) as build_filter, patch.object(
            self.window, "start_parse_batch_with_progress"
        ) as start_batch:
            self.assertTrue(self.window.prepare_selected_can_signals([
                "CAN_Bus 1_speed", "CAN_Bus 1_rpm",
            ]))
        build_filter.assert_called_once_with(db, {(0x100, False): {"rpm"}})
        self.assertIs(widget._cached_filtered_db, filtered)
        start_batch.assert_called_once_with([1])

    def test_plot_button_starts_demand_parse_without_old_parse_action(self):
        self.window.signals["CAN_Bus 1_speed"] = {"bus_id": 1}
        with patch.object(self.window, "get_selected_signals", return_value=["CAN_Bus 1_speed"]), \
             patch.object(self.window, "prepare_selected_can_signals", return_value=True) as prepare, \
             patch.object(self.window, "_set_plot_empty_state") as empty_state:
            self.window.plot_selected_signals()
        prepare.assert_called_once_with(["CAN_Bus 1_speed"])
        empty_state.assert_called_once_with("正在按需解析所选 CAN 信号…")

    def test_initial_auto_parse_does_not_repeat_restore_batch(self):
        self.window.auto_parse_enabled = True
        self.window.bus_protocols[1] = "one.dbc"
        self.window._auto_parse_suppressed_for_restore = True
        with patch.object(self.window, "start_parse_with_progress") as start_parse:
            self.window.auto_parse_all_buses()
        start_parse.assert_not_called()
        self.assertFalse(self.window._auto_parse_suppressed_for_restore)

    def test_bus_configuration_creation_does_not_schedule_automatic_parsing(self):
        self.window.can_bus_data[1] = {
            "name": "Bus 1", "id_count": 0, "has_fd": False,
        }
        with patch.object(baseline.QTimer, "singleShot") as single_shot:
            self.window.create_bus_config_ui()
        single_shot.assert_not_called()

    def test_signal_selection_waits_for_every_bus_in_batched_restore(self):
        self.window.can_bus_data.update({1: {}, 2: {}})
        self.window._pending_signal_restore = {
            "expected_buses": {1, 2},
            "selected": [],
            "identities": {},
            "config": {},
        }
        with patch.object(self.window, "plot_selected_signals"):
            self.window._complete_pending_signal_restore(1)
            self.assertIsNotNone(self.window._pending_signal_restore)
            self.assertEqual(
                self.window._pending_signal_restore["completed_buses"], {1}
            )
            self.window._complete_pending_signal_restore(2)
        self.assertIsNone(self.window._pending_signal_restore)

    def test_identity_matching_does_not_restore_same_key_for_other_frame(self):
        saved = {
            "bus_id": 1, "frame_id": 0x100, "is_extended": False,
            "message_name": "OldFrame", "signal_name": "speed",
        }
        current = {
            "bus_id": 1, "frame_id": 0x200, "is_extended": False,
            "message_name": "NewFrame", "signal_name": "speed",
        }
        self.window.signals["CAN_Bus 1_speed"] = {
            "name": "CAN_Bus 1_speed", "display_name": "speed", "bus_id": 1,
            "signal_identity": current,
        }
        self.window.refresh_signal_list_ui()
        with patch.object(self.window, "plot_selected_signals"), \
             patch("vet_data_modular.window.QMessageBox.warning"):
            missing = self.window._apply_can_selection(
                ["CAN_Bus 1_speed"], {"CAN_Bus 1_speed": saved}
            )
        self.assertEqual(missing, ["CAN_Bus 1_speed"])
        self.assertEqual(len(self.window.get_selected_signals()), 1)
        unavailable_key = self.window.get_selected_signals()[0]
        self.assertFalse(self.window.signals[unavailable_key]["available"])

    def test_selection_config_survives_file_catalog_clear_and_can_be_cancelled(self):
        identity = {
            "bus_id": 1, "raw_channel": 0, "frame_id": 0x100,
            "is_extended": False, "message_name": "Powertrain",
            "signal_name": "speed",
        }
        self.window.signals["CAN_Bus 1_speed"] = {
            "name": "CAN_Bus 1_speed", "display_name": "speed", "bus_id": 1,
            "signal_type": "can", "signal_identity": identity,
            "available": True,
        }
        self.window.can_parsed_data["CAN_Bus 1_speed"] = {
            "timestamps": [0.0], "samples": [42.0], "bus_id": 1,
        }
        self.window.refresh_signal_list_ui()
        self.window.signal_widgets["CAN_Bus 1_speed"]["checkbox"].setChecked(True)
        self.window._sync_selected_can_config()
        self.window.can_parsed_data.clear()
        self.window.signals.clear()
        self.window.refresh_signal_list_ui()
        selected, identities = self.window._selected_can_config_payload()
        self.assertEqual(selected, ["CAN_Bus 1_speed"])
        self.assertEqual(identities["CAN_Bus 1_speed"], identity)

        self.window._add_pending_selection_rows()
        self.assertEqual(len(self.window.get_selected_signals()), 1)
        self.window.signal_widgets[self.window.get_selected_signals()[0]]["checkbox"].setChecked(False)
        self.assertFalse(self.window._selected_can_config)

    def test_pending_selection_rows_are_idempotent_and_distinct_across_buses(self):
        identity_a = {
            "bus_id": 1, "raw_channel": 0, "frame_id": 0x100,
            "is_extended": False, "message_name": "Powertrain",
            "signal_name": "speed",
        }
        identity_b = dict(identity_a, bus_id=2, raw_channel=1)
        self.window._load_selected_can_config(
            ["CAN_Bus 1_speed", "CAN_Bus 2_speed"],
            {"CAN_Bus 1_speed": identity_a, "CAN_Bus 2_speed": identity_b},
        )
        self.window._add_pending_selection_rows()
        first_count = len(self.window.signals)
        self.window._add_pending_selection_rows()
        self.assertEqual(first_count, 2)
        self.assertEqual(len(self.window.signals), 2)
        self.assertEqual(len(self.window.get_selected_signals()), 2)
        self.assertEqual(
            len({self.window._identity_token(info["signal_identity"], info["bus_id"])
                 for info in self.window.signals.values()}),
            2,
        )

    def test_catalog_replaces_matching_pending_placeholder_in_place(self):
        identity = {
            "bus_id": 1, "raw_channel": 0, "frame_id": 0x100,
            "is_extended": False, "message_name": "Powertrain",
            "signal_name": "speed",
        }
        self.window._load_selected_can_config(["saved_speed"], {"saved_speed": identity})
        self.window._add_pending_selection_rows()
        self.window.can_bus_data[1] = {"name": "Bus 1", "raw_channel": 0}
        with tempfile.TemporaryDirectory() as folder:
            dbc = Path(folder, "catalog.dbc")
            dbc.write_text(
                'VERSION ""\n\nNS_ :\n\nBS_:\n\nBU_: Vector__XXX\n'
                'BO_ 256 Powertrain: 1 Vector__XXX\n'
                ' SG_ speed : 0|8@1+ (1,0) [0|255] "" Vector__XXX\n',
                encoding="utf-8",
            )
            self.window.on_bus_protocol_changed(1, str(dbc))
        self.assertEqual(len(self.window.signals), 1)
        self.assertNotIn("saved_speed", self.window.signals)
        catalog = self.window.signals["CAN_Bus 1_speed"]
        self.assertFalse(catalog.get("pending_selection_placeholder", False))
        self.assertEqual(catalog["signal_identity"]["frame_id"], 0x100)

    def test_restoring_multiple_signals_does_not_drop_later_records(self):
        identities = {}
        for index, name in enumerate(("speed", "rpm"), 1):
            identity = {
                "bus_id": 1, "raw_channel": 0, "frame_id": 0x100 + index,
                "is_extended": False, "message_name": "Powertrain",
                "signal_name": name,
            }
            key = f"CAN_Bus 1_{name}"
            identities[key] = identity
            self.window.signals[key] = {
                "name": key, "display_name": name, "bus_id": 1,
                "signal_type": "can", "signal_identity": identity,
                "available": True,
            }
            self.window.can_parsed_data[key] = {
                "timestamps": [0.0, 1.0], "samples": [1.0, 2.0],
                "bus_id": 1,
            }
        self.window.refresh_signal_list_ui()
        with patch.object(self.window, "plot_selected_signals"):
            self.window._apply_can_selection(list(identities), identities)
        self.assertEqual(set(self.window.get_selected_signals()), set(identities))
        self.assertEqual(len(self.window._selected_can_config), 2)

        key = "CAN_Bus 1_speed"
        self.window.signal_widgets[key]["checkbox"].setChecked(False)
        selected, _identities = self.window._selected_can_config_payload()
        self.assertEqual(selected, ["CAN_Bus 1_rpm"])

    def test_saved_single_identity_has_same_shape_as_parser_identity(self):
        identity = {
            "bus_id": 1, "raw_channel": 0, "frame_id": 0x100,
            "is_extended": False, "message_name": "Powertrain",
            "signal_name": "speed",
        }
        self.assertEqual(
            self.window._identity_token([identity]),
            self.window._identity_token(identity),
        )

    def test_dbc_catalog_keeps_selected_signal_unavailable_when_frame_missing(self):
        identity = {
            "raw_channel": 0, "frame_id": 0x100, "is_extended": False,
            "message_name": "Powertrain", "signal_name": "speed",
        }
        self.window.can_bus_data[1] = {"name": "Bus 1", "parse_state": "complete"}
        self.window.can_parsed_data = {}
        self.window.signals = {}
        self.window.register_can_signal_catalog(1, "Bus 1", {
            "signal_identity": {"speed": [identity]},
            "matched_frames": [], "decode_failed_frames": 0,
        })
        catalog = self.window.signals["CAN_Bus 1_speed"]
        self.assertFalse(catalog["available"])
        self.assertIn("不包含", catalog["unavailable_reason"])

    def test_switch_restores_same_identity_and_keeps_missing_signal_selected(self):
        identity_a = {
            "bus_id": 1, "raw_channel": 0, "frame_id": 0x101,
            "is_extended": False, "message_name": "Powertrain",
            "signal_name": "speed",
        }
        identity_b = dict(identity_a, bus_id=2)
        self.window.signals["CAN_Bus 1_speed"] = {
            "name": "CAN_Bus 1_speed", "display_name": "speed", "bus_id": 1,
            "signal_type": "can", "signal_identity": identity_a,
            "available": True,
        }
        self.window.can_parsed_data["CAN_Bus 1_speed"] = {
            "timestamps": [0.0, 1.0], "samples": [1.0, 2.0], "bus_id": 1,
        }
        self.window.refresh_signal_list_ui()
        self.window.signal_widgets["CAN_Bus 1_speed"]["checkbox"].setChecked(True)
        self.window._sync_selected_can_config()

        self.window._clear_current_can_state()
        self.assertFalse(self.window.can_parsed_data)
        self.window._add_pending_selection_rows()
        self.window.signals.pop("CAN_Bus 1_speed", None)
        self.window.signals["CAN_Bus 2_speed"] = {
            "name": "CAN_Bus 2_speed", "display_name": "speed", "bus_id": 2,
            "signal_type": "can", "signal_identity": identity_b,
            "available": True,
        }
        self.window.can_parsed_data["CAN_Bus 2_speed"] = {
            "timestamps": [0.0, 1.0], "samples": [3.0, 4.0], "bus_id": 2,
        }
        with patch.object(self.window, "plot_selected_signals"):
            self.window._apply_can_selection(
                ["CAN_Bus 1_speed"], {"CAN_Bus 1_speed": identity_a}
            )
        self.assertEqual(self.window.get_selected_signals(), ["CAN_Bus 2_speed"])
        self.assertEqual(self.window.can_parsed_data["CAN_Bus 2_speed"]["samples"], [3.0, 4.0])


if __name__ == "__main__":
    unittest.main()
