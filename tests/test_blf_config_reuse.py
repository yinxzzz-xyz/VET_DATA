import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

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

    def test_config_restore_schedules_parse_without_missing_qtimer(self):
        self.window.mdf_path = r"D:\captures\next.blf"
        self.window.can_detection_snapshot = object()
        self.window.can_bus_data[1] = {"name": "Bus 1"}
        with tempfile.TemporaryDirectory() as folder:
            dbc = Path(folder, "car.dbc")
            dbc.write_text("VERSION \\\"\\\"", encoding="utf-8")
            config_path = Path(folder, "saved.json")
            config_path.write_text(json.dumps({
                "selected_signals": ["CAN_Bus 1_speed"],
                "bus_config": {"1": {"name": "Bus 1", "protocol": str(dbc)}},
            }), encoding="utf-8")
            with patch.object(window_module.QFileDialog, "getOpenFileName", return_value=(str(config_path), "")), \
                 patch.object(window_module.QTimer, "singleShot") as single_shot, \
                 patch.object(window_module.QMessageBox, "warning"), \
                 patch.object(window_module.QMessageBox, "critical") as critical:
                self.window.load_signal_config()
        critical.assert_not_called()
        single_shot.assert_called_once()
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
