import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from vet_data_modular.blf_slice_table import TableParseError, parse_condition_table


class CsvConditionTableTests(unittest.TestCase):
    def _write(self, folder, content, encoding="utf-8"):
        path = Path(folder) / "conditions.csv"
        path.write_bytes(content.encode(encoding))
        return path

    def test_utf8_bom_chinese_and_both_time_formats(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(
                folder,
                "\ufeff记录时间,记录内容\n2026-06-29 14:30:15,动力不足\n2026/6/29 9:05,山路工况\n",
            )
            result = parse_condition_table(path)
        self.assertEqual(result.encoding, "utf-8-sig")
        self.assertEqual([item.name for item in result.conditions], ["动力不足", "山路工况"])
        self.assertEqual(result.conditions[0].recorded_at, datetime(2026, 6, 29, 14, 30, 15))
        self.assertEqual(result.conditions[1].recorded_at, datetime(2026, 6, 29, 9, 5, 0))

    def test_gbk_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(folder, "记录时间,记录内容\n2026/6/29 9:05,高速动力不足\n", "gbk")
            result = parse_condition_table(path)
        self.assertEqual(result.encoding, "gbk")
        self.assertEqual(result.conditions[0].name, "高速动力不足")

    def test_quoted_comma_escaped_quote_newline_and_source_lines(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(
                folder,
                '记录时间,记录内容,附加列\n2026-06-29 14:30:15,"动力不足,""复现""\n第二行",忽略\n',
            )
            result = parse_condition_table(path)
        condition = result.conditions[0]
        self.assertEqual(condition.name, '动力不足,"复现"\n第二行')
        self.assertEqual(condition.original_row_number, 2)
        self.assertEqual(result.discarded_rows, ())

    def test_additional_columns_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(
                folder,
                "序号,记录时间,纬度,记录内容,时间来源\n1,2026/6/29 9:05,31.2,测试,设备\n",
            )
            result = parse_condition_table(path)
        self.assertEqual(len(result.conditions), 1)
        self.assertEqual(result.conditions[0].name, "测试")

    def test_missing_required_header_is_file_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(folder, "记录时间,其他\n2026/6/29 9:05,测试\n")
            with self.assertRaisesRegex(TableParseError, "缺少必需表头: 记录内容"):
                parse_condition_table(path)

    def test_blank_and_invalid_times_are_discarded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(
                folder,
                "记录时间,记录内容\n,空时间\n14:30:15,只有时间\n2026-06-29 14:30:15.123,毫秒\n无效,坏时间\n",
            )
            result = parse_condition_table(path)
        self.assertEqual(result.conditions, ())
        self.assertEqual(len(result.discarded_rows), 4)
        self.assertEqual(result.discarded_rows[0].row_number, 2)

    def test_year_boundaries_and_out_of_range_years(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(
                folder,
                "记录时间,记录内容\n2000-01-01 00:00:00,最小\n2100-12-31 23:59:59,最大\n1999-12-31 23:59:59,过小\n2101-01-01 00:00:00,过大\n",
            )
            result = parse_condition_table(path)
        self.assertEqual([item.name for item in result.conditions], ["最小", "最大"])
        self.assertEqual(len(result.discarded_rows), 2)
        self.assertTrue(all("2000～2100" in row.reason for row in result.discarded_rows))

    def test_optional_seconds_and_empty_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(
                folder,
                "记录时间,记录内容,向前秒数,向后秒数\n2026/6/29 9:05,自定义,0,125\n2026/6/29 9:06,默认,,\n",
            )
            result = parse_condition_table(path)
        self.assertEqual((result.conditions[0].before_seconds, result.conditions[0].after_seconds), (0, 125))
        self.assertEqual((result.conditions[1].before_seconds, result.conditions[1].after_seconds), (60, 60))

    def test_missing_optional_columns_use_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(folder, "记录时间,记录内容\n2026/6/29 9:05,默认\n")
            result = parse_condition_table(path)
        self.assertEqual((result.conditions[0].before_seconds, result.conditions[0].after_seconds), (60, 60))

    def test_invalid_row_does_not_block_valid_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(
                folder,
                "记录时间,记录内容,向前秒数\n错误,坏行,60\n2026/6/29 9:05,好行,abc\n2026/6/29 9:06,另一好行,30\n",
            )
            result = parse_condition_table(path)
        self.assertEqual([item.name for item in result.conditions], ["另一好行"])
        self.assertEqual(len(result.discarded_rows), 2)
        self.assertIn("记录时间格式无效", result.discarded_rows[0].reason)
        self.assertIn("必须为整数", result.discarded_rows[1].reason)

    def test_blank_record_is_discarded_with_location(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(folder, "记录时间,记录内容\n\n2026/6/29 9:05,有效\n")
            result = parse_condition_table(path)
        self.assertEqual(result.discarded_rows[0].source_location, "第 2 行")
        self.assertEqual(result.conditions[0].original_row_number, 3)


if __name__ == "__main__":
    unittest.main()
