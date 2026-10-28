import contextlib
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import can
import cantools

from vet_data_modular import baseline
from vet_data_modular.workers import CanFileIdentity, CanRawMessageCache, normalized_can_path


def load_dbc(body):
    return cantools.database.load_string(
        'VERSION ""\n\nNS_ :\n\nBS_:\n\nBU_: Vector__XXX\n\n' + body,
        database_format="dbc",
    )


def message(channel, arbitration_id, data, timestamp, *, is_fd=False):
    return can.Message(
        timestamp=timestamp,
        arbitration_id=arbitration_id,
        is_extended_id=False,
        data=data,
        channel=channel,
        is_fd=is_fd,
    )


class FakeReader:
    def __init__(self, messages, error=None):
        self.messages = list(messages)
        self.error = error
        self.stopped = False
        self.yielded = 0

    def __iter__(self):
        for item in self.messages:
            self.yielded += 1
            yield item
        if self.error:
            raise self.error

    def stop(self):
        self.stopped = True


class CanMultiBusParseTests(unittest.TestCase):
    def setUp(self):
        self.identity = CanFileIdentity(
            normalized_can_path("input.blf"), 100, 200
        )

    def run_batch(self, definitions, messages, *, identity_values=None):
        parsers = []
        for bus_id, raw_channel, db in definitions:
            msg_count = sum(
                baseline.message_raw_channel(item) == raw_channel
                for item in messages
            )
            parsers.append(
                baseline.ParseWorker(
                    bus_id, raw_channel, msg_count, "input.blf", self.identity, db
                )
            )
        results = {"finished": [], "errors": [], "cancelled": [], "progress": []}
        for parser in parsers:
            parser.parse_finished.connect(
                lambda bus, stats: results["finished"].append((bus, stats))
            )
            parser.parse_error.connect(
                lambda bus, error: results["errors"].append((bus, error))
            )
            parser.parse_cancelled.connect(results["cancelled"].append)
            parser.progress_updated.connect(
                lambda bus, progress, text: results["progress"].append(
                    (bus, progress, text)
                )
            )
        reader = FakeReader(messages)
        identity_values = identity_values or [self.identity, self.identity]
        with patch.object(
            baseline.can, "BLFReader", return_value=reader
        ) as reader_factory, patch.object(
            baseline, "capture_can_file_identity", side_effect=identity_values
        ), contextlib.redirect_stdout(io.StringIO()):
            batch = baseline.MultiBusParseWorker(parsers)
            batch.run()
        self.assertEqual(reader_factory.call_count, 1)
        self.assertTrue(reader.stopped)
        return batch, parsers, reader, results

    def test_interleaved_channels_same_id_keep_bus_specific_dbc_and_global_t0(self):
        db0 = load_dbc(
            'BO_ 256 Left: 1 Vector__XXX\n'
            ' SG_ left_value : 0|8@1+ (1,0) [0|255] "" Vector__XXX\n'
        )
        db1 = load_dbc(
            'BO_ 256 Right: 1 Vector__XXX\n'
            ' SG_ right_value : 0|8@1+ (2,1) [0|511] "" Vector__XXX\n'
        )
        messages = [
            message(1, 0x100, b"\x05", 3.0),
            message(0, 0x100, b"\x0A", 4.0),
            message(1, 0x100, b"\x07", 5.0),
            message(0, 0x100, b"\x14", 6.0),
        ]
        _batch, _parsers, _reader, results = self.run_batch(
            [(1, 0, db0), (2, 1, db1)], messages
        )
        stats = {bus: data for bus, data in results["finished"]}
        self.assertEqual(set(stats), {1, 2})
        self.assertEqual(stats[1]["pool"]["left_value"]["t"], [1.0, 3.0])
        self.assertEqual(stats[1]["pool"]["left_value"]["v"], [10.0, 20.0])
        self.assertEqual(stats[2]["pool"]["right_value"]["t"], [0.0, 2.0])
        self.assertEqual(stats[2]["pool"]["right_value"]["v"], [11.0, 15.0])
        self.assertEqual(stats[1]["scanned_frames"], 2)
        self.assertEqual(stats[2]["scanned_frames"], 2)
        self.assertEqual([bus for bus, _value, _text in results["progress"]], [2, 1, 2, 1])

    def test_can_fd_truncated_frame_is_decoded_in_its_own_context(self):
        db0 = load_dbc(
            'BO_ 512 Truncated: 8 Vector__XXX\n'
            ' SG_ early : 0|8@1+ (1,0) [0|255] "" Vector__XXX\n'
            ' SG_ late : 56|8@1+ (1,0) [0|255] "" Vector__XXX\n'
        )
        db1 = load_dbc(
            'BO_ 256 Other: 1 Vector__XXX\n'
            ' SG_ other : 0|8@1+ (1,0) [0|255] "" Vector__XXX\n'
        )
        messages = [
            message(0, 0x200, b"\x2A\x00\x00\x00", 10.0, is_fd=True),
            message(1, 0x100, b"\x19", 11.0),
        ]
        _batch, _parsers, _reader, results = self.run_batch(
            [(1, 0, db0), (2, 1, db1)], messages
        )
        stats = {bus: data for bus, data in results["finished"]}
        self.assertEqual(stats[1]["pool"]["early"]["v"], [42.0])
        self.assertNotIn("late", stats[1]["pool"])
        self.assertEqual(stats[1]["truncated_frames"], 1)
        self.assertEqual(stats[2]["pool"]["other"]["v"], [25.0])

    def test_container_pdu_frame_uses_original_parser_logic(self):
        class Signal:
            def __init__(self, name, start, length, *, multiplex=None):
                self.name = name
                self.start = start
                self.length = length
                self.scale = 1
                self.offset = 0
                self.unit = ""
                self.is_little_endian = True
                self.minimum = 0
                self.maximum = 255
                self.multiplex = multiplex

        class Definition:
            frame_id = 0x13E
            name = "Container"
            length = 8
            is_extended_frame = False
            signals = [
                Signal("header_id", 0, 24),
                Signal("header_dlc", 24, 8),
                Signal("pdu_m1_value", 32, 8, multiplex=1),
            ]

        database = SimpleNamespace(messages=[Definition()])
        messages = [message(0, 0x13E, b"\x00\x00\x01\x01\x2A", 2.0)]
        _batch, _parsers, _reader, results = self.run_batch(
            [(1, 0, database)], messages
        )
        stats = {bus: data for bus, data in results["finished"]}[1]
        self.assertEqual(stats["pool"]["pdu_m1_value"]["t"], [0.0])
        self.assertEqual(stats["pool"]["pdu_m1_value"]["v"], [42])

    def test_complete_raw_cache_replays_only_incremental_filtered_database(self):
        full_db = load_dbc(
            'BO_ 256 Values: 1 Vector__XXX\n'
            ' SG_ first : 0|4@1+ (1,0) [0|15] "" Vector__XXX\n'
            ' SG_ second : 4|4@1+ (1,0) [0|15] "" Vector__XXX\n'
        )
        first_db, _ = baseline.BusConfigWidget._build_filtered_database(
            full_db, {0x100: {"first"}}
        )
        second_db, _ = baseline.BusConfigWidget._build_filtered_database(
            full_db, {0x100: {"second"}}
        )
        frames = [message(0, 0x100, b"\x21", 1.0), message(0, 0x100, b"\x43", 2.0)]
        cache = CanRawMessageCache(self.identity, 0, {(0x100, False)}, (self.identity, 0))
        first_parser = baseline.ParseWorker(1, 0, 2, "input.blf", self.identity, first_db)
        first_parser.raw_cache_collectors = {0: (cache,)}
        first_results = []
        first_parser.parse_finished.connect(lambda _bus, stats: first_results.append(stats))
        reader = FakeReader(frames)
        with patch.object(baseline.can, "BLFReader", return_value=reader), patch.object(
            baseline, "capture_can_file_identity", return_value=self.identity
        ), contextlib.redirect_stdout(io.StringIO()):
            first_parser.run()
        self.assertEqual(first_results[0]["pool"].keys(), {"first"})
        self.assertTrue(cache.complete)
        self.assertEqual(cache.frame_count, 2)

        incremental = baseline.ParseWorker(1, 0, 2, "input.blf", self.identity, second_db)
        incremental.raw_message_cache = cache
        results = []
        incremental.parse_finished.connect(lambda _bus, stats: results.append(stats))
        with patch.object(
            baseline.can, "BLFReader", side_effect=AssertionError("cache replay must not scan BLF")
        ), patch.object(
            baseline, "capture_can_file_identity", return_value=self.identity
        ), contextlib.redirect_stdout(io.StringIO()):
            incremental.run()
        self.assertEqual(results[0]["pool"].keys(), {"second"})
        self.assertTrue(results[0]["cache_hit"])
        self.assertEqual(results[0]["cache_replayed_frames"], 2)
        self.assertEqual(results[0]["full_file_scan_seconds"], 0.0)

    def test_raw_cache_round_trip_preserves_fd_extended_frames_and_timestamps(self):
        cache = CanRawMessageCache(
            self.identity, 0, {(0x123, False), (0x123, True)}, ("fd",)
        )
        standard = can.Message(
            timestamp=10.25, arbitration_id=0x123, is_extended_id=False,
            data=b"\x01\x02", channel=0,
        )
        extended_fd = can.Message(
            timestamp=10.75, arbitration_id=0x123, is_extended_id=True,
            data=bytes(range(12)), channel=0, is_fd=True, bitrate_switch=True,
            error_state_indicator=True,
        )
        cache.append(standard, 10.0, raw_channel=0)
        cache.append(extended_fd, 10.0, raw_channel=0)
        cache.finish_scan(10.0)
        recovered = {
            (msg.arbitration_id, msg.is_extended_id): (msg, file_t0)
            for msg, file_t0 in cache.iter_messages({(0x123, False), (0x123, True)})
        }
        normal, normal_t0 = recovered[(0x123, False)]
        fd, fd_t0 = recovered[(0x123, True)]
        self.assertEqual((normal.timestamp, normal.data, normal_t0), (10.25, b"\x01\x02", 10.0))
        self.assertEqual(fd.timestamp, 10.75)
        self.assertEqual(fd.data, bytes(range(12)))
        self.assertTrue(fd.is_fd)
        self.assertTrue(fd.bitrate_switch)
        self.assertTrue(fd.error_state_indicator)
        self.assertEqual(fd_t0, 10.0)

    def test_single_bus_cancel_does_not_cancel_other_bus_and_all_cancel_stops(self):
        db = load_dbc(
            'BO_ 256 Value: 1 Vector__XXX\n'
            ' SG_ value : 0|8@1+ (1,0) [0|255] "" Vector__XXX\n'
        )
        messages = [
            message(0, 0x100, b"\x01", 1.0),
            message(1, 0x100, b"\x02", 2.0),
        ]
        batch, parsers, reader, results = self.run_batch(
            [(1, 0, db), (2, 1, db)], messages
        )
        self.assertEqual(results["cancelled"], [])
        self.assertEqual(len(results["finished"]), 2)

        parsers = [
            baseline.ParseWorker(1, 0, 1, "input.blf", self.identity, db),
            baseline.ParseWorker(2, 1, 1, "input.blf", self.identity, db),
        ]
        cancelled = {"finished": [], "cancelled": []}
        for parser in parsers:
            parser.parse_finished.connect(
                lambda bus, _stats: cancelled["finished"].append(bus)
            )
            parser.parse_cancelled.connect(cancelled["cancelled"].append)
        reader = FakeReader(messages)
        with patch.object(baseline.can, "BLFReader", return_value=reader), patch.object(
            baseline, "capture_can_file_identity", return_value=self.identity
        ), contextlib.redirect_stdout(io.StringIO()):
            batch = baseline.MultiBusParseWorker(parsers)
            batch.cancel_bus(1)
            batch.run()
        self.assertEqual(cancelled["cancelled"], [1])
        self.assertEqual(cancelled["finished"], [2])
        self.assertTrue(reader.stopped)

        parsers = [
            baseline.ParseWorker(1, 0, 1, "input.blf", self.identity, db),
            baseline.ParseWorker(2, 1, 1, "input.blf", self.identity, db),
        ]
        cancelled_all = []
        for parser in parsers:
            parser.parse_cancelled.connect(cancelled_all.append)
        reader = FakeReader(messages)
        with patch.object(baseline.can, "BLFReader", return_value=reader), patch.object(
            baseline, "capture_can_file_identity", return_value=self.identity
        ), contextlib.redirect_stdout(io.StringIO()):
            batch = baseline.MultiBusParseWorker(parsers)
            batch.cancel_bus(1)
            batch.cancel_bus(2)
            batch.run()
        self.assertEqual(set(cancelled_all), {1, 2})
        self.assertLessEqual(reader.yielded, 256)
        self.assertTrue(reader.stopped)

    def test_cancelled_initial_scan_discards_partial_raw_cache(self):
        db = load_dbc(
            'BO_ 256 Value: 1 Vector__XXX\n'
            ' SG_ value : 0|8@1+ (1,0) [0|255] "" Vector__XXX\n'
        )
        cache = CanRawMessageCache(self.identity, 0, {(0x100, False)}, ("ctx",))
        parser = baseline.ParseWorker(1, 0, 1, "input.blf", self.identity, db)
        parser.raw_cache_collectors = {0: (cache,)}
        cancelled = []
        parser.parse_cancelled.connect(cancelled.append)
        parser.stop()
        reader = FakeReader([message(0, 0x100, b"\x01", 1.0)])
        with patch.object(baseline.can, "BLFReader", return_value=reader), patch.object(
            baseline, "capture_can_file_identity", return_value=self.identity
        ), contextlib.redirect_stdout(io.StringIO()):
            parser.run()
        self.assertEqual(cancelled, [1])
        self.assertFalse(cache.complete)
        self.assertEqual(cache.frame_count, 0)

    def test_reader_error_and_file_identity_change_fail_each_active_bus(self):
        db = load_dbc(
            'BO_ 256 Value: 1 Vector__XXX\n'
            ' SG_ value : 0|8@1+ (1,0) [0|255] "" Vector__XXX\n'
        )
        parsers = [
            baseline.ParseWorker(1, 0, 1, "input.blf", self.identity, db),
            baseline.ParseWorker(2, 1, 1, "input.blf", self.identity, db),
        ]
        errors = []
        for parser in parsers:
            parser.parse_error.connect(lambda bus, error: errors.append((bus, error)))
        reader = FakeReader([], error=OSError("reader broke"))
        with patch.object(baseline.can, "BLFReader", return_value=reader), patch.object(
            baseline, "capture_can_file_identity", return_value=self.identity
        ), contextlib.redirect_stdout(io.StringIO()):
            baseline.MultiBusParseWorker(parsers).run()
        self.assertEqual({bus for bus, _error in errors}, {1, 2})
        self.assertTrue(all("reader broke" in error for _bus, error in errors))
        self.assertTrue(reader.stopped)

        changed = CanFileIdentity(self.identity.path, 101, self.identity.mtime_ns)
        messages = [
            message(0, 0x100, b"\x01", 1.0),
            message(1, 0x100, b"\x02", 2.0),
        ]
        parsers = [
            baseline.ParseWorker(1, 0, 1, "input.blf", self.identity, db),
            baseline.ParseWorker(2, 1, 1, "input.blf", self.identity, db),
        ]
        errors = []
        for parser in parsers:
            parser.parse_error.connect(lambda bus, error: errors.append((bus, error)))
        with patch.object(baseline.can, "BLFReader", return_value=FakeReader(messages)), patch.object(
            baseline,
            "capture_can_file_identity",
            side_effect=[self.identity, changed],
        ), contextlib.redirect_stdout(io.StringIO()):
            baseline.MultiBusParseWorker(parsers).run()
        self.assertEqual({bus for bus, _error in errors}, {1, 2})
        self.assertTrue(all("发生变化" in error for _bus, error in errors))

    def test_scan_count_mismatch_never_emits_success(self):
        db = load_dbc(
            'BO_ 256 Value: 1 Vector__XXX\n'
            ' SG_ value : 0|8@1+ (1,0) [0|255] "" Vector__XXX\n'
        )
        parser = baseline.ParseWorker(
            1, 0, 2, "input.blf", self.identity, db
        )
        finished = []
        errors = []
        parser.parse_finished.connect(lambda bus, stats: finished.append((bus, stats)))
        parser.parse_error.connect(lambda bus, error: errors.append((bus, error)))
        reader = FakeReader([message(0, 0x100, b"\x01", 1.0)])
        with patch.object(baseline.can, "BLFReader", return_value=reader), patch.object(
            baseline, "capture_can_file_identity", return_value=self.identity
        ), contextlib.redirect_stdout(io.StringIO()):
            baseline.MultiBusParseWorker([parser]).run()
        self.assertFalse(finished)
        self.assertEqual(len(errors), 1)
        self.assertIn("扫描计数与探测快照不一致", errors[0][1])
        self.assertTrue(reader.stopped)


if __name__ == "__main__":
    unittest.main()
