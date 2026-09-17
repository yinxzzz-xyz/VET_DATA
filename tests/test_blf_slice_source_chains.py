from pathlib import Path

import pytest

from vet_data_modular.blf_slice_models import (
    BlfIndexEntry,
    BlfTimeRangeStatus,
)
from vet_data_modular.blf_slice_service import build_blf_source_chains


def _entry(
    name: str,
    start: float | None = None,
    stop: float | None = None,
    status: BlfTimeRangeStatus = BlfTimeRangeStatus.CONFIRMED,
    error: str | None = None,
) -> BlfIndexEntry:
    return BlfIndexEntry(
        path=Path(name),
        time_range_status=status,
        effective_start_timestamp=start,
        effective_stop_timestamp=stop,
        error=error,
    )


def test_exactly_five_seconds_joins_one_chain() -> None:
    result = build_blf_source_chains(
        [_entry("a.blf", 0.0, 10.0), _entry("b.blf", 15.0, 20.0)]
    )

    assert [chain.paths for chain in result.chains] == [
        (Path("a.blf"), Path("b.blf"))
    ]
    assert result.chains[0].gaps_seconds == (5.0,)
    assert result.breaks == ()


def test_more_than_five_seconds_starts_new_chain_and_records_break() -> None:
    result = build_blf_source_chains(
        [_entry("a.blf", 0.0, 10.0), _entry("b.blf", 15.001, 20.0)]
    )

    assert [chain.paths for chain in result.chains] == [
        (Path("a.blf"),),
        (Path("b.blf"),),
    ]
    assert len(result.breaks) == 1
    assert result.breaks[0].previous_path == Path("a.blf")
    assert result.breaks[0].next_path == Path("b.blf")
    assert result.breaks[0].gap_seconds == pytest.approx(5.001)


def test_overlap_never_joins_same_chain() -> None:
    result = build_blf_source_chains(
        [_entry("a.blf", 0.0, 10.0), _entry("b.blf", 9.0, 12.0)]
    )

    assert [chain.paths for chain in result.chains] == [
        (Path("a.blf"),),
        (Path("b.blf"),),
    ]
    assert result.breaks == ()


def test_equal_closed_interval_endpoint_is_an_overlap() -> None:
    result = build_blf_source_chains(
        [_entry("a.blf", 0.0, 10.0), _entry("b.blf", 10.0, 12.0)]
    )

    assert [chain.paths for chain in result.chains] == [
        (Path("a.blf"),),
        (Path("b.blf"),),
    ]


def test_multiple_joinable_chains_choose_nearest_tail() -> None:
    result = build_blf_source_chains(
        [
            _entry("a.blf", 0.0, 10.0),
            _entry("b.blf", 5.0, 12.0),
            _entry("next.blf", 14.0, 15.0),
        ]
    )

    assert [chain.paths for chain in result.chains] == [
        (Path("a.blf"),),
        (Path("b.blf"), Path("next.blf")),
    ]
    assert result.chains[1].gaps_seconds == (2.0,)


def test_equal_tail_distance_uses_normalized_chain_first_path() -> None:
    result = build_blf_source_chains(
        [
            _entry("z.blf", 0.0, 10.0),
            _entry("a.blf", 5.0, 10.0),
            _entry("next.blf", 12.0, 13.0),
        ]
    )

    assert [chain.paths for chain in result.chains] == [
        (Path("z.blf"),),
        (Path("a.blf"), Path("next.blf")),
    ]


def test_same_start_is_sorted_by_normalized_full_path() -> None:
    result = build_blf_source_chains(
        [_entry("z.blf", 0.0, 2.0), _entry("a.blf", 0.0, 1.0)]
    )

    assert [chain.paths for chain in result.chains] == [
        (Path("a.blf"),),
        (Path("z.blf"),),
    ]


def test_unresolved_entries_are_retained_and_empty_is_resolved() -> None:
    failed = _entry(
        "failed.blf",
        status=BlfTimeRangeStatus.FAILED,
        error="reader failed",
    )
    cancelled = _entry("cancelled.blf", status=BlfTimeRangeStatus.CANCELLED)
    unconfirmed = _entry("unconfirmed.blf", status=BlfTimeRangeStatus.UNCONFIRMED)
    malformed = _entry("malformed.blf", 3.0, 2.0)
    empty = _entry("empty.blf", status=BlfTimeRangeStatus.EMPTY)

    result = build_blf_source_chains(
        [_entry("ok.blf", 0.0, 1.0), failed, cancelled, unconfirmed, malformed, empty]
    )

    assert result.chains[0].paths == (Path("ok.blf"),)
    assert result.unresolved_entries == (failed, cancelled, unconfirmed, malformed)
    assert result.unresolved_entries[0].error == "reader failed"
    assert not result.is_complete


def test_duplicate_normalized_path_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate BLF path"):
        build_blf_source_chains(
            [_entry("same.blf", 0.0, 1.0), _entry("./same.blf", 2.0, 3.0)]
        )


def test_real_2026_08_26_confirmed_ranges_form_one_chain() -> None:
    root = Path("测试数据") / "blf"
    entries = [
        _entry(
            str(root / "dut_02-15-d7-97-99-fd_2026-08-26_021128.109663.3.blf"),
            1787681488.6967878,
            1787682402.6638238,
        ),
        _entry(
            str(root / "dut_02-15-d7-97-99-fd_2026-08-26_022642.087642.4.blf"),
            1787682402.664389,
            1787683320.192183,
        ),
        _entry(
            str(root / "dut_02-15-d7-97-99-fd_2026-08-26_024159.608645.5.blf"),
            1787683320.1922019,
            1787684235.4414358,
        ),
    ]

    assert all(entry.path.is_file() for entry in entries)
    result = build_blf_source_chains(list(reversed(entries)))

    assert len(result.chains) == 1
    assert result.chains[0].paths == tuple(entry.path for entry in entries)
    assert result.chains[0].gaps_seconds == pytest.approx(
        (0.0005650520324707031, 0.000018835067749023438)
    )
    assert result.unresolved_entries == ()
    assert result.breaks == ()
