import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from types import MappingProxyType
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from vet_data_modular import baseline
from vet_data_modular.window import MDFPlotter
from vet_data_modular.workers import (
    CanFileIdentity,
    CanBusCountSnapshot,
    CanBusMapping,
    CanDetectionSnapshot,
    capture_can_file_identity,
    detect_can_bus_info,
    message_raw_channel,
    normalize_can_channel,
    normalized_can_path,
)


APP = QApplication.instance() or QApplication([])


def message(channel, value=1, *, is_fd=False, arbitration_id=0x100, timestamp=1.0):
    return SimpleNamespace(
        channel=channel,
        timestamp=timestamp,
        arbitration_id=arbitration_id,
        data=bytes([value]),
        is_fd=is_fd,
    )


class MissingChannelMessage:
    timestamp = 1.0
    arbitration_id = 0x100
    data = b"\x01"
    is_fd = False


class FakeReader:
    def __init__(self, messages):
        self.messages = list(messages)
        self.stopped = False

    def __iter__(self):
        return iter(self.messages)

    def stop(self):
        self.stopped = True

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.stop()


class FakeSignal:
    name = "speed"
    unit = "km/h"
    comment = ""
    minimum = 0
    maximum = 255

    def decode(self, data):
        return data[0]


class FakeMessageDefinition:
    frame_id = 0x100
    name = "Speed"
    signals = [FakeSignal()]

    def decode(self, data, decode_choices=False):
        return {"speed": data[0]}

    def get_signal_by_name(self, name):
        return self.signals[0] if name == "speed" else None


class FakeDatabase:
    messages = [FakeMessageDefinition()]


class CanBusMappingMatrixTests(unittest.TestCase):
    CASES = (
        ((0,), {0: 1}),
        ((1,), {1: 1}),
        ((0, 1), {0: 1, 1: 2}),
        ((1, 2), {1: 1, 2: 2}),
        ((0, 1, 2, 3), {0: 1, 1: 2, 2: 3, 3: 4}),
        ((1, 2, 3, 4), {1: 1, 2: 2, 3: 3, 4: 4}),
    )

    def setUp(self):
        self.identity_patcher = patch(
            "vet_data_modular.workers.capture_can_file_identity",
            side_effect=lambda path: CanFileIdentity(
                normalized_can_path(path), 100, 200
            ),
        )
        self.identity_patcher.start()

    def tearDown(self):
        self.identity_patcher.stop()

    def test_required_raw_channel_matrix_and_original_bus_ids(self):
        for channels, expected in self.CASES:
            with self.subTest(channels=channels):
                reader = FakeReader(
                    message(channel, channel + 1, is_fd=index % 2 == 1, timestamp=index + 1.0)
                    for index, channel in enumerate(channels)
                )
                with patch("vet_data_modular.workers.can.BLFReader", return_value=reader):
                    detected = detect_can_bus_info("matrix.blf")
                self.assertEqual(dict(detected.mapping.raw_to_bus), expected)
                self.assertEqual(
                    dict(detected.mapping.bus_to_raw),
                    {bus: raw for raw, bus in expected.items()},
                )
                self.assertEqual(set(detected), set(expected.values()))
                for raw, bus in expected.items():
                    self.assertEqual(detected[bus]["original_bus_id"], raw)
                    self.assertEqual(detected[bus]["msg_count"], 1)
                self.assertTrue(reader.stopped)

    def test_can_can_fd_and_mixed_detection_stays_on_mapped_bus(self):
        reader = FakeReader([
            message(1, is_fd=False, timestamp=1.0),
            message(1, is_fd=True, timestamp=2.0),
            message(2, is_fd=False, timestamp=3.0),
        ])
        with patch("vet_data_modular.workers.can.BLFReader", return_value=reader):
            detected = detect_can_bus_info("types.blf")
        self.assertTrue(detected[1]["has_fd"])
        self.assertFalse(detected[2]["has_fd"])
        self.assertEqual(detected[1]["is_fd"], [False, True])

    def test_missing_none_and_invalid_channels_use_explicit_raw_one_fallback(self):
        self.assertEqual(message_raw_channel(MissingChannelMessage()), 1)
        for channel in (None, -1, True, 1.5, "can0", ""):
            with self.subTest(channel=channel):
                self.assertEqual(normalize_can_channel(channel), 1)
        self.assertEqual(normalize_can_channel("0"), 0)
        self.assertEqual(normalize_can_channel("2"), 2)

        reader = FakeReader([
            MissingChannelMessage(),
            message(None, timestamp=2.0),
            message("invalid", timestamp=3.0),
            message(1, timestamp=4.0),
        ])
        with patch("vet_data_modular.workers.can.BLFReader", return_value=reader):
            detected = detect_can_bus_info("missing.blf")
        self.assertEqual(set(detected), {1})
        self.assertEqual(detected[1]["original_bus_id"], 1)
        self.assertEqual(detected[1]["msg_count"], 4)

    def test_blf_asc_and_trc_use_the_same_mapping(self):
        for path, reader_name in (
            ("input.blf", "BLFReader"),
            ("input.asc", "LogReader"),
            ("input.trc", "LogReader"),
        ):
            with self.subTest(path=path):
                reader = FakeReader([message(1)])
                with patch(
                    f"vet_data_modular.workers.can.{reader_name}",
                    return_value=reader,
                ):
                    detected = detect_can_bus_info(path)
                self.assertEqual(dict(detected.mapping.raw_to_bus), {1: 1})


class ParseWorkerMappingTests(unittest.TestCase):
    def run_worker(self, target_raw_channel, messages, suffix="blf"):
        readers = [FakeReader(messages)]
        finished = []
        errors = []
        worker = baseline.ParseWorker(
            1,
            target_raw_channel,
            sum(message_raw_channel(item) == target_raw_channel for item in messages),
            f"input.{suffix}",
            CanFileIdentity(normalized_can_path(f"input.{suffix}"), 100, 200),
            FakeDatabase(),
        )
        worker.parse_finished.connect(lambda bus, stats: finished.append((bus, stats)))
        worker.parse_error.connect(lambda bus, error: errors.append((bus, error)))
        reader_name = "BLFReader" if suffix == "blf" else "LogReader"
        output = io.StringIO()
        with patch.object(baseline.can, reader_name, return_value=readers[0]) as reader_factory, patch.object(
            baseline,
            "capture_can_file_identity",
            return_value=worker.file_identity,
        ), contextlib.redirect_stdout(output):
            worker.run()
        self.assertEqual(reader_factory.call_count, 1)
        self.assertFalse(errors)
        self.assertEqual(len(finished), 1)
        return finished[0], output.getvalue(), readers

    def test_single_raw_one_maps_to_gui_bus_one_and_parses(self):
        (bus_id, stats), output, readers = self.run_worker(1, [message(1, 17)])
        self.assertEqual(bus_id, 1)
        self.assertEqual(stats["messages_processed"], 1)
        self.assertEqual(stats["pool"]["speed"]["v"], [17.0])
        self.assertIn("探测消息总数: 1", output)
        self.assertTrue(all(reader.stopped for reader in readers))

    def test_existing_channel_zero_maps_to_gui_bus_one_and_still_parses_raw_zero(self):
        (bus_id, stats), output, _readers = self.run_worker(0, [message(0, 9)])
        self.assertEqual(bus_id, 1)
        self.assertEqual(stats["messages_processed"], 1)
        self.assertEqual(stats["pool"]["speed"]["v"], [9.0])
        self.assertIn("探测消息总数: 1", output)

    def test_same_arbitration_id_on_adjacent_raw_channels_never_crosses_bus(self):
        messages = [
            message(1, 11, arbitration_id=0x100, timestamp=1.0),
            message(2, 22, arbitration_id=0x100, timestamp=2.0),
        ]
        (_bus_id, stats), output, _readers = self.run_worker(1, messages)
        self.assertEqual(stats["messages_processed"], 1)
        self.assertEqual(stats["pool"]["speed"]["v"], [11.0])
        self.assertIn("探测消息总数: 1", output)

    def test_final_registered_signal_keeps_gui_bus_id(self):
        (_bus_id, stats), _output, _readers = self.run_worker(1, [
            message(1, 23, timestamp=1.0),
            message(1, 24, timestamp=2.0),
        ])

        class Target:
            can_parsed_data = {}
            signals = {}
            bus_signal_map = {}

        target = Target()
        baseline.MDFPlotter.register_can_signals(
            target, stats["pool"], "Bus 1", 1
        )
        key = "CAN_Bus 1_speed"
        self.assertEqual(target.signals[key]["bus_id"], 1)
        self.assertEqual(target.can_parsed_data[key]["bus_id"], 1)
        self.assertEqual(target.can_parsed_data[key]["samples"][-1], 24.0)


class GuiBusBindingTests(unittest.TestCase):
    @staticmethod
    def bus_data(mapping):
        return {
            bus: {
                "name": f"Bus {bus}",
                "original_bus_id": raw,
                "actual_ids": [0x100],
                "id_count": 1,
                "msg_count": 1,
                "has_fd": False,
            }
            for bus, raw in mapping.bus_to_raw.items()
        }

    def test_dbc_binding_creates_worker_with_mapped_target_raw_channel(self):
        window = MDFPlotter()
        mapping = CanBusMapping.from_raw_channels((1, 2))
        window.can_bus_mapping = mapping
        window.can_bus_data.update(self.bus_data(mapping))
        window.bus_protocols.update({1: "bus1.dbc", 2: "bus2.dbc"})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.blf"
            path.write_bytes(b"stable")
            window.mdf_path = str(path)
            window.can_detection_snapshot = CanDetectionSnapshot(
                capture_can_file_identity(path),
                mapping,
                MappingProxyType({
                    bus: CanBusCountSnapshot(bus, raw, 1)
                    for bus, raw in mapping.bus_to_raw.items()
                }),
            )
            try:
                with patch.object(
                    baseline.cantools.database, "load_file", return_value=FakeDatabase()
                ), patch.object(baseline.ParseWorker, "start"):
                    window.parse_bus(1)
                    window.parse_bus(2)
                self.assertEqual(window.parse_workers[1].target_raw_channel, 1)
                self.assertEqual(window.parse_workers[2].target_raw_channel, 2)
                self.assertEqual(window.parse_workers[1].msg_count, 1)
                self.assertEqual(window.parse_workers[2].msg_count, 1)
                self.assertIsInstance(window.parse_workers[1].db, FakeDatabase)
                self.assertIsInstance(window.parse_workers[2].db, FakeDatabase)
            finally:
                for dialog in window.progress_dialogs.values():
                    dialog.deleteLater()
                for worker in window.parse_workers.values():
                    worker.deleteLater()
                window.deleteLater()
                APP.processEvents()

    def test_saved_bus_config_restores_by_gui_bus_without_remapping(self):
        source = MDFPlotter()
        target = MDFPlotter()
        mapping = CanBusMapping.from_raw_channels((1, 2))
        try:
            source.can_bus_mapping = mapping
            source.can_bus_data.update(self.bus_data(mapping))
            source.can_bus_data[1]["name"] = "Powertrain"
            source.bus_protocols[1] = "powertrain.dbc"
            config = source._signal_config_data()

            target.can_bus_mapping = mapping
            target.can_bus_data.update(self.bus_data(mapping))
            target._apply_saved_bus_config(config)
            self.assertEqual(target.can_bus_data[1]["name"], "Powertrain")
            self.assertEqual(target.bus_protocols[1], "powertrain.dbc")
            self.assertEqual(target.can_bus_data[1]["original_bus_id"], 1)
            self.assertEqual(target.can_bus_data[2]["original_bus_id"], 2)
        finally:
            source.deleteLater()
            target.deleteLater()
            APP.processEvents()


class AuxiliaryMappingTests(unittest.TestCase):
    def test_timestamp_signals_use_gui_bus_mapping(self):
        mapping = CanBusMapping.from_raw_channels((0, 1))

        class Target:
            can_bus_mapping = mapping
            can_bus_data = {1: {"name": "Bus 1"}, 2: {"name": "Bus 2"}}
            can_parsed_data = {}
            signals = {}
            bus_signal_map = {}
            mdf_file = None

        reader = FakeReader([
            message(0, timestamp=1.0),
            message(1, timestamp=1.5),
            message(0, timestamp=2.0),
            message(1, timestamp=2.5),
        ])
        target = Target()
        with patch.object(baseline.can, "BLFReader", return_value=reader):
            baseline.MDFPlotter.load_can_timestamps_only_multibus(
                target, "input.blf"
            )
        self.assertEqual(target.signals["CAN_Bus 1_Time"]["bus_id"], 1)
        self.assertEqual(target.signals["CAN_Bus 2_Time"]["bus_id"], 2)

    def test_blf_cut_gui_bus_filter_is_translated_through_mapping(self):
        mapping = CanBusMapping.from_raw_channels((0, 1))
        messages = [message(0, timestamp=1.0), message(1, timestamp=1.0)]
        readers = [FakeReader(messages), FakeReader(messages)]

        class Writer:
            written = []

            def __init__(self, _path):
                self.written = []
                Writer.written = self.written

            def __enter__(self):
                return self

            def __exit__(self, _type, _value, _traceback):
                pass

            def write(self, msg):
                self.written.append(msg)

        target = SimpleNamespace(can_bus_mapping=mapping)
        with patch.object(baseline.can, "BLFReader", side_effect=readers), patch.object(
            baseline.can, "BLFWriter", Writer
        ):
            result = baseline.MDFPlotter.cut_blf_by_time(
                target, "input.blf", "output.blf", 0.0, 1.0, [1]
            )
        self.assertTrue(result)
        self.assertEqual([message_raw_channel(item) for item in Writer.written], [0])


if __name__ == "__main__":
    unittest.main()
