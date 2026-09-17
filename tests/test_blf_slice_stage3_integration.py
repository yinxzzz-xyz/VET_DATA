from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from vet_data_modular.blf_slice_models import (
    BlfHeaderStatus,
    BlfTimeRangeStatus,
    DuplicateFileStatus,
)
from vet_data_modular.blf_slice_service import (
    build_blf_header_index,
    build_blf_source_chains,
    detect_duplicate_blf_files,
    select_blf_candidates,
)
from vet_data_modular.blf_slice_time import ConditionTimeWindow


BASE = 1_800_000_000.0


class HeaderReader:
    def __init__(self, start: float, stop: float):
        self.start_timestamp = start
        self.stop_timestamp = stop
        self.iterated = False

    def __iter__(self):
        self.iterated = True
        raise AssertionError("Header indexing must not iterate messages")

    def stop(self):
        pass


class ScanReader:
    def __init__(self, timestamps):
        self.timestamps = timestamps

    def __iter__(self):
        for timestamp in self.timestamps:
            yield SimpleNamespace(
                timestamp=timestamp,
                is_remote_frame=False,
                is_error_frame=False,
                is_fd=False,
            )

    def stop(self):
        pass


def test_stage3_pipeline_models_and_interfaces_are_consistent(tmp_path: Path) -> None:
    volume3 = tmp_path / "capture.3.blf"
    volume4 = tmp_path / "capture.4.blf"
    volume5 = tmp_path / "capture.5.blf"
    duplicate3 = tmp_path / "capture.3-copy.blf"
    volume3.write_bytes(b"volume-three")
    duplicate3.write_bytes(volume3.read_bytes())
    volume4.write_bytes(b"volume-four-longer")
    volume5.write_bytes(b"volume-five-even-longer")

    duplicates = detect_duplicate_blf_files(
        [volume5, duplicate3, volume4, volume3],
        quick_chunk_size=2,
        hash_chunk_size=3,
    )
    assert len(duplicates.groups) == 1
    assert duplicates.groups[0].paths == (duplicate3, volume3)
    assert all(
        item.status is DuplicateFileStatus.DUPLICATE
        for item in duplicates.files
        if item.path in (duplicate3, volume3)
    )

    retained = (volume3, volume4, volume5)
    header_readers = []

    def header_factory(path):
        reader = HeaderReader(BASE, BASE + 15)
        header_readers.append(reader)
        return reader

    indexed = build_blf_header_index(
        retained,
        timedelta(0),
        reader_factory=header_factory,
    )
    assert all(entry.header_status is BlfHeaderStatus.VALID for entry in indexed)
    assert all(
        entry.time_range_status is BlfTimeRangeStatus.UNCONFIRMED
        for entry in indexed
    )
    assert not any(reader.iterated for reader in header_readers)

    actual_ranges = {
        "capture.3.blf": (BASE + 10, BASE + 20),
        "capture.4.blf": (BASE + 20.5, BASE + 30),
        "capture.5.blf": (BASE + 30.6, BASE + 40),
    }
    scan_counts = {name: 0 for name in actual_ranges}

    def scan_factory(path):
        scan_counts[path.name] += 1
        return ScanReader(actual_ranges[path.name])

    selected = select_blf_candidates(
        indexed,
        [
            ConditionTimeWindow(BASE + 15, BASE + 15),
            ConditionTimeWindow(BASE + 25, BASE + 25),
            ConditionTimeWindow(BASE + 35, BASE + 35),
        ],
        timedelta(0),
        reader_factory=scan_factory,
    )
    assert scan_counts == {name: 1 for name in actual_ranges}
    assert all(
        entry.time_range_status is BlfTimeRangeStatus.CONFIRMED
        for entry in selected.entries
    )
    assert [match.candidate_paths for match in selected.matches] == [
        (volume3,),
        (volume4,),
        (volume5,),
    ]

    chains = build_blf_source_chains(selected.entries)
    assert chains.is_complete
    assert len(chains.chains) == 1
    assert chains.chains[0].paths == retained
    assert chains.chains[0].gaps_seconds == pytest.approx((0.5, 0.6))
