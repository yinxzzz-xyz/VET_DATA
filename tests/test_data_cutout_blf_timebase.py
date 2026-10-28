"""Window selection for BLF cutout: one global time base for every bus."""

import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from vet_data_modular import baseline
from vet_data_modular.workers import CanBusMapping


class FakeMessage:
    __slots__ = ("timestamp", "channel", "arbitration_id", "data")

    def __init__(self, timestamp, channel, arbitration_id):
        self.timestamp = timestamp
        self.channel = channel
        self.arbitration_id = arbitration_id
        self.data = b"\x01"


class FakeReader:
    def __init__(self, messages, counter=None):
        self._messages = list(messages)
        self.stopped = False
        if counter is not None:
            counter.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.stop()

    def __iter__(self):
        return iter(self._messages)

    def stop(self):
        self.stopped = True


class OnMessageWriter:
    written = []

    def __init__(self, path):
        self.path = path
        self.written = []
        OnMessageWriter.written = self.written

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        pass

    def on_message_received(self, message):
        self.written.append(message)


class WriteWriter:
    written = []

    def __init__(self, path):
        self.path = path
        self.written = []
        WriteWriter.written = self.written

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        pass

    def write(self, message):
        self.written.append(message)


def run_cut(messages, start, stop, bus_filter=None, mapping=None, writer=OnMessageWriter):
    readers = []
    target = SimpleNamespace(can_bus_mapping=mapping)
    with patch.object(
        baseline.can, "BLFReader", side_effect=lambda _path: FakeReader(messages, readers)
    ), patch.object(baseline.can, "BLFWriter", writer):
        with contextlib.redirect_stdout(io.StringIO()):
            baseline.MDFPlotter.cut_blf_by_time(
                target, "input.blf", "output.blf", start, stop, bus_filter
            )
    return list(writer.written), readers


class BlfCutTimeBaseTests(unittest.TestCase):
    def test_head_window_keeps_every_bus_and_drops_late_frames(self):
        messages = [
            FakeMessage(1000.0, 2, 0x100),   # first message -> global t0
            FakeMessage(1000.5, 0, 0x101),
            FakeMessage(1005.0, 1, 0x102),
            FakeMessage(1010.0, 0, 0x101),
            FakeMessage(1012.0, 3, 0x103),  # 12 s: outside [0, 10]
            FakeMessage(1119.9, 1, 0x102),  # 119.9 s: outside [0, 10]
        ]
        written, readers = run_cut(messages, 0.0, 10.0)
        self.assertEqual(
            [message.timestamp for message in written],
            [1000.0, 1000.5, 1005.0, 1010.0],
        )
        self.assertEqual([message.channel for message in written], [2, 0, 1, 0])
        # The old implementation opened the file twice; the cut is one pass now.
        self.assertEqual(len(readers), 1)

    def test_tail_window_keeps_every_bus(self):
        messages = [
            FakeMessage(1000.0, 2, 0x100),
            FakeMessage(1100.0, 0, 0x101),
            FakeMessage(1119.9, 1, 0x102),
            FakeMessage(1119.95, 0, 0x101),
            FakeMessage(1120.0, 3, 0x103),
        ]
        written, _ = run_cut(messages, 119.9, 120.0)
        self.assertEqual(
            [message.timestamp for message in written],
            [1119.9, 1119.95, 1120.0],
        )

    def test_window_near_zero_includes_only_first_frame(self):
        messages = [
            FakeMessage(1000.0, 3, 0x100),
            FakeMessage(1000.0002, 0, 0x101),
            FakeMessage(1000.5, 0, 0x101),
        ]
        written, _ = run_cut(messages, 0.0, 0.0)
        self.assertEqual([message.timestamp for message in written], [1000.0])

    def test_bus_filter_uses_gui_bus_mapping(self):
        mapping = CanBusMapping.from_raw_channels((0, 1))
        messages = [
            FakeMessage(1000.0, 0, 0x100),
            FakeMessage(1001.0, 1, 0x101),
            FakeMessage(1002.0, 0, 0x100),
            FakeMessage(1003.0, 1, 0x101),
        ]
        written, _ = run_cut(messages, 0.0, 10.0, bus_filter=[2], mapping=mapping)
        self.assertEqual([message.channel for message in written], [1, 1])

    def test_filtered_bus_still_uses_global_time_base(self):
        mapping = CanBusMapping.from_raw_channels((0, 1))
        messages = [
            FakeMessage(1000.0, 1, 0x100),
            FakeMessage(1005.0, 0, 0x101),
            FakeMessage(1120.0, 0, 0x101),
        ]
        written, _ = run_cut(messages, 0.0, 10.0, bus_filter=[1], mapping=mapping)
        self.assertEqual([message.timestamp for message in written], [1005.0])

    def test_single_bus_window_matches_legacy_selection(self):
        timestamps = [1000.0, 1000.01, 1000.02, 1000.5, 1001.0, 1002.0, 1010.0]
        messages = [FakeMessage(stamp, 0, 0x200) for stamp in timestamps]
        written, _ = run_cut(messages, 0.0, 1.0)
        expected = [
            message for message in messages
            if 0.0 <= message.timestamp - 1000.0 <= 1.0
        ]
        self.assertEqual(len(written), len(expected))
        self.assertEqual(written, expected)

    def test_writer_without_on_message_received_uses_write(self):
        messages = [FakeMessage(1000.0, 0, 0x100), FakeMessage(1001.0, 0, 0x100)]
        written, _ = run_cut(messages, 0.0, 5.0, writer=WriteWriter)
        self.assertEqual(len(written), 2)

    def test_empty_window_reports_total_message_count(self):
        messages = [FakeMessage(1000.0, 0, 0x100), FakeMessage(1010.0, 0, 0x100)]
        with self.assertRaises(Exception) as caught:
            run_cut(messages, 100.0, 110.0)
        self.assertIn("总报文数: 2", str(caught.exception))

    def test_empty_file_reports_zero_messages(self):
        with self.assertRaises(Exception) as caught:
            run_cut([], 0.0, 10.0)
        self.assertIn("总报文数: 0", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
