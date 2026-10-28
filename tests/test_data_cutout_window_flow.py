"""Main-window cut-out flow: option routing, outputs and error handling."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd
import asammdf
import can
from asammdf import Signal
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from vet_data_modular import baseline
from vet_data_modular.data_cutout import (
    CutoutOption,
    OutputFormat,
    OutputScope,
    SourceKind,
)
from vet_data_modular.window import MDFPlotter


APP = QApplication.instance() or QApplication([])


class FakeSettings:
    def __init__(self):
        self.values = {}

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        pass


class FakeDialog:
    option = None
    path = None
    accepted = True
    contexts = []

    def __init__(self, context, **_kwargs):
        FakeDialog.contexts.append(context)

    def exec(self):
        return (
            QDialog.DialogCode.Accepted if FakeDialog.accepted
            else QDialog.DialogCode.Rejected
        )

    def selected_option(self):
        return FakeDialog.option

    def output_path(self):
        return FakeDialog.path


def handle(*_args, **_kwargs):
    return QMessageBox.StandardButton.Yes


class ViewBox:
    def __init__(self, start, stop):
        self._range = [[start, stop], [0.0, 1.0]]

    def viewRange(self):
        return self._range


def make_window(start=1.0, stop=3.0):
    window = MDFPlotter()
    window._gui_settings = FakeSettings()
    window.master_viewbox = ViewBox(start, stop)
    return window


def wait_for_cutout(window, timeout=20.0):
    deadline = time.monotonic() + timeout
    while getattr(window, "_cutout_worker", None) is not None and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(0.005)
    APP.processEvents()
    return getattr(window, "_cutout_worker", None) is None


def make_mdf(path, channels):
    mdf = asammdf.MDF(version="4.10")
    try:
        for name, timestamps, samples in channels:
            mdf.append(Signal(
                samples=np.asarray(samples, dtype=float),
                timestamps=np.asarray(timestamps, dtype=float),
                name=name,
                unit="",
            ))
        mdf.save(str(path), overwrite=True)
    finally:
        mdf.close()


def choice(fmt, scope):
    return CutoutOption(fmt, scope, "", "")


class CutoutWindowFlowTests(unittest.TestCase):
    def setUp(self):
        FakeDialog.option = None
        FakeDialog.path = None
        FakeDialog.accepted = True
        FakeDialog.contexts.clear()

    def run_flow(self, window, option, path, *, messages=None):
        FakeDialog.option = option
        FakeDialog.path = path
        captured = messages if messages is not None else {}
        with patch.object(baseline, "CutoutDialog", FakeDialog), \
             patch.object(QMessageBox, "information", side_effect=lambda *a, **k: captured.setdefault("info", a[2])), \
             patch.object(QMessageBox, "critical", side_effect=lambda *a, **k: captured.setdefault("critical", a[2])), \
             patch.object(QMessageBox, "warning", side_effect=lambda *a, **k: captured.setdefault("warning", a[2])):
            window.save_data_cutout()
            wait_for_cutout(window)
        return captured

    def test_csv_all_signals_flow(self):
        window = make_window()
        try:
            window.mdf_path = "D:/data/source.csv"
            window.mdf_file = pd.DataFrame(
                {"a": [1.0, 2.0, 3.0, 4.0], "b": [5.0, 6.0, 7.0, 8.0]},
                index=[0.0, 1.0, 2.0, 3.0],
            )
            window.mdf_file.index.name = "timestamp"
            window.signals = {
                "a": {"name": "a", "display_name": "a", "group": -1, "channel": -1},
                "b": {"name": "b", "display_name": "b", "group": -1, "channel": -1},
            }
            window.get_selected_signals = lambda: ["a"]
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder, "cut.csv")
                captured = self.run_flow(
                    window, choice(OutputFormat.CSV, OutputScope.ALL), output,
                )
                frame = pd.read_csv(output)
            self.assertEqual(list(frame.columns), ["timestamp", "a", "b"])
            self.assertEqual(len(frame), 3)
            self.assertIn("3 行", captured["info"])
        finally:
            window.deleteLater()
            APP.processEvents()

    def test_csv_selected_includes_calculated_channel(self):
        window = make_window()
        try:
            window.mdf_path = "D:/data/source.csv"
            window.mdf_file = pd.DataFrame(
                {"a": [1.0, 2.0, 3.0, 4.0]}, index=[0.0, 1.0, 2.0, 3.0],
            )
            window.mdf_file.index.name = "timestamp"
            window.signals = {
                "a": {"name": "a", "display_name": "a", "group": -1, "channel": -1},
                "MATH_x": {"name": "MATH_x", "display_name": "🧮 calc", "group": -99, "channel": -99},
            }
            window.custom_math_data["MATH_x"] = {
                "timestamps": np.array([0.0, 1.0, 2.0, 3.0]),
                "samples": np.array([0.0, 10.0, 20.0, 30.0]),
                "unit": "",
            }
            window.get_selected_signals = lambda: ["a", "MATH_x"]
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder, "cut.csv")
                captured = self.run_flow(
                    window, choice(OutputFormat.CSV, OutputScope.SELECTED), output,
                )
                frame = pd.read_csv(output)
            self.assertEqual(list(frame.columns), ["timestamp", "a", "🧮 calc"])
            np.testing.assert_allclose(frame["🧮 calc"], [10.0, 20.0, 30.0])
        finally:
            window.deleteLater()
            APP.processEvents()

    def test_mdf_all_channels_flow(self):
        window = make_window()
        try:
            with tempfile.TemporaryDirectory() as folder:
                source = Path(folder, "source.mf4")
                output = Path(folder, "cut.mf4")
                make_mdf(source, (
                    ("one", [0.0, 1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0]),
                    ("two", [0.0, 1.0, 2.0, 3.0], [5.0, 6.0, 7.0, 8.0]),
                ))
                window.mdf_path = str(source)
                window.signals = {
                    "one_G0_C1": {"name": "one", "display_name": "one", "group": 0, "channel": 1},
                    "two_G1_C1": {"name": "two", "display_name": "two", "group": 1, "channel": 1},
                }
                window.get_selected_signals = lambda: []
                captured = self.run_flow(
                    window, choice(OutputFormat.MDF, OutputScope.ALL), output,
                )
                mdf = asammdf.MDF(output)
                try:
                    names = sorted(group.channels[1].name for group in mdf.groups)
                finally:
                    mdf.close()
            self.assertEqual(names, ["one", "two"])
            self.assertIn("2", captured["info"])
        finally:
            window.deleteLater()
            APP.processEvents()

    def test_mdf_selected_channels_and_calculated_note(self):
        window = make_window()
        try:
            with tempfile.TemporaryDirectory() as folder:
                source = Path(folder, "source.mf4")
                output = Path(folder, "cut.mf4")
                make_mdf(source, (
                    ("one", [0.0, 1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0]),
                    ("two", [0.0, 1.0, 2.0, 3.0], [5.0, 6.0, 7.0, 8.0]),
                ))
                window.mdf_path = str(source)
                window.signals = {
                    "one_G0_C1": {"name": "one", "display_name": "one", "group": 0, "channel": 1},
                    "two_G1_C1": {"name": "two", "display_name": "two", "group": 1, "channel": 1},
                    "MATH_calc": {"name": "MATH_calc", "display_name": "🧮 calc", "group": -99, "channel": -99},
                }
                window.custom_math_data["MATH_calc"] = {
                    "timestamps": np.array([0.0, 1.0]), "samples": np.array([1.0, 2.0]), "unit": "",
                }
                window.get_selected_signals = lambda: ["two_G1_C1", "MATH_calc"]
                captured = self.run_flow(
                    window, choice(OutputFormat.MDF, OutputScope.SELECTED), output,
                )
                mdf = asammdf.MDF(output)
                try:
                    names = [group.channels[1].name for group in mdf.groups]
                finally:
                    mdf.close()
            self.assertEqual(names, ["two"])
            self.assertIn("公式计算通道", captured["info"])
        finally:
            window.deleteLater()
            APP.processEvents()

    def test_blf_raw_flow_keeps_both_buses(self):
        base = 1_700_000_000.0
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, "source.blf")
            writer = can.BLFWriter(str(source))
            try:
                for index in range(5):
                    writer.on_message_received(can.Message(
                        timestamp=base + index * 0.5,
                        arbitration_id=0x100 + index % 2,
                        data=bytes([index]),
                        channel=1 + index % 2,
                        is_extended_id=False,
                    ))
            finally:
                writer.stop()

            window = make_window(start=0.0, stop=1.0)
            try:
                window.mdf_path = str(source)
                window.can_bus_data = {1: {"raw_channel": 1}, 2: {"raw_channel": 2}}
                output = Path(folder, "cut.blf")
                captured = self.run_flow(
                    window, choice(OutputFormat.BLF, OutputScope.ALL), output,
                )
                reader = can.BLFReader(str(output))
                try:
                    written = list(reader)
                finally:
                    reader.stop()
                self.assertEqual(len(written), 3)
                self.assertEqual({message.channel for message in written}, {1, 2})
                self.assertIn("3 条原始报文", captured["info"])
            finally:
                window.deleteLater()
                APP.processEvents()

    def test_empty_window_reports_error_and_leaves_no_partial_file(self):
        window = make_window(start=50.0, stop=60.0)
        try:
            window.mdf_path = "D:/data/source.csv"
            window.mdf_file = pd.DataFrame({"a": [1.0, 2.0]}, index=[0.0, 1.0])
            window.mdf_file.index.name = "timestamp"
            window.signals = {"a": {"name": "a", "display_name": "a", "group": -1, "channel": -1}}
            window.get_selected_signals = lambda: ["a"]
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder, "cut.csv")
                captured = self.run_flow(
                    window, choice(OutputFormat.CSV, OutputScope.ALL), output,
                )
                self.assertFalse(output.exists())
                self.assertEqual(list(Path(folder).glob("*.part*")), [])
            self.assertIn("没有可导出的数据行", captured["critical"])
        finally:
            window.deleteLater()
            APP.processEvents()

    def test_rejected_dialog_does_nothing(self):
        window = make_window()
        try:
            window.mdf_path = "D:/data/source.csv"
            window.mdf_file = pd.DataFrame({"a": [1.0]}, index=[0.0])
            window.signals = {"a": {"name": "a", "display_name": "a", "group": -1, "channel": -1}}
            window.get_selected_signals = lambda: ["a"]
            FakeDialog.accepted = False
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder, "cut.csv")
                self.run_flow(window, choice(OutputFormat.CSV, OutputScope.ALL), output)
                self.assertFalse(output.exists())
        finally:
            window.deleteLater()
            APP.processEvents()

    def test_no_plot_warns_without_dialog(self):
        window = MDFPlotter()
        try:
            window.master_viewbox = None
            with patch.object(baseline, "CutoutDialog") as dialog, \
                 patch.object(QMessageBox, "warning") as warning:
                window.save_data_cutout()
            dialog.assert_not_called()
            warning.assert_called_once()
        finally:
            window.deleteLater()
            APP.processEvents()

    def test_context_counts_selected_available_signals(self):
        window = make_window()
        try:
            window.can_parsed_data = {
                "CAN_Bus 1_a": {"timestamps": np.array([0.0]), "samples": np.array([1.0]), "unit": ""},
                "CAN_Bus 1_b": {"timestamps": np.array([0.0]), "samples": np.array([1.0]), "unit": ""},
                "CAN_Bus 1_empty": {"timestamps": np.array([]), "samples": np.array([]), "unit": ""},
            }
            window.custom_math_data["MATH_x"] = {
                "timestamps": np.array([0.0]), "samples": np.array([1.0]), "unit": "",
            }
            window.get_selected_signals = lambda: ["CAN_Bus 1_a", "MATH_x", "CAN_Bus 1_empty"]
            context = window._build_cutout_context(
                SourceKind.BLF, baseline.CutoutWindow(0.0, 1.0),
            )
        finally:
            window.deleteLater()
            APP.processEvents()
        self.assertEqual(context.selected_total, 3)
        self.assertEqual(context.selected_available, 1)
        self.assertEqual(context.parsed_signal_count, 2)


if __name__ == "__main__":
    unittest.main()
