"""Repeatable offscreen timing harness for VET_DATA plot operations.

Run from the repository root with:
    python tests/benchmark_plot_performance.py

Synthetic signal generation and widget construction are reported separately
from plot construction. GUI event processing is included only in scenarios
whose name says ``*_and_events``. This is diagnostic tooling, not a test.
"""

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "vet_data"))

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from vet_data_modular.window import MDFPlotter


def percentile(values, percent):
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def measure(name, operation, repeats):
    operation()  # one warm-up pass; exclude import/cache initialization
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - started) * 1000)
    return {
        "scenario": name,
        "repeats": repeats,
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(percentile(samples, 0.95), 3),
    }


def run_case(app, signal_count, point_count, mode, repeats):
    window = MDFPlotter()
    window.resize(1400, 900)
    window.show()
    app.processEvents()

    # Generate equal-sized, float64 arrays before timing plot operations.
    timestamps = np.linspace(0.0, 1000.0, point_count, dtype=np.float64)
    signals = {}
    parsed = {}
    for index in range(signal_count):
        key = f"signal_{index:03d}"
        signals[key] = {
            "name": key, "display_name": key, "comment": "synthetic",
            "bus_id": 0,
        }
        parsed[key] = {
            "timestamps": timestamps,
            "samples": np.sin(timestamps * (0.001 + index * 0.00001) + index),
            "unit": "V",
        }
    window.signals = signals
    window.can_parsed_data = parsed
    window.can_bus_data = {0: {"name": "Synthetic"}}
    window.refresh_signal_list_ui()
    for row in range(window.signal_list_widget.count()):
        window.signal_list_widget.item(row).setCheckState(Qt.CheckState.Checked)
    window.plot_mode_combo.setCurrentIndex(window.plot_mode_combo.findData(mode))

    result = {
        "mode": mode,
        "signals": signal_count,
        "points_per_signal": point_count,
        "total_points": signal_count * point_count,
        "numpy": np.__version__,
        "scenarios": [],
    }
    result["scenarios"].append(measure(
        "initial_plot_no_event_flush",
        window.plot_selected_signals,
        repeats,
    ))
    app.processEvents()
    # Rebuild for timing, so setup from the previous measurement does not
    # contaminate cursor/range timings.
    window.plot_selected_signals()
    app.processEvents()
    window.set_cursor_mode("single")
    app.processEvents()
    initial_plot = window.plot_widgets[0]
    initial_manager = getattr(initial_plot, "multi_y_axis_manager", None)
    initial_viewbox_count = (
        len(initial_manager.signal_keys) if initial_manager is not None else 0
    )

    result["scenarios"].append(measure(
        "cursor_render_no_event_flush",
        lambda: window._render_single_cursor_at(503.125),
        repeats,
    ))
    result["scenarios"].append(measure(
        "cursor_render_and_events",
        lambda: (window._render_single_cursor_at(503.125), app.processEvents()),
        repeats,
    ))
    result["scenarios"].append(measure(
        "range_statistics_refresh_no_event_flush",
        lambda: window._update_stats(window.plot_widgets[0]),
        repeats,
    ))
    result["scenarios"].append(measure(
        "debounced_auto_scale_work_no_event_flush",
        window.execute_auto_scale,
        repeats,
    ))
    window._cursor_a_time = 333.333
    window._cursor_b_time = 666.667
    window.set_cursor_mode("dual")
    result["scenarios"].append(measure(
        "dual_cursor_values_and_interval_stats_no_event_flush",
        window._update_measurement_display,
        repeats,
    ))
    window.set_cursor_mode("single")
    window._current_cursor_time = 503.125
    window._mouse_in_plot = True
    result["scenarios"].append(measure(
        "signal_list_value_refresh_no_event_flush",
        window._update_signal_list_values,
        repeats,
    ))

    plot = window.plot_widgets[0]
    low_high = [(100.0, 800.0), (150.0, 850.0)]
    range_index = [0]

    def change_range(*, flush_events):
        low, high = low_high[range_index[0] % len(low_high)]
        range_index[0] += 1
        window.master_viewbox.setXRange(low, high, padding=0)
        if flush_events:
            app.processEvents()

    result["scenarios"].append(measure(
        "x_zoom_range_without_event_flush",
        lambda: change_range(flush_events=False),
        repeats,
    ))
    app.processEvents()

    result["scenarios"].append(measure(
        "x_zoom_range_and_events",
        lambda: change_range(flush_events=True),
        repeats,
    ))

    def switch_current_signal():
        item = window.signal_list_widget.item(range_index[0] % signal_count)
        range_index[0] += 1
        window.signal_list_widget.setCurrentItem(item)
        app.processEvents()

    result["scenarios"].append(measure(
        "current_signal_switch_and_events", switch_current_signal, repeats
    ))

    # Resizing exercises widget geometry, overlay ViewBox synchronization,
    # stats visibility, and shared-axis alignment.
    size_index = [0]

    def resize_window():
        window.resize(*( [(1400, 900), (1200, 760)][size_index[0] % 2] ))
        size_index[0] += 1
        app.processEvents()

    result["scenarios"].append(measure(
        "window_resize_and_events", resize_window, repeats
    ))

    def remove_then_restore_last_signal():
        item = window.signal_list_widget.item(signal_count - 1)
        item.setCheckState(Qt.CheckState.Unchecked)
        window.plot_selected_signals()
        item.setCheckState(Qt.CheckState.Checked)
        window.plot_selected_signals()
        app.processEvents()

    result["scenarios"].append(measure(
        "remove_and_add_signal_replot_and_events",
        remove_then_restore_last_signal,
        repeats,
    ))

    mode_index = [window.plot_mode_combo.currentIndex()]

    def switch_plot_mode():
        choices = [
            window.plot_mode_combo.findData("independent"),
            window.plot_mode_combo.findData("overlay"),
        ]
        mode_index[0] = choices[1] if mode_index[0] == choices[0] else choices[0]
        window.plot_mode_combo.setCurrentIndex(mode_index[0])
        app.processEvents()

    result["scenarios"].append(measure(
        "plot_mode_switch_and_events", switch_plot_mode, repeats
    ))

    result["multi_viewbox_count"] = initial_viewbox_count
    window.hide()
    window.deleteLater()
    app.processEvents()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signals", nargs="+", type=int, default=[10, 30, 50])
    parser.add_argument("--points", nargs="+", type=int, default=[100_000])
    parser.add_argument("--modes", nargs="+", choices=["independent", "overlay"],
                        default=["independent", "overlay"])
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    app = QApplication.instance() or QApplication([])
    for points in args.points:
        for count in args.signals:
            for mode in args.modes:
                try:
                    print(json.dumps(
                        run_case(app, count, points, mode, args.repeats),
                        ensure_ascii=False,
                    ), flush=True)
                except Exception as exc:
                    print(json.dumps({
                        "mode": mode, "signals": count,
                        "points_per_signal": points, "error": repr(exc),
                    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
