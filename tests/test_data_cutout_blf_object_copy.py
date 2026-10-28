"""Object-level BLF copy: fidelity, fallback, cancellation and fast-path use."""

import os
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

import can

from vet_data_modular.blf_object_copy import RawCopyUnavailable, copy_blf_window
from vet_data_modular.data_cutout import CutoutCancelled, CutoutWindow, cut_blf_raw
from vet_data_modular.workers import CanBusMapping


BASE = 1_700_000_000.0


def make_blf(path, messages):
    writer = can.BLFWriter(str(path))
    try:
        for message in messages:
            writer.on_message_received(message)
    finally:
        writer.stop()


def read_blf(path):
    reader = can.BLFReader(str(path))
    try:
        return list(reader)
    finally:
        reader.stop()


def message_key(message):
    return (
        round(message.timestamp, 9),
        message.channel,
        message.arbitration_id,
        message.dlc,
        bytes(message.data),
        bool(message.is_extended_id),
        bool(message.is_fd),
        bool(message.bitrate_switch),
        bool(message.is_error_frame),
        bool(message.is_remote_frame),
        bool(message.is_rx),
    )


def sample_messages():
    messages = []
    for index in range(1200):
        timestamp = BASE + index * 0.01
        if index % 11 == 0:
            messages.append(can.Message(
                timestamp=timestamp, arbitration_id=0x1AB, data=b"\x01\x02\x03",
                channel=2, is_extended_id=True, is_fd=True, bitrate_switch=True,
            ))
        elif index % 7 == 0:
            messages.append(can.Message(
                timestamp=timestamp, arbitration_id=0x1AC, data=b"",
                channel=0, is_remote_frame=True, is_extended_id=False,
            ))
        else:
            messages.append(can.Message(
                timestamp=timestamp, arbitration_id=0x100 + index % 3,
                data=bytes([index % 251, 2, 3, 4, 5, 6, 7, 8]),
                channel=index % 4, is_extended_id=False,
            ))
    return messages


class ObjectCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.source = self.folder / "source.blf"
        make_blf(self.source, sample_messages())
        self.first = read_blf(self.source)[0].timestamp
        self.last = read_blf(self.source)[-1].timestamp

    def tearDown(self):
        self.temp.cleanup()

    def expected(self, window):
        return [
            message for message in read_blf(self.source)
            if window.start <= message.timestamp - self.first <= window.stop
        ]

    def test_windows_match_python_can_messages(self):
        windows = {
            "head": CutoutWindow(0.0, 2.0),
            "middle": CutoutWindow(5.0, 7.0),
            "tail": CutoutWindow(10.0, 12.0),
        }
        for label, window in windows.items():
            with self.subTest(label):
                output = self.folder / f"{label}.blf"
                copied, scanned, bus_counts = copy_blf_window(self.source, output, window)
                written = read_blf(output)
                expected = self.expected(window)
                self.assertEqual(copied, len(expected))
                self.assertEqual(len(written), len(expected))
                self.assertEqual(
                    [message_key(item) for item in written],
                    [message_key(item) for item in expected],
                )
                self.assertEqual(sum(count for _bus, count in bus_counts), copied)
                reader = can.BLFReader(str(output))
                try:
                    self.assertEqual(reader.object_count, copied)
                finally:
                    reader.stop()

    def test_full_copy_preserves_every_message(self):
        output = self.folder / "full.blf"
        duration = self.last - self.first
        copied, scanned, _ = copy_blf_window(
            self.source, output, CutoutWindow(0.0, duration + 0.001),
        )
        written = read_blf(output)
        expected = read_blf(self.source)
        self.assertEqual(copied, len(expected))
        self.assertEqual(scanned, len(expected))
        self.assertEqual(
            [message_key(item) for item in written],
            [message_key(item) for item in expected],
        )

    def test_bus_counts_follow_the_gui_mapping(self):
        mapping = CanBusMapping.from_raw_channels((0, 1, 2, 3))
        output = self.folder / "mapped.blf"
        _copied, _scanned, bus_counts = copy_blf_window(
            self.source, output, CutoutWindow(0.0, 12.0), mapping=mapping,
        )
        self.assertEqual(
            {bus for bus, _count in bus_counts},
            set(mapping.bus_to_raw),
        )

    def test_non_blf_source_is_reported_as_unavailable(self):
        broken = self.folder / "broken.blf"
        broken.write_bytes(b"not a blf file")
        with self.assertRaises(RawCopyUnavailable):
            copy_blf_window(broken, self.folder / "out.blf", CutoutWindow(0.0, 1.0))

    def test_empty_window_reports_scanned_total(self):
        with self.assertRaises(ValueError) as caught:
            copy_blf_window(self.source, self.folder / "out.blf", CutoutWindow(100.0, 101.0))
        self.assertIn("总报文数", str(caught.exception))

    def test_cancel_before_copy(self):
        event = Event()
        event.set()
        with self.assertRaises(CutoutCancelled):
            copy_blf_window(
                self.source, self.folder / "out.blf", CutoutWindow(0.0, 1.0),
                cancel_event=event,
            )

    def test_cut_blf_raw_uses_the_object_copy(self):
        with patch.object(can, "BLFReader", side_effect=AssertionError("decoded reader used")):
            result = cut_blf_raw(
                self.source, self.folder / "fast.blf", CutoutWindow(0.0, 2.0),
            )
        self.assertGreater(result.message_count, 0)
        self.assertTrue((self.folder / "fast.blf").exists())

    def test_cut_blf_raw_falls_back_when_layout_is_unsupported(self):
        broken = self.folder / "broken.blf"
        broken.write_bytes(b"not a blf file")
        with self.assertRaises(Exception):
            cut_blf_raw(broken, self.folder / "out.blf", CutoutWindow(0.0, 1.0))
        self.assertFalse((self.folder / "out.blf").exists())


if __name__ == "__main__":
    unittest.main()
