import os
from pathlib import Path
import tempfile
import unittest
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from vet_data_modular import baseline
from vet_data_modular.window import MDFPlotter
from vet_data_modular.workers import (
    CanBusCountSnapshot,
    CanBusData,
    CanBusMapping,
    CanDetectionSnapshot,
    capture_can_file_identity,
    detect_can_bus_info,
)


APP = QApplication.instance() or QApplication([])


def snapshot_for(path, *, mapping=None, count=2, original=1, bus_id=1):
    mapping = mapping or CanBusMapping.from_raw_channels((original,))
    return CanDetectionSnapshot(
        capture_can_file_identity(path),
        mapping,
        MappingProxyType({
            bus_id: CanBusCountSnapshot(bus_id, original, count),
        }),
    )


def target_for(path, snapshot, *, mutable_count=None, mutable_original=None):
    bus = snapshot.buses[1]
    return SimpleNamespace(
        mdf_path=str(path),
        can_detection_snapshot=snapshot,
        can_bus_data={
            1: {
                "original_bus_id": bus.original_bus_id if mutable_original is None else mutable_original,
                "msg_count": bus.msg_count if mutable_count is None else mutable_count,
            }
        },
        _can_bus_detection_worker=None,
    )


def validate(target, bus_id=1):
    return baseline.MDFPlotter._validated_can_detection_bus(target, bus_id)


class DetectionSnapshotValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "input.blf"
        self.path.write_bytes(b"stable-can-log")

    def tearDown(self):
        self.temp.cleanup()

    def test_unchanged_file_allows_snapshot_count(self):
        snapshot = snapshot_for(self.path, count=7)
        bus = validate(target_for(self.path, snapshot))
        self.assertEqual((bus.bus_id, bus.original_bus_id, bus.msg_count), (1, 1, 7))

    def test_appended_file_is_rejected(self):
        snapshot = snapshot_for(self.path)
        with self.path.open("ab") as stream:
            stream.write(b"append")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            validate(target_for(self.path, snapshot))

    def test_truncated_file_is_rejected(self):
        snapshot = snapshot_for(self.path)
        self.path.write_bytes(b"x")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            validate(target_for(self.path, snapshot))

    def test_replaced_same_path_is_rejected_by_mtime(self):
        snapshot = snapshot_for(self.path)
        original_size = self.path.stat().st_size
        self.path.write_bytes(b"r" * original_size)
        changed_ns = snapshot.file_identity.mtime_ns + 10_000_000
        os.utime(self.path, ns=(changed_ns, changed_ns))
        with self.assertRaisesRegex(ValueError, "发生变化"):
            validate(target_for(self.path, snapshot))

    def test_different_file_path_is_rejected(self):
        snapshot = snapshot_for(self.path)
        other = Path(self.temp.name) / "other.blf"
        other.write_bytes(self.path.read_bytes())
        with self.assertRaisesRegex(ValueError, "不是同一文件"):
            validate(target_for(other, snapshot))

    def test_parse_is_blocked_while_detection_is_running(self):
        snapshot = snapshot_for(self.path)
        target = target_for(self.path, snapshot)
        target._can_bus_detection_worker = SimpleNamespace(isRunning=lambda: True)
        with self.assertRaisesRegex(ValueError, "仍在进行"):
            validate(target)

    def test_missing_cancelled_or_failed_snapshot_is_rejected(self):
        target = SimpleNamespace(
            mdf_path=str(self.path),
            can_detection_snapshot=None,
            can_bus_data={},
            _can_bus_detection_worker=None,
        )
        with self.assertRaisesRegex(ValueError, "缺少完整成功"):
            validate(target)

    def test_file_changed_during_detection_produces_no_snapshot(self):
        source = self.path
        can_message = SimpleNamespace(
            channel=1,
            timestamp=1.0,
            arbitration_id=0x100,
            data=b"\x01",
            is_fd=False,
        )

        class MutatingReader:
            def __iter__(self):
                yield can_message
                with source.open("ab") as stream:
                    stream.write(b"changed")

            def stop(self):
                pass

        with patch(
            "vet_data_modular.workers.can.BLFReader",
            return_value=MutatingReader(),
        ), self.assertRaisesRegex(RuntimeError, "探测期间发生变化"):
            detect_can_bus_info(source)

    def test_invalid_mutable_counts_are_all_rejected(self):
        snapshot = snapshot_for(self.path, count=2)
        for value in (None, True, -1, 0, 1.5, "2"):
            with self.subTest(value=value):
                target = target_for(self.path, snapshot, mutable_count=value)
                if value is None:
                    target.can_bus_data[1].pop("msg_count")
                with self.assertRaisesRegex(ValueError, "msg_count"):
                    validate(target)

    def test_invalid_snapshot_counts_are_all_rejected(self):
        mapping = CanBusMapping.from_raw_channels((1,))
        for value in (True, -1, 0, 1.5, "2"):
            with self.subTest(value=value):
                snapshot = CanDetectionSnapshot(
                    capture_can_file_identity(self.path),
                    mapping,
                    MappingProxyType({1: CanBusCountSnapshot(1, 1, value)}),
                )
                with self.assertRaisesRegex(ValueError, "msg_count"):
                    validate(target_for(self.path, snapshot))

    def test_missing_bus_and_mapping_mismatch_are_rejected(self):
        snapshot = snapshot_for(self.path)
        with self.assertRaisesRegex(ValueError, "不存在 Bus 2"):
            validate(target_for(self.path, snapshot), 2)

        inconsistent = CanBusMapping(
            MappingProxyType({1: 2}),
            MappingProxyType({2: 1}),
            0,
        )
        bad_snapshot = snapshot_for(
            self.path, mapping=inconsistent, count=2, original=1, bus_id=1
        )
        with self.assertRaisesRegex(ValueError, "映射不一致"):
            validate(target_for(self.path, bad_snapshot))


class DetectionLifecycleTests(unittest.TestCase):
    def test_synchronous_detection_failure_invalidates_old_snapshot(self):
        target = SimpleNamespace(
            can_bus_data={1: {"msg_count": 9}},
            can_bus_mapping=object(),
            can_detection_snapshot=object(),
        )
        with patch.object(baseline, "detect_can_bus_info", side_effect=OSError("failed")):
            with self.assertRaisesRegex(Exception, "读取CAN总线信息失败"):
                baseline.MDFPlotter.load_can_bus_info(target, "input.blf")
        self.assertEqual(target.can_bus_data, {})
        self.assertIsNone(target.can_bus_mapping)
        self.assertIsNone(target.can_detection_snapshot)

    def test_stale_detection_result_cannot_replace_current_state(self):
        window = MDFPlotter()
        old_worker = object()
        current_worker = object()
        window._can_detection_generation = 2
        window._can_bus_detection_worker = current_worker
        window.can_bus_data[99] = {"msg_count": 1}
        mapping = CanBusMapping.from_raw_channels((1,))
        identity = SimpleNamespace(path="unused", size=1, mtime_ns=1)
        snapshot = CanDetectionSnapshot(
            identity,
            mapping,
            MappingProxyType({1: CanBusCountSnapshot(1, 1, 1)}),
        )
        result = CanBusData(snapshot)
        result[1] = {"msg_count": 1, "original_bus_id": 1}
        try:
            window._finish_can_bus_detection(result, 1, old_worker)
            self.assertEqual(set(window.can_bus_data), {99})
            self.assertIsNone(window.can_detection_snapshot)
        finally:
            window._can_bus_detection_worker = None
            window.deleteLater()
            APP.processEvents()

    def test_parse_bus_does_not_create_worker_without_trusted_snapshot(self):
        window = MDFPlotter()
        window.bus_protocols[1] = "bus1.dbc"
        window.can_detection_snapshot = None
        try:
            with patch.object(baseline.QMessageBox, "warning") as warning, patch.object(
                baseline, "ParseWorker"
            ) as worker_class:
                window.parse_bus(1)
            worker_class.assert_not_called()
            warning.assert_called_once()
            self.assertNotIn(1, window.parse_workers)
        finally:
            window.deleteLater()
            APP.processEvents()

    def test_cancelled_or_failed_detection_clears_trusted_snapshot(self):
        window = MDFPlotter()
        worker = object()
        window._can_detection_generation = 3
        window._can_bus_detection_worker = worker
        window.can_detection_snapshot = object()
        try:
            window._cancel_can_bus_detection(3, worker)
            self.assertIsNone(window.can_detection_snapshot)
            window.can_detection_snapshot = object()
            with patch("vet_data_modular.window.QMessageBox.critical"):
                window._fail_can_bus_detection("failed", 3, worker)
            self.assertIsNone(window.can_detection_snapshot)
        finally:
            window._can_bus_detection_worker = None
            window.deleteLater()
            APP.processEvents()


if __name__ == "__main__":
    unittest.main()
