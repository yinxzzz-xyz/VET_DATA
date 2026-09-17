from pathlib import Path
from threading import Event

import pytest

from vet_data_modular.blf_slice_models import DuplicateFileStatus
from vet_data_modular.blf_slice_service import detect_duplicate_blf_files


def _records_by_name(result):
    return {record.path.name: record for record in result.files}


def test_unique_sizes_skip_all_content_reads(tmp_path: Path) -> None:
    first = tmp_path / "a.blf"
    second = tmp_path / "b.blf"
    first.write_bytes(b"a")
    second.write_bytes(b"bb")

    def forbidden_open(*args, **kwargs):
        raise AssertionError("unique-size files must not be opened")

    result = detect_duplicate_blf_files(
        [second, first],
        open_file=forbidden_open,
    )

    assert result.groups == ()
    assert [item.path for item in result.files] == [first, second]
    assert all(
        item.status is DuplicateFileStatus.SIZE_UNIQUE
        for item in result.files
    )


def test_same_size_different_quick_digest_skips_full_hash(tmp_path: Path) -> None:
    first = tmp_path / "a.blf"
    second = tmp_path / "b.blf"
    first.write_bytes(b"abcde")
    second.write_bytes(b"xbcdy")

    result = detect_duplicate_blf_files(
        [first, second],
        quick_chunk_size=2,
        hash_chunk_size=1,
    )

    assert result.groups == ()
    assert all(
        item.status is DuplicateFileStatus.QUICK_DIGEST_UNIQUE
        for item in result.files
    )
    assert all(item.quick_digest for item in result.files)
    assert all(item.sha256 is None for item in result.files)


def test_equal_quick_digest_but_different_full_hash_is_not_duplicate(
    tmp_path: Path,
) -> None:
    first = tmp_path / "a.blf"
    second = tmp_path / "b.blf"
    first.write_bytes(b"aaXzz")
    second.write_bytes(b"aaYzz")

    result = detect_duplicate_blf_files(
        [first, second],
        quick_chunk_size=2,
        hash_chunk_size=2,
    )

    assert result.groups == ()
    assert result.files[0].quick_digest == result.files[1].quick_digest
    assert result.files[0].sha256 != result.files[1].sha256
    assert all(
        item.status is DuplicateFileStatus.FULL_DIGEST_UNIQUE
        for item in result.files
    )


def test_only_identical_size_and_full_sha256_form_duplicate_group(
    tmp_path: Path,
) -> None:
    first = tmp_path / "a.blf"
    second = tmp_path / "b.blf"
    different = tmp_path / "c.blf"
    first.write_bytes(b"aa-middle-zz")
    second.write_bytes(first.read_bytes())
    different.write_bytes(b"aa-diffxx-zz")

    result = detect_duplicate_blf_files(
        [different, second, first],
        quick_chunk_size=2,
        hash_chunk_size=3,
    )
    records = _records_by_name(result)

    assert len(result.groups) == 1
    assert result.groups[0].paths == (first, second)
    assert result.groups[0].size == first.stat().st_size
    assert result.groups[0].sha256 == records["a.blf"].sha256
    assert records["a.blf"].status is DuplicateFileStatus.DUPLICATE
    assert records["b.blf"].status is DuplicateFileStatus.DUPLICATE
    assert records["c.blf"].status is DuplicateFileStatus.FULL_DIGEST_UNIQUE


def test_quick_digest_read_error_is_explicit_and_never_duplicate(
    tmp_path: Path,
) -> None:
    failed = tmp_path / "bad.blf"
    good = tmp_path / "good.blf"
    failed.write_bytes(b"same")
    good.write_bytes(b"same")

    def selective_open(path, mode):
        if Path(path) == failed:
            raise OSError("denied")
        return open(path, mode)

    result = detect_duplicate_blf_files(
        [failed, good],
        open_file=selective_open,
    )
    records = _records_by_name(result)

    assert result.groups == ()
    assert records["bad.blf"].status is DuplicateFileStatus.FAILED
    assert "quick_digest_error: OSError: denied" == records["bad.blf"].error
    assert records["good.blf"].status is DuplicateFileStatus.QUICK_DIGEST_UNIQUE
    assert result.errors == (records["bad.blf"],)


def test_full_hash_read_error_is_explicit_and_never_duplicate(
    tmp_path: Path,
) -> None:
    failed = tmp_path / "bad.blf"
    good = tmp_path / "good.blf"
    failed.write_bytes(b"identical")
    good.write_bytes(b"identical")
    open_counts = {}

    def fail_second_open(path, mode):
        path = Path(path)
        open_counts[path] = open_counts.get(path, 0) + 1
        if path == failed and open_counts[path] == 2:
            raise OSError("full hash denied")
        return open(path, mode)

    result = detect_duplicate_blf_files(
        [failed, good],
        open_file=fail_second_open,
        hash_chunk_size=2,
    )
    records = _records_by_name(result)

    assert result.groups == ()
    assert records["bad.blf"].status is DuplicateFileStatus.FAILED
    assert "sha256_error: OSError: full hash denied" == records["bad.blf"].error
    assert records["good.blf"].status is DuplicateFileStatus.FULL_DIGEST_UNIQUE


def test_cancel_during_size_stage_stops_before_content_reads(
    tmp_path: Path,
) -> None:
    first = tmp_path / "a.blf"
    second = tmp_path / "b.blf"
    first.write_bytes(b"a")
    second.write_bytes(b"bb")
    cancel = Event()

    def on_progress(item):
        if item.stage == "size":
            cancel.set()

    def forbidden_open(*args, **kwargs):
        raise AssertionError("cancelled size phase must not open file content")

    result = detect_duplicate_blf_files(
        [first, second],
        cancel_event=cancel,
        progress_callback=on_progress,
        open_file=forbidden_open,
    )

    assert result.cancelled
    assert result.groups == ()
    assert all(
        item.status is DuplicateFileStatus.CANCELLED
        for item in result.files
    )


def test_cancel_during_quick_digest_marks_unfinished_files_cancelled(
    tmp_path: Path,
) -> None:
    first = tmp_path / "a.blf"
    second = tmp_path / "b.blf"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    cancel = Event()
    progress = []

    def on_progress(item):
        progress.append(item)
        if item.stage == "quick_digest":
            cancel.set()

    result = detect_duplicate_blf_files(
        [first, second],
        cancel_event=cancel,
        progress_callback=on_progress,
    )

    assert result.cancelled
    assert result.groups == ()
    assert all(
        item.status is DuplicateFileStatus.CANCELLED
        for item in result.files
    )
    assert any(item.stage == "quick_digest" for item in progress)


def test_cancel_during_full_hash_preserves_no_partial_duplicate_group(
    tmp_path: Path,
) -> None:
    first = tmp_path / "a.blf"
    second = tmp_path / "b.blf"
    first.write_bytes(b"identical-content")
    second.write_bytes(b"identical-content")
    cancel = Event()

    def on_progress(item):
        if item.stage == "sha256":
            cancel.set()

    result = detect_duplicate_blf_files(
        [first, second],
        cancel_event=cancel,
        progress_callback=on_progress,
        quick_chunk_size=2,
        hash_chunk_size=2,
    )

    assert result.cancelled
    assert result.groups == ()
    assert all(
        item.status is DuplicateFileStatus.CANCELLED
        for item in result.files
    )


def test_result_and_groups_are_stable_for_reversed_input(tmp_path: Path) -> None:
    a = tmp_path / "a.blf"
    b = tmp_path / "b.blf"
    c = tmp_path / "c.blf"
    d = tmp_path / "d.blf"
    a.write_bytes(b"one")
    b.write_bytes(b"one")
    c.write_bytes(b"second")
    d.write_bytes(b"second")

    forward = detect_duplicate_blf_files([a, b, c, d], quick_chunk_size=1)
    reverse = detect_duplicate_blf_files([d, c, b, a], quick_chunk_size=1)

    assert forward == reverse
    assert [group.paths for group in forward.groups] == [(a, b), (c, d)]


def test_cancel_preserves_already_completed_duplicate_groups(
    tmp_path: Path,
) -> None:
    a = tmp_path / "a.blf"
    b = tmp_path / "b.blf"
    c = tmp_path / "c.blf"
    d = tmp_path / "d.blf"
    a.write_bytes(b"one")
    b.write_bytes(b"one")
    c.write_bytes(b"four")
    d.write_bytes(b"four")
    cancel = Event()

    def on_progress(item):
        if item.stage == "sha256" and item.path == c:
            cancel.set()

    result = detect_duplicate_blf_files(
        [d, c, b, a],
        cancel_event=cancel,
        progress_callback=on_progress,
        quick_chunk_size=1,
        hash_chunk_size=1,
    )

    assert result.cancelled
    assert [group.paths for group in result.groups] == [(a, b)]
    records = _records_by_name(result)
    assert records["a.blf"].status is DuplicateFileStatus.DUPLICATE
    assert records["b.blf"].status is DuplicateFileStatus.DUPLICATE
    assert records["c.blf"].status is DuplicateFileStatus.CANCELLED
    assert records["d.blf"].status is DuplicateFileStatus.CANCELLED


def test_duplicate_normalized_input_path_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "same.blf"
    path.write_bytes(b"x")

    with pytest.raises(ValueError, match="duplicate BLF path"):
        detect_duplicate_blf_files([path, path])
