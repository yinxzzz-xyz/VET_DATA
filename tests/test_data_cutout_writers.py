"""Cut-out writers: real BLF/MDF/CSV files, atomic output and cancel hooks."""

import os
import tempfile
import unittest
from pathlib import Path
from threading import Event

import numpy as np
import pandas as pd
import asammdf
import can
from asammdf import Signal

from vet_data_modular.data_cutout import (
    CutoutCancelled,
    CutoutWindow,
    SignalSeries,
    atomic_output,
    cut_blf_raw,
    cut_csv_frame,
    cut_mdf_file,
    cut_signal_mdf,
    mdf_signal_count,
)
from vet_data_modular.workers import CanBusMapping


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


def make_mdf(path, channels):
    mdf = asammdf.MDF(version="4.10")
    try:
        for name, timestamps, samples in channels:
            mdf.append(Signal(
                samples=np.asarray(samples, dtype=float),
                timestamps=np.asarray(timestamps, dtype=float),
                name=name,
                unit="u",
            ))
        mdf.save(str(path), overwrite=True)
    finally:
        mdf.close()


class CsvWriterTests(unittest.TestCase):
    def setUp(self):
        self.frame = pd.DataFrame(
            {"a": [1.0, 2.0, 3.0, 4.0], "b": [5.0, 6.0, 7.0, 8.0]},
            index=[0.0, 1.0, 2.0, 3.0],
        )
        self.frame.index.name = "timestamp"
        self.window = CutoutWindow(1.0, 3.0)

    def test_all_columns_are_sliced_without_resampling(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "out.csv")
            result = cut_csv_frame(self.frame, path, self.window)
            frame = pd.read_csv(path)
        self.assertEqual(result.row_count, 3)
        self.assertEqual(result.channel_count, 2)
        self.assertEqual(list(frame.columns), ["timestamp", "a", "b"])
        np.testing.assert_allclose(frame["a"], [2.0, 3.0, 4.0])
        np.testing.assert_allclose(frame["timestamp"], [1.0, 2.0, 3.0])

    def test_selected_columns_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "out.csv")
            cut_csv_frame(self.frame, path, self.window, columns=("b",))
            frame = pd.read_csv(path)
        self.assertEqual(list(frame.columns), ["timestamp", "b"])

    def test_missing_column_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                cut_csv_frame(self.frame, Path(folder, "out.csv"), self.window, columns=("zzz",))

    def test_calculated_channel_is_interpolated(self):
        calculated = SignalSeries(
            "calc", "", "", np.array([0.0, 1.0, 2.0, 3.0]), np.array([0.0, 10.0, 20.0, 30.0]),
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "out.csv")
            cut_csv_frame(self.frame, path, self.window, calculated=(calculated,))
            frame = pd.read_csv(path)
        np.testing.assert_allclose(frame["calc"], [10.0, 20.0, 30.0])

    def test_empty_window_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                cut_csv_frame(self.frame, Path(folder, "out.csv"), CutoutWindow(10.0, 20.0))


class AtomicOutputTests(unittest.TestCase):
    def test_existing_target_is_replaced_only_on_success(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder, "out.mf4")
            target.write_bytes(b"old")
            with atomic_output(target) as temporary:
                self.assertEqual(temporary.suffix, ".mf4")
                temporary.write_bytes(b"new")
                self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual(list(Path(folder).glob("*.part*")), [])

    def test_failure_keeps_target_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder, "out.mf4")
            target.write_bytes(b"old")
            with self.assertRaises(RuntimeError):
                with atomic_output(target) as temporary:
                    temporary.write_bytes(b"partial")
                    raise RuntimeError("boom")
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(list(Path(folder).glob("*.part*")), [])


class SignalMdfWriterTests(unittest.TestCase):
    def test_channels_are_clipped_to_window(self):
        series = (
            SignalSeries("speed", "km/h", "speed", np.array([0.0, 1.0, 2.0, 3.0]),
                         np.array([10.0, 11.0, 12.0, 13.0])),
            SignalSeries("rpm", "rpm", "rpm", np.array([0.5, 2.5, 4.5]),
                         np.array([100.0, 200.0, 300.0])),
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "signals.mf4")
            result = cut_signal_mdf(series, path, CutoutWindow(1.0, 3.0))
            mdf = asammdf.MDF(path)
            try:
                self.assertEqual(result.channel_count, 2)
                self.assertEqual(result.sample_count, 4)
                self.assertEqual(mdf_signal_count(mdf), 2)
                speed = mdf.get("speed")
                np.testing.assert_allclose(speed.samples, [11.0, 12.0, 13.0])
                self.assertEqual(mdf.get_channel_unit("speed"), "km/h")
                rpm = mdf.get("rpm")
                np.testing.assert_allclose(rpm.samples, [200.0])
            finally:
                mdf.close()

    def test_empty_signals_are_skipped_and_nothing_is_written(self):
        series = (SignalSeries("empty", "", "", np.array([]), np.array([])),)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "signals.mf4")
            with self.assertRaises(ValueError):
                cut_signal_mdf(series, path, CutoutWindow(1.0, 3.0))
            self.assertFalse(path.exists())


class MdfFileWriterTests(unittest.TestCase):
    def test_cut_keeps_all_channels(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, "source.mf4")
            output = Path(folder, "out.mf4")
            make_mdf(source, (
                ("one", [0.0, 1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0]),
                ("two", [0.0, 1.0, 2.0, 3.0], [5.0, 6.0, 7.0, 8.0]),
            ))
            result = cut_mdf_file(source, output, CutoutWindow(1.0, 2.0))
            mdf = asammdf.MDF(output)
            try:
                names = [group.channels[1].name for group in mdf.groups]
                self.assertEqual(sorted(names), ["one", "two"])
                self.assertEqual(result.channel_count, 2)
                np.testing.assert_allclose(mdf.get("one").samples, [2.0, 3.0])
            finally:
                mdf.close()

    def test_selected_channels_only(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, "source.mf4")
            output = Path(folder, "out.mf4")
            make_mdf(source, (
                ("one", [0.0, 1.0, 2.0], [1.0, 2.0, 3.0]),
                ("two", [0.0, 1.0, 2.0], [5.0, 6.0, 7.0]),
            ))
            result = cut_mdf_file(
                source, output, CutoutWindow(0.0, 2.0), channels=(("two", 1, 1),),
            )
            mdf = asammdf.MDF(output)
            try:
                self.assertEqual([group.channels[1].name for group in mdf.groups], ["two"])
                self.assertEqual(result.channel_count, 1)
            finally:
                mdf.close()


class BlfWriterTests(unittest.TestCase):
    base = 1_700_000_000.0

    def messages(self):
        return [
            can.Message(timestamp=self.base + 0.5, arbitration_id=0x100, data=b"\x01",
                        channel=1, is_extended_id=False),
            can.Message(timestamp=self.base + 0.5, arbitration_id=0x101, data=b"\x02",
                        channel=2, is_extended_id=False),
            can.Message(timestamp=self.base + 1.0, arbitration_id=0x100, data=b"\x03",
                        channel=1, is_extended_id=False),
            can.Message(timestamp=self.base + 2.0, arbitration_id=0x101, data=b"\x04",
                        channel=2, is_extended_id=False),
            can.Message(timestamp=self.base + 10.0, arbitration_id=0x100, data=b"\x05",
                        channel=1, is_extended_id=False),
        ]

    def test_window_copies_every_bus_with_original_timestamps(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, "source.blf")
            output = Path(folder, "out.blf")
            make_blf(source, self.messages())
            result = cut_blf_raw(source, output, CutoutWindow(0.0, 2.0))
            written = read_blf(output)
        self.assertEqual(result.message_count, 4)
        self.assertEqual(dict(result.bus_counts), {1: 2, 2: 2})
        self.assertEqual([message.channel for message in written], [1, 2, 1, 2])
        np.testing.assert_allclose(
            [message.timestamp for message in written],
            [self.base + 0.5, self.base + 0.5, self.base + 1.0, self.base + 2.0],
        )

    def test_bus_filter_uses_gui_mapping(self):
        mapping = CanBusMapping.from_raw_channels((1, 2))
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, "source.blf")
            output = Path(folder, "out.blf")
            make_blf(source, self.messages())
            result = cut_blf_raw(
                source, output, CutoutWindow(0.0, 2.0), mapping=mapping, bus_filter=[2],
            )
            written = read_blf(output)
        self.assertEqual(result.message_count, 2)
        self.assertEqual({message.channel for message in written}, {2})

    def test_empty_window_reports_scanned_total(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, "source.blf")
            make_blf(source, self.messages())
            with self.assertRaises(ValueError) as caught:
                cut_blf_raw(source, Path(folder, "out.blf"), CutoutWindow(100.0, 110.0))
        self.assertIn("总报文数: 5", str(caught.exception))

    def test_cancel_before_work_raises(self):
        event = Event()
        event.set()
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, "source.blf")
            make_blf(source, self.messages())
            with self.assertRaises(CutoutCancelled):
                cut_blf_raw(
                    source, Path(folder, "out.blf"), CutoutWindow(0.0, 2.0),
                    cancel_event=event,
                )

    def test_progress_callback_reports_scanned_and_written(self):
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, "source.blf")
            make_blf(source, self.messages())
            cut_blf_raw(
                source, Path(folder, "out.blf"), CutoutWindow(0.0, 2.0),
                progress_callback=lambda scanned, written: seen.append((scanned, written)),
                progress_interval=2,
            )
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], 5)
        self.assertEqual(seen[-1][1], 4)


if __name__ == "__main__":
    unittest.main()
