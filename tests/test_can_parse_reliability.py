import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from types import MappingProxyType
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import can
import cantools
from cantools.database.can import Database, Message, Signal
from PyQt6.QtWidgets import QApplication

from vet_data_modular import baseline
from vet_data_modular.window import MDFPlotter
from vet_data_modular.workers import (
    CanBusCountSnapshot,
    CanBusMapping,
    CanDetectionSnapshot,
    CanFileIdentity,
    capture_can_file_identity,
    normalized_can_path,
)


APP = QApplication.instance() or QApplication([])


def load_dbc(body):
    return cantools.database.load_string(
        'VERSION ""\n\nNS_ :\n\nBS_:\n\nBU_: Vector__XXX\n\n' + body,
        database_format="dbc",
    )


def frame(
    arbitration_id,
    data,
    *,
    is_fd=False,
    is_extended_id=False,
    channel=1,
    timestamp=1.0,
):
    return can.Message(
        timestamp=timestamp,
        arbitration_id=arbitration_id,
        is_extended_id=is_extended_id,
        is_fd=is_fd,
        channel=channel,
        data=data,
    )


class FakeReader:
    def __init__(self, messages):
        self.messages = list(messages)
        self.stopped = False

    def __iter__(self):
        return iter(self.messages)

    def stop(self):
        self.stopped = True


class ParseWorkerReliabilityTests(unittest.TestCase):
    def run_worker(self, database, messages):
        identity = CanFileIdentity(normalized_can_path("input.blf"), 100, 200)
        worker = baseline.ParseWorker(
            1, 1, len(messages), "input.blf", identity, database
        )
        finished = []
        errors = []
        progress = []
        worker.parse_finished.connect(lambda bus, stats: finished.append((bus, stats)))
        worker.parse_error.connect(lambda bus, error: errors.append((bus, error)))
        worker.progress_updated.connect(
            lambda bus, value, text: progress.append((bus, value, text))
        )
        reader = FakeReader(messages)
        with patch.object(baseline.can, "BLFReader", return_value=reader), patch.object(
            baseline, "capture_can_file_identity", return_value=identity
        ), contextlib.redirect_stdout(io.StringIO()):
            worker.run()
        self.assertFalse(errors)
        self.assertEqual(len(finished), 1)
        return finished[0][1], progress, reader

    def test_classic_can_and_full_can_fd_payload_decode_normally(self):
        database = load_dbc(
            "BO_ 256 Classic: 1 Vector__XXX\n"
            " SG_ classic_value : 0|8@1+ (1,0) [0|255] \"\" Vector__XXX\n\n"
            "BO_ 512 FullFd: 8 Vector__XXX\n"
            " SG_ fd_value : 56|8@1+ (1,0) [0|255] \"\" Vector__XXX\n"
        )
        messages = [
            frame(0x100, b"\x11"),
            frame(0x200, b"\x00\x00\x00\x00\x00\x00\x00\x22", is_fd=True),
        ]
        stats, progress, _reader = self.run_worker(database, messages)
        self.assertEqual(stats["pool"]["classic_value"]["v"], [17.0])
        self.assertEqual(stats["pool"]["fd_value"]["v"], [34.0])
        self.assertEqual(stats["decoded_frames"], 2)
        self.assertEqual(stats["decode_failed_frames"], 0)
        self.assertNotIn(100, [value for _bus, value, _text in progress])

    def test_truncated_can_fd_returns_only_complete_signals_without_padding(self):
        database = load_dbc(
            "BO_ 512 TruncatedFd: 8 Vector__XXX\n"
            " SG_ early : 0|8@1+ (1,0) [0|255] \"\" Vector__XXX\n"
            " SG_ late : 56|8@1+ (1,0) [0|255] \"\" Vector__XXX\n"
        )
        stats, _progress, _reader = self.run_worker(
            database, [frame(0x200, b"\x2A\x00\x00\x00", is_fd=True)]
        )
        self.assertEqual(stats["pool"]["early"]["v"], [42.0])
        self.assertNotIn("late", stats["pool"])
        self.assertEqual(stats["truncated_frames"], 1)
        self.assertEqual(stats["decoded_frames"], 1)
        self.assertEqual(stats["decode_failed_frames"], 0)

    def test_standard_and_extended_frames_with_same_id_do_not_cross_match(self):
        database = load_dbc(
            "BO_ 256 Standard: 1 Vector__XXX\n"
            " SG_ standard_value : 0|8@1+ (1,0) [0|255] \"\" Vector__XXX\n\n"
            "BO_ 2147483904 Extended: 1 Vector__XXX\n"
            " SG_ extended_value : 0|8@1+ (1,0) [0|255] \"\" Vector__XXX\n"
        )
        messages = [
            frame(0x100, b"\x0B", is_extended_id=False),
            frame(0x100, b"\x16", is_extended_id=True, timestamp=2.0),
        ]
        stats, _progress, _reader = self.run_worker(database, messages)
        self.assertEqual(stats["pool"]["standard_value"]["v"], [11.0])
        self.assertEqual(stats["pool"]["extended_value"]["v"], [22.0])
        self.assertEqual(stats["dbc_matched_frames"], 2)

    def test_many_undefined_frames_are_counted_without_hiding_valid_data(self):
        database = load_dbc(
            "BO_ 256 Defined: 1 Vector__XXX\n"
            " SG_ value : 0|8@1+ (1,0) [0|255] \"\" Vector__XXX\n"
        )
        messages = [frame(0x300 + index, b"\x00", timestamp=float(index)) for index in range(50)]
        messages.append(frame(0x100, b"\x2A", timestamp=51.0))
        stats, _progress, _reader = self.run_worker(database, messages)
        self.assertEqual(stats["scanned_frames"], 51)
        self.assertEqual(stats["dbc_undefined_frames"], 50)
        self.assertEqual(stats["dbc_matched_frames"], 1)
        self.assertEqual(stats["pool"]["value"]["v"], [42.0])

    def test_matched_frame_decode_failure_is_counted_and_not_worker_error(self):
        class BrokenMessage:
            frame_id = 0x100
            is_extended_frame = False
            name = "Broken"
            length = 1
            signals = [SimpleNamespace(name="value")]

            def decode(self, *_args, **_kwargs):
                raise ValueError("broken definition")

        stats, _progress, _reader = self.run_worker(
            SimpleNamespace(messages=[BrokenMessage()]), [frame(0x100, b"\x01")]
        )
        self.assertEqual(stats["dbc_matched_frames"], 1)
        self.assertEqual(stats["decode_failed_frames"], 1)
        self.assertEqual(stats["decode_error_counts"], {"ValueError": 1})
        self.assertEqual(stats["generated_signal_count"], 0)

    def test_dbc_without_signals_and_no_matching_id_have_distinct_stats(self):
        no_signals = load_dbc("BO_ 256 Empty: 1 Vector__XXX\n")
        empty_stats, _progress, _reader = self.run_worker(
            no_signals, [frame(0x100, b"\x01")]
        )
        self.assertEqual(empty_stats["dbc_signal_count"], 0)
        self.assertEqual(empty_stats["dbc_matched_frames"], 1)
        self.assertEqual(empty_stats["generated_signal_count"], 0)

        defined = load_dbc(
            "BO_ 256 Defined: 1 Vector__XXX\n"
            " SG_ value : 0|8@1+ (1,0) [0|255] \"\" Vector__XXX\n"
        )
        unmatched_stats, _progress, _reader = self.run_worker(
            defined, [frame(0x321, b"\x01")]
        )
        self.assertEqual(unmatched_stats["dbc_signal_count"], 1)
        self.assertEqual(unmatched_stats["dbc_matched_frames"], 0)
        self.assertEqual(unmatched_stats["dbc_undefined_frames"], 1)
        self.assertEqual(unmatched_stats["generated_signal_count"], 0)


class FilteredDatabaseReliabilityTests(unittest.TestCase):
    @staticmethod
    def database(*messages):
        return Database(messages=list(messages), sort_signals=None)

    def test_partial_and_all_signal_filters_decode_immediately(self):
        original = self.database(Message(
            frame_id=0x100,
            name="Classic",
            length=2,
            signals=[
                Signal("first", 0, 8),
                Signal("second", 8, 8),
            ],
            cycle_time=10,
            senders=["ECU"],
            comment="classic message",
            bus_name="PTCAN",
            sort_signals=None,
        ))
        data = b"\x11\x22"
        original_values = original.get_message_by_frame_id(0x100).decode(data)

        partial, _auto_added = baseline.BusConfigWidget._build_filtered_database(
            original, {0x100: ["second"]}
        )
        partial_message = partial.get_message_by_frame_id(0x100)
        self.assertEqual(partial_message.decode(data), {"second": original_values["second"]})
        self.assertEqual(partial_message.cycle_time, 10)
        self.assertEqual(partial_message.senders, ["ECU"])
        self.assertEqual(partial_message.comment, "classic message")
        self.assertEqual(partial_message.bus_name, "PTCAN")

        complete, _auto_added = baseline.BusConfigWidget._build_filtered_database(
            original, {0x100: ["first", "second"]}
        )
        self.assertEqual(
            complete.get_message_by_frame_id(0x100).decode(data),
            original_values,
        )

    def test_filtered_can_fd_preserves_type_and_truncated_decode(self):
        original = self.database(Message(
            frame_id=0x200,
            name="FdMessage",
            length=8,
            signals=[
                Signal("early", 0, 8),
                Signal("late", 56, 8),
            ],
            is_fd=True,
            sort_signals=None,
        ))
        filtered, _auto_added = baseline.BusConfigWidget._build_filtered_database(
            original, {0x200: ["early", "late"]}
        )
        message = filtered.get_message_by_frame_id(0x200)
        self.assertTrue(message.is_fd)
        self.assertEqual(
            message.decode(b"\x2A\x00\x00\x00", allow_truncated=True),
            {"early": 42},
        )

    def test_filtered_extended_message_preserves_lookup_and_decode(self):
        original = self.database(Message(
            frame_id=0x123,
            name="Extended",
            length=1,
            signals=[Signal("value", 0, 8)],
            is_extended_frame=True,
        ))
        filtered, _auto_added = baseline.BusConfigWidget._build_filtered_database(
            original, {0x123: ["value"]}
        )
        message = filtered.get_message_by_frame_id(0x123, force_extended_id=True)
        self.assertTrue(message.is_extended_frame)
        self.assertEqual(message.decode(b"\x33"), {"value": 51})

    def test_empty_selection_keeps_existing_database_behavior(self):
        original = self.database(Message(
            frame_id=0x100,
            name="Classic",
            length=1,
            signals=[Signal("value", 0, 8)],
        ))
        filtered, auto_added = baseline.BusConfigWidget._build_filtered_database(
            original, {}
        )
        self.assertIs(filtered, original)
        self.assertEqual(auto_added, 0)
        self.assertEqual(filtered.get_message_by_frame_id(0x100).decode(b"\x07"), {"value": 7})


class FilteredProtocolLifecycleTests(unittest.TestCase):
    @staticmethod
    def database(name, frame_id=0x100):
        return Database(messages=[Message(
            frame_id=frame_id,
            name=name,
            length=1,
            signals=[Signal(f"{name}_value", 0, 8)],
            sort_signals=None,
        )], sort_signals=None)

    def select_filtered(self, widget, source_path, source_db, filtered_db, save_path=""):
        emitted = []
        widget.protocol_changed.connect(lambda bus, path: emitted.append((bus, path)))

        def filter_and_choose(_database, _bus_name):
            widget._save_filtered_dbc_with_custom_name(filtered_db, source_path)
            return filtered_db

        with patch.object(
            baseline.QFileDialog, "getOpenFileName", return_value=(source_path, "")
        ), patch.object(
            baseline.QFileDialog, "getSaveFileName", return_value=(save_path, "")
        ), patch.object(
            baseline.cantools.database, "load_file", return_value=source_db
        ), patch.object(
            widget, "filter_dbc_signals", side_effect=filter_and_choose
        ), patch.object(baseline.QMessageBox, "information"), patch.object(
            baseline.QMessageBox, "warning"
        ):
            widget.select_protocol()
        return emitted

    @staticmethod
    def configure_window(window, path, widget, protocol_info):
        mapping = CanBusMapping.from_raw_channels((1,))
        window.can_bus_mapping = mapping
        window.can_bus_data[1] = {
            "name": "Bus 1",
            "original_bus_id": 1,
            "actual_ids": [0x100],
            "id_count": 1,
            "msg_count": 1,
            "has_fd": False,
        }
        window.bus_protocols[1] = protocol_info
        window.bus_config_widgets[1] = widget
        window.mdf_path = str(path)
        window.can_detection_snapshot = CanDetectionSnapshot(
            capture_can_file_identity(path),
            mapping,
            MappingProxyType({1: CanBusCountSnapshot(1, 1, 1)}),
        )

    def assert_cached_parse(self, widget, protocol_info):
        window = MDFPlotter()
        widget.setParent(window)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.blf"
            path.write_bytes(b"stable")
            self.configure_window(window, path, widget, protocol_info)
            try:
                with patch.object(
                    baseline.cantools.database,
                    "load_file",
                    side_effect=AssertionError("cached parse must not load a protocol path"),
                ), patch.object(baseline.ParseWorker, "start"):
                    window.parse_bus(1)
                self.assertIs(window.parse_workers[1].db, widget._cached_filtered_db)
            finally:
                for dialog in window.progress_dialogs.values():
                    dialog.deleteLater()
                for worker in window.parse_workers.values():
                    worker.deleteLater()
                window.deleteLater()
                APP.processEvents()

    def test_cancel_save_keeps_real_path_and_parses_cached_database(self):
        widget = baseline.BusConfigWidget(1)
        source = self.database("Source")
        filtered = self.database("Filtered")
        source_path = str(Path("C:/data/PTCAN.dbc"))
        emitted = self.select_filtered(widget, source_path, source, filtered)

        self.assertEqual(widget.protocol_path, source_path)
        self.assertEqual(emitted[-1], (1, source_path))
        self.assertNotIn("[已筛选-内存]", emitted[-1][1])
        self.assertIn("[已筛选-内存]", widget.protocol_label.text())
        self.assertIs(widget._cached_filtered_db, filtered)
        self.assertIsNone(widget._filtered_dbc_path)
        self.assert_cached_parse(widget, source_path)

    def test_display_marker_is_never_sent_to_protocol_loader(self):
        widget = baseline.BusConfigWidget(1)
        widget._cached_filtered_db = self.database("Filtered")
        widget.set_protocol_display("PTCAN.dbc [已筛选-内存]")
        self.assert_cached_parse(widget, "[已筛选-内存]")

    def test_saved_filtered_dbc_path_and_cached_parse_remain_supported(self):
        widget = baseline.BusConfigWidget(1)
        with tempfile.TemporaryDirectory() as folder:
            saved_path = str(Path(folder) / "PTCAN_filtered.dbc")
            emitted = self.select_filtered(
                widget,
                str(Path(folder) / "PTCAN.dbc"),
                self.database("Source"),
                self.database("Filtered"),
                saved_path,
            )
            self.assertTrue(Path(saved_path).is_file())
            self.assertEqual(widget.protocol_path, saved_path)
            self.assertEqual(widget._filtered_dbc_path, saved_path)
            self.assertEqual(emitted[-1], (1, saved_path))
            self.assert_cached_parse(widget, saved_path)

    def test_reselect_clear_and_multi_bus_keep_caches_isolated(self):
        first = baseline.BusConfigWidget(1)
        second = baseline.BusConfigWidget(2)
        first_db = self.database("First", 0x100)
        replacement_db = self.database("Replacement", 0x101)
        second_db = self.database("Second", 0x200)

        self.select_filtered(first, "C:/data/first.dbc", self.database("Raw1"), first_db)
        self.select_filtered(second, "C:/data/second.dbc", self.database("Raw2"), second_db)
        self.assertIs(first._cached_filtered_db, first_db)
        self.assertIs(second._cached_filtered_db, second_db)

        self.select_filtered(
            first, "C:/data/replacement.dbc", self.database("Raw3"), replacement_db
        )
        self.assertIs(first._cached_filtered_db, replacement_db)
        self.assertIs(second._cached_filtered_db, second_db)
        self.assertEqual(first.protocol_path, "C:/data/replacement.dbc")

        first.clear_protocol()
        self.assertEqual(first.protocol_path, "")
        self.assertIsNone(first._cached_filtered_db)
        self.assertIsNone(first._filtered_dbc_path)
        self.assertIs(second._cached_filtered_db, second_db)


class ParseResultClassificationTests(unittest.TestCase):
    class Widget:
        def __init__(self):
            self.status = None

        def set_status(self, status):
            self.status = status

        def set_id_count(self, total, parsed=None):
            self.id_count = (total, parsed)

    class Dialog:
        def __init__(self):
            self.reason = None
            self.progress = []

        def set_empty_result(self, reason):
            self.reason = reason

        def update_progress(self, value, text):
            self.progress.append((value, text))

        def set_finished(self, success, stats):
            self.finished = (success, stats)

    def classify(self, **overrides):
        stats = {
            "pool": {},
            "dbc_signal_count": 1,
            "dbc_matched_frames": 1,
            "decode_failed_frames": 1,
        }
        stats.update(overrides)
        target = SimpleNamespace(
            _parsing_bus_ids={1},
            bus_config_widgets={1: self.Widget()},
            progress_dialogs={1: self.Dialog()},
            can_bus_data={1: {}},
            db_per_bus={1: {}},
            parse_workers={1: object()},
        )
        baseline.MDFPlotter.on_parse_finished(target, 1, stats)
        return target.can_bus_data[1]["parse_stats"], target.progress_dialogs[1]

    def test_dbc_without_signals_has_specific_actionable_reason(self):
        stats, dialog = self.classify(dbc_signal_count=0, dbc_matched_frames=0)
        self.assertEqual(stats["result_state"], "dbc_has_no_signals")
        self.assertIn("ARXML", dialog.reason)
        self.assertFalse(dialog.progress)

    def test_no_matching_frames_has_specific_actionable_reason(self):
        stats, dialog = self.classify(dbc_matched_frames=0)
        self.assertEqual(stats["result_state"], "no_matching_frames")
        self.assertIn("Bus/DBC", dialog.reason)
        self.assertFalse(dialog.progress)

    def test_all_matching_frames_failed_has_specific_actionable_reason(self):
        stats, dialog = self.classify()
        self.assertEqual(stats["result_state"], "all_matching_frames_failed")
        self.assertIn("CAN FD", dialog.reason)
        self.assertFalse(dialog.progress)

    def test_successful_but_empty_decodes_have_a_distinct_reason(self):
        stats, dialog = self.classify(
            empty_decoded_frames=10,
            decode_failed_frames=0,
        )
        self.assertEqual(stats["result_state"], "all_decodes_empty")
        self.assertIn("未返回信号", dialog.reason)
        self.assertFalse(dialog.progress)

    def test_success_is_the_only_result_that_advances_gui_to_100(self):
        dialog = self.Dialog()

        class Target:
            _parsing_bus_ids = {1}
            bus_config_widgets = {1: ParseResultClassificationTests.Widget()}
            progress_dialogs = {1: dialog}
            can_bus_data = {1: {"name": "Bus 1", "id_count": 1}}
            db_per_bus = {1: {}}
            parse_workers = {1: object()}

            def register_can_signals(self, pool, bus_name, bus_id):
                self.registered = (pool, bus_name, bus_id)

            def refresh_signal_list_ui(self):
                self.refreshed = True

            def close_progress_dialog(self, _bus_id):
                pass

        target = Target()
        stats = {
            "pool": {"speed": {"t": [0.0], "v": [1.0]}},
            "dbc_signal_count": 1,
            "dbc_matched_frames": 1,
            "msg_ids_with_signals": 1,
        }
        baseline.MDFPlotter.on_parse_finished(target, 1, stats)
        self.assertEqual(dialog.progress, [(100, "解析完成!")])
        self.assertTrue(dialog.finished[0])
        self.assertEqual(target.can_bus_data[1]["parse_stats"]["result_state"], "success")


if __name__ == "__main__":
    unittest.main()
