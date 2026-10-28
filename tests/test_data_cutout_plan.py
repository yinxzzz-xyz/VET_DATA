"""Pure cut-out planning: source kinds, options, naming and path rules."""

import unittest
from pathlib import Path

import numpy as np

from vet_data_modular.data_cutout import (
    CutoutContext,
    CutoutWindow,
    OutputFormat,
    OutputScope,
    SourceKind,
    build_options,
    clean_signal_name,
    default_option,
    default_output_name,
    option_by_key,
    sanitize_file_name,
    source_kind,
    validate_output_path,
)


class SourceKindTests(unittest.TestCase):
    def test_supported_extensions(self):
        self.assertIs(source_kind("a.blf"), SourceKind.BLF)
        self.assertIs(source_kind("A.BLF"), SourceKind.BLF)
        self.assertIs(source_kind("a.mdf"), SourceKind.MDF)
        self.assertIs(source_kind("a.mf4"), SourceKind.MDF)
        self.assertIs(source_kind("a.csv"), SourceKind.CSV)
        self.assertIs(source_kind("a.vbo"), SourceKind.CSV)
        self.assertIs(source_kind("a.asc"), SourceKind.CAN_LOG)
        self.assertIs(source_kind("a.trc"), SourceKind.CAN_LOG)

    def test_unknown_extension_is_rejected(self):
        with self.assertRaises(ValueError):
            source_kind("a.txt")
        with self.assertRaises(ValueError):
            source_kind("noextension")


class WindowTests(unittest.TestCase):
    def test_describe_and_duration(self):
        window = CutoutWindow(1.0, 3.5)
        self.assertEqual(window.duration, 2.5)
        self.assertEqual(window.describe(), "1.00 s – 3.50 s（共 2.50 s）")

    def test_mask_is_inclusive(self):
        mask = CutoutWindow(1.0, 3.0).mask(np.array([0.0, 1.0, 2.0, 3.0, 4.0]))
        self.assertEqual(mask.tolist(), [False, True, True, True, False])

    def test_equal_bounds_are_allowed(self):
        self.assertEqual(CutoutWindow(2.0, 2.0).duration, 0.0)

    def test_invalid_ranges_are_rejected(self):
        with self.assertRaises(ValueError):
            CutoutWindow(3.0, 1.0)
        with self.assertRaises(ValueError):
            CutoutWindow(float("nan"), 1.0)
        with self.assertRaises(TypeError):
            CutoutWindow("1", 2.0)


class OptionTests(unittest.TestCase):
    def context(self, kind, **kwargs):
        defaults = dict(
            source_path="D:/data/sample.bin",
            kind=kind,
            window=CutoutWindow(0.0, 10.0),
        )
        defaults.update(kwargs)
        return CutoutContext(**defaults)

    def test_blf_offers_raw_messages_and_selected_signals(self):
        options = build_options(self.context(
            SourceKind.BLF, selected_total=3, selected_available=2, parsed_signal_count=40,
        ))
        self.assertEqual(len(options), 2)
        self.assertEqual(options[0].key, "blf:all")
        self.assertTrue(options[0].enabled)
        self.assertEqual(options[0].extension, ".blf")
        self.assertEqual(options[1].key, "mdf:selected")
        self.assertTrue(options[1].enabled)
        self.assertIn("2", options[1].title)
        self.assertEqual(options[1].extension, ".mf4")

    def test_blf_signal_option_disabled_without_selection_or_data(self):
        no_selection = build_options(self.context(SourceKind.BLF))
        self.assertFalse(no_selection[1].enabled)
        self.assertIn("尚未勾选", no_selection[1].disabled_reason)

        no_data = build_options(self.context(
            SourceKind.BLF, selected_total=2, selected_available=0,
        ))
        self.assertFalse(no_data[1].enabled)
        self.assertIn("没有可导出的数据", no_data[1].disabled_reason)

    def test_can_log_offers_all_and_selected_parsed_signals(self):
        options = build_options(self.context(
            SourceKind.CAN_LOG, selected_total=4, selected_available=2, parsed_signal_count=12,
        ))
        self.assertEqual([option.key for option in options], ["mdf:all", "mdf:selected"])
        self.assertTrue(all(option.enabled for option in options))
        self.assertIn("12", options[0].title)

        empty = build_options(self.context(SourceKind.CAN_LOG))
        self.assertFalse(empty[0].enabled)
        self.assertIn("尚未解析", empty[0].disabled_reason)
        self.assertFalse(empty[1].enabled)

    def test_mdf_labels_show_file_signal_count(self):
        options = build_options(self.context(
            SourceKind.MDF, selected_total=2, selected_available=1, file_signal_count=2640,
        ))
        self.assertEqual([option.key for option in options], ["mdf:all", "mdf:selected"])
        self.assertIn("2640", options[0].title)
        self.assertIn("1", options[1].title)

    def test_csv_selected_requires_available_data(self):
        options = build_options(self.context(
            SourceKind.CSV, selected_total=3, selected_available=0, file_signal_count=6,
        ))
        self.assertTrue(options[0].enabled)
        self.assertFalse(options[1].enabled)
        self.assertIn("没有可用数据", options[1].disabled_reason)

    def test_helpers(self):
        options = build_options(self.context(SourceKind.BLF, selected_available=1))
        self.assertIs(default_option(options), options[0])
        self.assertIs(option_by_key(options, "mdf:selected"), options[1])
        self.assertIsNone(option_by_key(options, "nope"))


class NamingTests(unittest.TestCase):
    window = CutoutWindow(12.345, 25.5)

    def test_names_per_format(self):
        self.assertEqual(
            default_output_name("D:/d/run01.blf", self.window, OutputFormat.BLF, OutputScope.ALL),
            "run01_cut_12.35s-25.50s.blf",
        )
        self.assertEqual(
            default_output_name("D:/d/run01.blf", self.window, OutputFormat.MDF, OutputScope.SELECTED),
            "run01_cut_12.35s-25.50s_signals.mf4",
        )
        self.assertEqual(
            default_output_name("D:/d/run01.mf4", self.window, OutputFormat.MDF, OutputScope.ALL),
            "run01_cut_12.35s-25.50s.mf4",
        )
        self.assertEqual(
            default_output_name("D:/d/run01.csv", self.window, OutputFormat.CSV, OutputScope.SELECTED),
            "run01_cut_12.35s-25.50s_selected.csv",
        )

    def test_invalid_characters_are_replaced(self):
        self.assertEqual(sanitize_file_name('a<>:"/\\|?*b'), "a_________b")
        self.assertEqual(sanitize_file_name("name.  "), "name")
        self.assertEqual(sanitize_file_name("   "), "cut")

    def test_signal_names_drop_ui_markers(self):
        self.assertEqual(clean_signal_name("🚌 Bus 1 - Speed"), "Bus 1 - Speed")
        self.assertEqual(clean_signal_name("🧮 EnginePower"), "EnginePower")
        self.assertEqual(clean_signal_name("", "fallback"), "fallback")

    def test_output_path_rules(self):
        self.assertTrue(validate_output_path("D:/data/out.mf4", "D:/data/in.mf4").ok)
        same = validate_output_path("D:/data/in.mf4", "D:/data/in.mf4")
        self.assertFalse(same.ok)
        self.assertIn("源文件", same.reason)
        missing = validate_output_path("D:/data/definitely_missing_dir/out.mf4", "D:/data/in.mf4")
        self.assertFalse(missing.ok)
        self.assertIn("不存在", missing.reason)


if __name__ == "__main__":
    unittest.main()
