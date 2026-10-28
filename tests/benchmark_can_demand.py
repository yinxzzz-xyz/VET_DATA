"""Measure real-file CAN discovery, demand parsing, and raw-cache replay.

Run from the repository root. This reads project BLF/DBC/JSON inputs only.
"""

import contextlib
import argparse
import gc
import io
import json
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vet_data"))

import cantools
import can
from PyQt6.QtWidgets import QApplication

from vet_data_modular import baseline
from vet_data_modular.workers import (
    CanRawMessageCache, capture_can_file_identity, detect_can_bus_info,
)
from vet_data_modular.window import MDFPlotter


def rss_bytes():
    try:
        import psutil
        return psutil.Process().memory_info().rss
    except ImportError:
        return None


def run_worker(bus_id, channel, count, path, identity, db, *, caches=None, replay=None):
    worker = baseline.ParseWorker(bus_id, channel, count, path, identity, db)
    worker.raw_cache_collectors = caches or {}
    worker.raw_message_cache = replay
    finished, errors = [], []
    worker.parse_finished.connect(lambda _bus, stats: finished.append(stats))
    worker.parse_error.connect(lambda _bus, error: errors.append(error))
    with contextlib.redirect_stdout(io.StringIO()):
        worker.run()
    if errors:
        raise RuntimeError(errors[0])
    if not finished:
        raise RuntimeError("Parser did not return a result")
    return finished[0]


def measure_reader_only(path, repeats=1):
    timings = []
    counts = []
    for _ in range(repeats):
        started = time.perf_counter()
        reader = can.BLFReader(str(path))
        count = sum(1 for _message in reader)
        timings.append(time.perf_counter() - started)
        counts.append(count)
    return timings, counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first-repeats", type=int, default=3)
    parser.add_argument("--reader-repeats", type=int, default=1)
    args = parser.parse_args()
    config_path = ROOT / "测试数据" / "blf" / "IW5PM_X1.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    blf_path = ROOT / "测试数据" / "blf" / "dut_02-15-f2-3a-03-ec_2026-10-15_142305.138226.18.blf"
    selected_keys = config["selected_signals"]
    selected_names = [key.rsplit("_", 1)[-1] for key in selected_keys]
    app = QApplication.instance() or QApplication([])

    reader_only_times, reader_only_counts = measure_reader_only(
        blf_path, max(1, args.reader_repeats)
    )

    started = time.perf_counter()
    with contextlib.redirect_stdout(io.StringIO()):
        detected = detect_can_bus_info(str(blf_path))
    detect_seconds = time.perf_counter() - started
    dbc_started = time.perf_counter()
    databases = {}
    protocols = {}
    for saved_bus, bus_info in config["bus_config"].items():
        protocol = bus_info.get("protocol")
        if protocol and str(protocol).lower().endswith(".dbc"):
            bus_id = detected.mapping.raw_to_bus[int(bus_info["raw_channel"])]
            protocols[bus_id] = protocol
            databases[bus_id] = cantools.database.load_file(protocol)
    dbc_load_seconds = time.perf_counter() - dbc_started
    identity = detected.snapshot.file_identity

    window = MDFPlotter()
    window.mdf_path = str(blf_path)
    window.can_bus_mapping = detected.mapping
    window.can_detection_snapshot = detected.snapshot
    window.can_bus_data.update(detected)
    window.bus_protocols.update(protocols)
    catalog_started = time.perf_counter()
    for bus_id, protocol in protocols.items():
        window.on_bus_protocol_changed(bus_id, protocol)
    catalog_seconds = time.perf_counter() - catalog_started
    directory_count_first = len(window.signals)
    identities_first = sum(
        len(value.get("signal_identity", ())) if isinstance(value.get("signal_identity"), list)
        else bool(value.get("signal_identity"))
        for value in window.signals.values()
    )

    mapping_identity = tuple(sorted(detected.mapping.bus_to_raw.items()))
    def make_collectors():
        grouped = {}
        for cache_bus, cache_db in databases.items():
            raw_channel = detected.mapping.bus_to_raw[cache_bus]
            cache = CanRawMessageCache(
                identity, raw_channel,
                {(int(msg.frame_id), bool(msg.is_extended_frame)) for msg in cache_db.messages},
                (identity, mapping_identity, cache_bus, raw_channel, protocols[cache_bus]),
            )
            grouped.setdefault(raw_channel, []).append(cache)
        return {channel: tuple(items) for channel, items in grouped.items()}

    bus_id = detected.mapping.raw_to_bus[0]
    full_db = databases[bus_id]
    selected = {}
    for message in full_db.messages:
        names = {signal.name for signal in message.signals if signal.name in selected_names}
        if names:
            selected[message.frame_id] = names
    filtered_db, _ = baseline.BusConfigWidget._build_filtered_database(full_db, selected)
    count = detected[bus_id]["msg_count"]
    first_times = []
    first_runs = []
    first_rss_before = rss_bytes()
    first_rss_after_one_parse = None
    collectors = None
    for repeat_index in range(max(1, args.first_repeats)):
        if collectors is not None:
            del collectors
            gc.collect()
        collectors = make_collectors()
        first_started = time.perf_counter()
        first_stats = run_worker(
            bus_id, 0, count, str(blf_path), identity, filtered_db, caches=collectors
        )
        if repeat_index == 0:
            first_rss_after_one_parse = rss_bytes()
        first_times.append(time.perf_counter() - first_started)
        first_runs.append({
            key: first_stats.get(key) for key in (
                "full_file_scan_seconds", "target_processing_seconds",
                "cache_collection_seconds", "target_frame_match_seconds",
                "signal_decode_seconds", "result_append_seconds",
                "messages_processed", "signals_decoded", "generated_sample_count",
                "generated_signal_count", "raw_cache_frame_count",
                "raw_cache_memory_bytes", "raw_cache_payload_bytes",
            )
        })
    first_seconds = statistics.median(first_times)
    rss_after_repeats = rss_bytes()
    cache = next(cache for cache in collectors[0] if cache.raw_channel == 0)
    array_started = time.perf_counter()
    window.register_can_signals(first_stats.pop("pool"), "Bus 1", bus_id)
    result_array_seconds = time.perf_counter() - array_started
    for key, saved_signal_identity in config.get("signal_identities", {}).items():
        if key in window.signals:
            signal_identity = dict(saved_signal_identity)
            window.signals[key]["signal_identity"] = signal_identity
            window.can_parsed_data[key]["signal_identity"] = signal_identity

    window.refresh_signal_list_ui()
    window.signal_list_widget.blockSignals(True)
    for key in selected_keys:
        checkbox = window.signal_widgets[key]["checkbox"]
        checkbox.setChecked(True)
    window.signal_list_widget.blockSignals(False)
    plot_started = time.perf_counter()
    window.plot_selected_signals()
    app.processEvents()
    first_plot_seconds = time.perf_counter() - plot_started

    extra_signal = next(
        signal.name for message in full_db.messages for signal in message.signals
        if signal.name not in selected_names
    )
    extra_db, _ = baseline.BusConfigWidget._build_filtered_database(
        full_db,
        {message.frame_id: {signal.name for signal in message.signals if signal.name == extra_signal}
         for message in full_db.messages if any(signal.name == extra_signal for signal in message.signals)},
    )
    incremental_times = []
    incremental_runs = []
    for _ in range(3):
        incremental_started = time.perf_counter()
        incremental_stats = run_worker(
            bus_id, 0, count, str(blf_path), identity, extra_db, replay=cache
        )
        incremental_times.append(time.perf_counter() - incremental_started)
        incremental_runs.append({
            key: incremental_stats.get(key) for key in (
                "cache_hit", "cache_replayed_frames", "full_file_scan_seconds",
                "cache_replay_seconds", "target_frame_match_seconds",
                "signal_decode_seconds", "result_append_seconds",
                "messages_processed", "generated_signal_count",
                "generated_sample_count",
            )
        })
    incremental_seconds = statistics.median(incremental_times)
    incremental_array_started = time.perf_counter()
    window.register_can_signals(incremental_stats.pop("pool"), "Bus 1", bus_id)
    incremental_array_seconds = time.perf_counter() - incremental_array_started
    extra_definition = next(
        (message, signal) for message in full_db.messages for signal in message.signals
        if signal.name == extra_signal
    )
    extra_identity = {
        "bus_id": bus_id, "raw_channel": 0,
        "frame_id": int(extra_definition[0].frame_id),
        "is_extended": bool(extra_definition[0].is_extended_frame),
        "message_name": str(extra_definition[0].name),
        "signal_name": extra_signal,
    }
    extra_key = f"CAN_Bus 1_{extra_signal}"
    if extra_key in window.signals:
        window.signals[extra_key]["signal_identity"] = extra_identity
        window.can_parsed_data[extra_key]["signal_identity"] = extra_identity
        window.refresh_signal_list_ui()
        window.signal_list_widget.blockSignals(True)
        checkbox = window.signal_widgets[extra_key]["checkbox"]
        checkbox.setChecked(True)
        window.signal_list_widget.blockSignals(False)
    incremental_plot_started = time.perf_counter()
    window.plot_selected_signals()
    app.processEvents()
    incremental_plot_seconds = time.perf_counter() - incremental_plot_started

    repeat_started = time.perf_counter()
    missing = window.prepare_selected_can_signals(selected_keys)
    repeat_seconds = time.perf_counter() - repeat_started

    window._load_selected_can_config(selected_keys, config.get("signal_identities", {}))
    window._add_pending_selection_rows()
    window._add_pending_selection_rows()
    directory_count_after = len(window.signals)
    unique_keys_after = len(set(window.signals))

    output = {
        "blf": str(blf_path), "blf_bytes": identity.size,
        "detect_full_scan_seconds": round(detect_seconds, 3),
        "reader_only_seconds": [round(value, 3) for value in reader_only_times],
        "reader_only_message_counts": reader_only_counts,
        "dbc_load_seconds": round(dbc_load_seconds, 3),
        "dbc_catalog_build_seconds": round(catalog_seconds, 3),
        "dbc_message_count": sum(len(db.messages) for db in databases.values()),
        "catalog_rows": directory_count_first,
        "catalog_signal_identities": identities_first,
        "first_six_seconds_median": round(first_seconds, 3),
        "first_six_seconds_runs": [round(value, 3) for value in first_times],
        "first_six": first_runs[-1],
        "first_six_runs": first_runs,
        "result_array_build_seconds": round(result_array_seconds, 3),
        "six_signal_plot_and_events_seconds": round(first_plot_seconds, 3),
        "rss_before_bytes": first_rss_before,
        "rss_after_one_parse_bytes": first_rss_after_one_parse,
        "rss_after_all_cold_repeats_bytes": rss_after_repeats,
        "incremental_signal": extra_signal,
        "incremental_one_seconds_median": round(incremental_seconds, 3),
        "incremental_one_seconds_runs": [round(value, 6) for value in incremental_times],
        "incremental_one": incremental_runs[-1],
        "incremental_one_runs": incremental_runs,
        "incremental_array_build_seconds": round(incremental_array_seconds, 3),
        "seven_signal_plot_and_events_seconds": round(incremental_plot_seconds, 3),
        "repeat_selected_reuses_cache_seconds": round(repeat_seconds, 6),
        "repeat_requested_parse": missing,
        "directory_rows_after_repeat_pending_restore": directory_count_after,
        "directory_unique_keys": unique_keys_after,
        "directory_duplicate_rows": directory_count_after - unique_keys_after,
        "raw_cache_frames": cache.frame_count,
        "raw_cache_memory_bytes": cache.memory_bytes,
        "raw_cache_complete": cache.complete,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    window.deleteLater()
    app.processEvents()


if __name__ == "__main__":
    main()
