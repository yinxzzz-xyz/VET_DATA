"""Curve interaction enhancements."""

import math

import numpy as np
from PyQt6.QtCore import QEvent, QPointF, QSignalBlocker, QSize, QTimer, Qt
from PyQt6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QSpinBox, QWidget

from .legacy import baseline
from .signal_panel import INTERVAL_ROLE, KEY_ROLE, MEASUREMENT_ROLE


class PlotPanelMixin:
    _STATS_HIDE_WIDTH = 300
    _STATS_SHOW_WIDTH = 340

    def _setup_plot_panel(self):
        self.plot_min_height = 150
        self._plot_height_explicit = False
        self.curve_display_mode = self.plot_mode_combo.currentData() or "independent"
        self.cursor_enabled = False
        self._shared_axis_syncing = False
        self._aligning_shared_axis_geometry = False
        self.plot_scroll_area.viewport().installEventFilter(self)
        panel = next((g for g in self.findChildren(QWidget) if getattr(g, "title", lambda: "")() == "信号曲线"), None)
        if panel is None: return
        controls = QWidget(self.curve_ribbon_page)
        row = QHBoxLayout(controls); row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("信号高度:"))
        self.height_spin = QSpinBox()
        self.height_spin.setRange(80, 900); self.height_spin.setSingleStep(10)
        self.height_spin.setValue(self.plot_min_height); self.height_spin.setSuffix(" px")
        row.addWidget(self.height_spin); row.addStretch()
        self.box_zoom_button = QPushButton("框选放大")
        self.box_zoom_button.setCheckable(True)
        self.box_zoom_button.setProperty("uiRole", "stateToggle")
        self.box_zoom_button.setToolTip("拖动框选时间范围；只缩放横轴，纵轴保持当前范围")
        self.box_zoom_button.setMinimumSize(92, 32)
        self.reset_zoom_button = QPushButton("复位视图")
        self.reset_zoom_button.setMinimumSize(92, 32)
        row.addWidget(self.box_zoom_button)
        row.addWidget(self.reset_zoom_button)
        self.curve_ribbon_layout.insertWidget(
            max(0, self.curve_ribbon_layout.count() - 1), controls
        )
        self.height_spin.valueChanged.connect(self._plot_height_changed)
        self.box_zoom_button.toggled.connect(self._set_box_zoom_enabled)
        self.reset_zoom_button.clicked.connect(self.reset_plot_view)
        self.plot_mode_combo.currentIndexChanged.connect(
            lambda _index: self.set_curve_display_mode(self.plot_mode_combo.currentData())
        )
        self.signal_list_widget.currentItemChanged.connect(
            self._current_overlay_signal_changed
        )
        self.selected_signal_list_widget.currentItemChanged.connect(
            self._current_overlay_signal_changed
        )
        self.single_cursor_button.toggled.connect(
            lambda checked: self._cursor_mode_button_toggled("single", checked)
        )
        self.dual_cursor_button.toggled.connect(
            lambda checked: self._cursor_mode_button_toggled("dual", checked)
        )
        self.plot_empty_label = QLabel("选择信号并点击“绘制所选信号”")
        self.plot_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.plot_empty_label.setProperty("uiEmptyState", True)
        self.plot_layout.addWidget(self.plot_empty_label)

    def _current_overlay_signal_changed(self, item, _previous=None):
        if item is None:
            return
        signal_key = item.data(KEY_ROLE)
        for plot in tuple(getattr(self, "plot_widgets", ())):
            manager = getattr(plot, "multi_y_axis_manager", None)
            if manager is None or signal_key not in manager.signal_keys:
                continue
            manager.set_current_signal(signal_key)
            self._overlay_current_signal_key = signal_key
            return

    def _set_plot_empty_state(self, message):
        self.plot_empty_label.setText(message or "")
        self.plot_empty_label.setVisible(bool(message))

    def plot_selected_signals(self):
        snapshot = getattr(self, "can_detection_snapshot", None)
        if snapshot is not None:
            try:
                if baseline.capture_can_file_identity(self.mdf_path) != snapshot.file_identity:
                    self.can_parsed_data.clear()
                    self._plot_after_can_parse = False
                    self._set_plot_empty_state("BLF 文件已变化，请重新导入后再绘图。")
                    return
            except OSError:
                self.can_parsed_data.clear()
                self._plot_after_can_parse = False
                self._set_plot_empty_state("无法读取当前 BLF 文件，请重新导入后再绘图。")
                return
        selected_keys = self.get_selected_signals()
        if snapshot is not None:
            for key in selected_keys:
                data = self.can_parsed_data.get(key)
                if data is None:
                    continue
                bus_id = data.get("bus_id", self.signals.get(key, {}).get("bus_id"))
                protocol = self.bus_protocols.get(bus_id)
                try:
                    protocol_identity = (
                        baseline.capture_can_file_identity(protocol)
                        if protocol else None
                    )
                except OSError:
                    protocol_identity = None
                if (
                    data.get("file_identity") != snapshot.file_identity
                    or data.get("protocol_path") != protocol
                    or data.get("protocol_identity") != protocol_identity
                    or data.get("raw_channel") != self.can_bus_data.get(bus_id, {}).get("raw_channel")
                ):
                    self.can_parsed_data.pop(key, None)
        skip_demand = getattr(self, "_skip_can_demand_once", False)
        self._skip_can_demand_once = False
        selected_can_missing = not skip_demand and any(
            key.startswith("CAN_")
            and self.signals.get(key, {}).get("bus_id") is not None
            and key not in self.can_parsed_data
            for key in selected_keys
        )
        if selected_can_missing:
            self._plot_after_can_parse = True
            try:
                started = self.prepare_selected_can_signals(selected_keys)
            except Exception as exc:
                self._plot_after_can_parse = False
                self._set_plot_empty_state(f"无法启动 CAN 按需解析：{exc}")
                return
            if started:
                self._set_plot_empty_state("正在按需解析所选 CAN 信号…")
                return
            self._plot_after_can_parse = False
        self._clear_cursor_drag_state()
        self._set_plot_empty_state(None)
        super().plot_selected_signals()
        self.shared_time_axis_widget.setVisible(bool(self.plot_widgets))
        if self.master_viewbox is not None:
            shared_viewbox = self.shared_time_axis_widget.getViewBox()
            shared_viewbox.enableAutoRange(x=False)
            previous_master = getattr(self, "_shared_time_axis_master", None)
            if previous_master is not self.master_viewbox:
                if previous_master is not None:
                    try:
                        previous_master.sigXRangeChanged.disconnect(
                            self._shared_time_axis_range_changed
                        )
                    except (RuntimeError, TypeError):
                        pass
                try:
                    shared_viewbox.sigXRangeChanged.disconnect(
                        self._master_x_range_changed_by_shared_axis
                    )
                except (RuntimeError, TypeError):
                    pass
                self.master_viewbox.sigXRangeChanged.connect(
                    self._shared_time_axis_range_changed
                )
                shared_viewbox.sigXRangeChanged.connect(
                    self._master_x_range_changed_by_shared_axis
                )
                self._shared_time_axis_master = self.master_viewbox
            self._set_shared_time_axis_range(self.master_viewbox.viewRange()[0])
            self._align_shared_time_axis_geometry()
            QTimer.singleShot(0, self._align_shared_time_axis_geometry)
        for plot in self.plot_widgets:
            plot.getPlotItem().hideAxis("bottom")
            self._apply_plot_height(plot)
            plot.getViewBox().setMouseEnabled(
                x=True, y=not self.box_zoom_button.isChecked()
            )
            plot.getViewBox().setMouseMode(
                plot.getViewBox().RectMode
                if self.box_zoom_button.isChecked()
                else plot.getViewBox().PanMode
            )
            plot.viewport().installEventFilter(self)
            plot.getViewBox().sigRangeChanged.connect(
                self._schedule_shared_time_axis_alignment
            )
            left_axis = plot.getPlotItem().getAxis("left")
            left_axis.geometryChanged.connect(
                self._schedule_shared_time_axis_alignment
            )
            single_visible = self.cursor_mode == "single"
            plot.vLine.setVisible(single_visible)
            for label in getattr(plot, "cursor_labels", {}).get("single", ()):
                label.setVisible(single_visible)
            plot.text_item.setVisible(single_visible)
            for spot in getattr(plot, "cursor_spots", (plot.spot_item,)):
                spot.setVisible(single_visible)
            self._register_plot_stats_responsiveness(plot)
        self._refresh_plot_scroll_content()
        self._initialize_cursor_positions()
        self._apply_cursor_visibility()
        self._update_measurement_display()
        QTimer.singleShot(50, self._align_shared_time_axis_geometry)
        if self.cursor_mode == "single" and self._single_cursor_time is not None:
            self._render_single_cursor_at(self._single_cursor_time)
            self._current_cursor_time = self._single_cursor_time
            self._mouse_in_plot = True
            if self._view_mode == "selected":
                self._list_update_timer.start()

    def _schedule_shared_time_axis_alignment(self, *_args):
        """Queue alignment through a QObject-bound slot tied to this window."""
        QTimer.singleShot(0, self._align_shared_time_axis_geometry)

    def _shared_time_axis_range_changed(self, _view, ranges):
        self._set_shared_time_axis_range(ranges)

    def _set_shared_time_axis_range(self, target_range):
        if self._shared_axis_syncing or self.master_viewbox is None:
            return
        shared_viewbox = self.shared_time_axis_widget.getViewBox()
        if np.allclose(shared_viewbox.viewRange()[0], target_range, rtol=0, atol=1e-10):
            return
        self._shared_axis_syncing = True
        try:
            shared_viewbox.setXRange(*target_range, padding=0)
        except RuntimeError:
            return
        finally:
            self._shared_axis_syncing = False

    def _master_x_range_changed_by_shared_axis(self, _view, ranges):
        master = self.master_viewbox
        target_range = ranges
        if master is None or self._shared_axis_syncing:
            return
        if np.allclose(master.viewRange()[0], target_range, rtol=0, atol=1e-10):
            return
        self._shared_axis_syncing = True
        try:
            master.setXRange(*target_range, padding=0)
        except RuntimeError:
            return
        finally:
            self._shared_axis_syncing = False

    def _align_shared_time_axis_geometry(self):
        if self._aligning_shared_axis_geometry:
            return
        plots = tuple(getattr(self, "plot_widgets", ()))
        if not plots or self.master_viewbox is None:
            return
        self._aligning_shared_axis_geometry = True
        try:
            margins = self.plot_layout.contentsMargins()
            viewport_width = self.plot_scroll_area.viewport().width()
            target_width = max(1, viewport_width - margins.left() - margins.right())
            self.shared_time_axis_widget.setFixedWidth(target_width)
            left_width = max(
                max(
                    plot.getPlotItem().getAxis("left").width(),
                    plot.getPlotItem().getAxis("left").textWidth,
                )
                for plot in plots
            )
            for plot in plots:
                plot.getPlotItem().layout.setColumnMinimumWidth(0, left_width)

            plot_margins = plots[0].getPlotItem().layout.getContentsMargins()
            self.shared_time_axis_widget.getPlotItem().layout.setContentsMargins(
                *plot_margins
            )
            shared_left_axis = self.shared_time_axis_widget.getPlotItem().getAxis("left")
            self.shared_time_axis_widget.getPlotItem().layout.setColumnMinimumWidth(
                0, left_width
            )
            timestamps = [
                record["signal_data"].timestamps
                for plot in plots
                for record in getattr(plot, "signal_records", ())
                if record.get("signal_data") is not None
                and len(record["signal_data"].timestamps)
            ]
            if timestamps:
                shared_viewbox = self.shared_time_axis_widget.getViewBox()
                shared_viewbox.setLimits(
                    xMin=min(float(values[0]) for values in timestamps),
                    xMax=max(float(values[-1]) for values in timestamps),
                )
        except RuntimeError:
            return
        finally:
            self._aligning_shared_axis_geometry = False

    def set_curve_display_mode(self, mode):
        if mode not in {"independent", "overlay"}:
            return
        if mode == self.curve_display_mode:
            return
        previous_x_range = None
        if self.master_viewbox is not None:
            previous_x_range = tuple(self.master_viewbox.viewRange()[0])
        self.curve_display_mode = mode
        if self.plot_widgets:
            self.plot_selected_signals()
            if previous_x_range is not None:
                for plot in self.plot_widgets:
                    plot.getViewBox().setXRange(
                        previous_x_range[0], previous_x_range[1], padding=0
                    )

    def _cursor_mode_button_toggled(self, mode, checked):
        if checked:
            self.set_cursor_mode(mode)
        elif self.cursor_mode == mode:
            self.set_cursor_mode(None)

    def set_cursor_mode(self, mode):
        if mode not in {None, "single", "dual"}:
            raise ValueError(f"unknown cursor mode: {mode}")
        self._clear_cursor_drag_state()
        self.cursor_mode = mode
        self.cursor_enabled = mode is not None
        single_blocker = QSignalBlocker(self.single_cursor_button)
        dual_blocker = QSignalBlocker(self.dual_cursor_button)
        try:
            self.single_cursor_button.setChecked(mode == "single")
            self.dual_cursor_button.setChecked(mode == "dual")
        finally:
            del single_blocker
            del dual_blocker
        self._update_cursor_button_icons()
        self._single_cursor_time = None
        self._cursor_a_time = None
        self._cursor_b_time = None
        self._current_cursor_time = None
        self._mouse_in_plot = False
        self._clear_signal_values()
        self._initialize_cursor_positions()
        self._apply_cursor_visibility()
        self._update_measurement_display()
        if mode == "single" and self._single_cursor_time is not None:
            self._render_single_cursor_at(self._single_cursor_time)
            self._current_cursor_time = self._single_cursor_time
            self._mouse_in_plot = True
            if self._view_mode == "selected":
                self._list_update_timer.start()

    def _initialize_cursor_positions(self):
        if not self.plot_widgets or self.master_viewbox is None:
            return
        x_min, x_max = self.master_viewbox.viewRange()[0]
        if not np.isfinite(x_min) or not np.isfinite(x_max):
            return
        span = x_max - x_min
        if self.cursor_mode == "single" and self._single_cursor_time is None:
            self._single_cursor_time = float(x_min + span * 0.5)
        elif self.cursor_mode == "dual" and (
            self._cursor_a_time is None or self._cursor_b_time is None
        ):
            self._cursor_a_time = float(x_min + span / 3.0)
            self._cursor_b_time = float(x_min + span * 2.0 / 3.0)

    def _apply_cursor_visibility(self):
        single_visible = self.cursor_mode == "single"
        dual_visible = self.cursor_mode == "dual"
        for plot in tuple(getattr(self, "plot_widgets", ())):
            try:
                records = tuple(getattr(plot, "signal_records", ()))
                has_visible_curve = any(
                    record.get("curve_visible", True) for record in records
                )
                plot.vLine.setVisible(single_visible and has_visible_curve)
                for index, label in enumerate(
                    getattr(plot, "cursor_labels", {}).get("single", ())
                ):
                    visible = (
                        index < len(records)
                        and records[index].get("curve_visible", True)
                    )
                    label.setVisible(single_visible and visible)
                first_curve_visible = bool(
                    records and records[0].get("curve_visible", True)
                )
                plot.text_item.setVisible(single_visible and first_curve_visible)
                for index, spot in enumerate(
                    getattr(plot, "cursor_spots", (plot.spot_item,))
                ):
                    visible = (
                        index < len(records)
                        and records[index].get("curve_visible", True)
                    )
                    spot.setVisible(single_visible and visible)
                if single_visible and self._single_cursor_time is not None:
                    plot.vLine.setPos(self._single_cursor_time)
                for name, line in getattr(plot, "measurement_lines", {}).items():
                    time_value = self._cursor_a_time if name == "A" else self._cursor_b_time
                    line.setVisible(
                        dual_visible and time_value is not None and has_visible_curve
                    )
                    if dual_visible and time_value is not None:
                        line.setPos(time_value)
                    for index, label in enumerate(
                        getattr(plot, "cursor_labels", {}).get(name, ())
                    ):
                        curve_visible = (
                            index < len(records)
                            and records[index].get("curve_visible", True)
                        )
                        label.setVisible(
                            dual_visible and time_value is not None and curve_visible
                        )
            except RuntimeError:
                continue

    def _clear_cursor_drag_state(self):
        self._dragging_cursor = None

    def _clear_cursor_positions_for_data_change(self):
        self._clear_cursor_drag_state()
        self._single_cursor_time = None
        self._cursor_a_time = None
        self._cursor_b_time = None
        self._current_cursor_time = None
        self._mouse_in_plot = False
        self._clear_signal_values()
        self._update_measurement_display()

    def _cursor_line_at_position(self, plot, position):
        view_box = plot.getViewBox()
        scene_position = plot.mapToScene(position)
        if not view_box.sceneBoundingRect().contains(scene_position):
            return None
        y_min, y_max = view_box.viewRange()[1]
        center_y = (y_min + y_max) * 0.5
        if self.cursor_mode == "single":
            candidates = (("single", plot.vLine),)
        elif self.cursor_mode == "dual":
            candidates = tuple(getattr(plot, "measurement_lines", {}).items())
        else:
            return None
        hits = []
        for name, line in candidates:
            if not line.isVisible():
                continue
            scene_line = view_box.mapViewToScene(QPointF(line.value(), center_y))
            viewport_line = plot.mapFromScene(scene_line)
            distance = abs(viewport_line.x() - position.x())
            if distance <= 9:
                hits.append((distance, name))
        return min(hits)[1] if hits else None

    def eventFilter(self, watched, event):
        if watched is self.plot_scroll_area.viewport():
            if event.type() == QEvent.Type.Resize:
                QTimer.singleShot(0, self._align_shared_time_axis_geometry)
            return False
        plot = next((
            candidate for candidate in tuple(getattr(self, "plot_widgets", ()))
            if candidate.viewport() is watched
        ), None)
        if plot is None:
            return False

        event_type = event.type()
        drag = self._dragging_cursor
        if drag is not None:
            if event_type == QEvent.Type.MouseMove:
                owner = drag["plot"]
                try:
                    local = owner.viewport().mapFromGlobal(
                        event.globalPosition().toPoint()
                    )
                    scene_position = owner.mapToScene(local)
                    self._move_active_cursor(owner, drag["name"], scene_position)
                except RuntimeError:
                    self._clear_cursor_drag_state()
                return True
            if event_type == QEvent.Type.MouseButtonRelease:
                if event.button() == Qt.MouseButton.LeftButton:
                    self._clear_cursor_drag_state()
                    return True
                return False

        if (
            event_type == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            name = self._cursor_line_at_position(plot, event.position().toPoint())
            if name is not None:
                self._dragging_cursor = {"plot": plot, "name": name}
                return True
        return False

    def _move_active_cursor(self, plot, name, scene_position):
        if self.cursor_mode == "single" and name == "single":
            self.update_cursor_positions(scene_position, plot)
            return
        if self.cursor_mode != "dual" or name not in {"A", "B"}:
            return
        cursor_time = float(plot.getViewBox().mapSceneToView(scene_position).x())
        x_min, x_max = self.master_viewbox.viewRange()[0]
        cursor_time = min(max(cursor_time, x_min), x_max)
        if name == "A":
            self._cursor_a_time = cursor_time
        else:
            self._cursor_b_time = cursor_time
        self._update_measurement_display()
        master_y = sum(self.master_viewbox.viewRange()[1]) * 0.5
        self.update_map_marker(
            self.master_viewbox.mapViewToScene(QPointF(cursor_time, master_y))
        )

    def _set_box_zoom_enabled(self, enabled):
        for plot in tuple(getattr(self, "plot_widgets", ())):
            try:
                view_box = plot.getViewBox()
                view_box.setMouseEnabled(x=True, y=not enabled)
                view_box.setMouseMode(
                    view_box.RectMode if enabled else view_box.PanMode
                )
            except RuntimeError:
                continue

    def reset_plot_view(self):
        self.box_zoom_button.setChecked(False)
        for plot in tuple(getattr(self, "plot_widgets", ())):
            try:
                view_box = plot.getViewBox()
                initial_range = getattr(plot, "initial_view_range", None)
                if initial_range is not None:
                    view_box.setRange(
                        xRange=initial_range[0],
                        yRange=initial_range[1],
                        padding=0,
                    )
                else:
                    view_box.enableAutoRange(x=True, y=True)
                    view_box.autoRange(padding=0.02)
                    view_box.enableAutoRange(x=False, y=False)
                manager = getattr(plot, "multi_y_axis_manager", None)
                if manager is not None:
                    for signal_key, y_range in getattr(
                        plot, "initial_signal_y_ranges", {}
                    ).items():
                        if signal_key in manager.signal_keys:
                            manager.set_y_range(signal_key, y_range)
            except RuntimeError:
                continue

    def _format_measurement_value(self, record, cursor_time):
        signal = record.get("signal_data")
        if signal is None or not len(signal.timestamps):
            return None
        timestamps = signal.timestamps
        index = int(np.searchsorted(timestamps, cursor_time))
        if index >= len(timestamps) or cursor_time < timestamps[0]:
            return None
        if index > 0 and abs(cursor_time - timestamps[index - 1]) <= abs(timestamps[index] - cursor_time):
            index -= 1
        value = signal.samples[index]
        try:
            if not math.isfinite(float(value)):
                return None
        except (TypeError, ValueError):
            pass
        if record.get("is_text_signal"):
            raw = getattr(signal, "raw_text_values", None)
            if raw is not None and index < len(raw):
                return str(raw[index])
            mapping = getattr(signal, "text_mapping", None) or ()
            number = int(round(float(value)))
            return str(mapping[number]) if 0 <= number < len(mapping) else str(value)
        return float(value)

    def _measurement_sample_y(self, record, cursor_time):
        signal = record.get("signal_data")
        if signal is None or not len(signal.timestamps):
            return None
        timestamps = signal.timestamps
        index = int(np.searchsorted(timestamps, cursor_time))
        if index >= len(timestamps) or cursor_time < timestamps[0]:
            return None
        if index > 0 and abs(cursor_time - timestamps[index - 1]) <= abs(timestamps[index] - cursor_time):
            index -= 1
        try:
            return float(signal.samples[index])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _position_cursor_label(plot, label, cursor_time, sample_y, view_box=None):
        view_box = view_box or plot.getViewBox()
        if sample_y is None or not np.isfinite(sample_y):
            sample_y = sum(view_box.viewRange()[1]) * 0.5
        anchor = plot.mapFromScene(
            view_box.mapViewToScene(QPointF(float(cursor_time), float(sample_y)))
        )
        bounds = plot.mapFromScene(view_box.sceneBoundingRect()).boundingRect()
        # Preserve the established strategy: in the rightmost 30% of the plot,
        # put the readout on the left so it cannot cover the cursor/curve.
        place_left = anchor.x() > bounds.left() + bounds.width() * 0.7
        label.setAnchor((1, 0.5) if place_left else (0, 0.5))
        offset = -8 if place_left else 8
        scene_pos = plot.mapToScene(
            QPointF(anchor.x() + offset, anchor.y() - 10).toPoint()
        )
        label.setPos(view_box.mapSceneToView(scene_pos))
        label_rect = plot.mapFromScene(label.sceneBoundingRect()).boundingRect()
        dx = max(bounds.left() + 2 - label_rect.left(), 0)
        dy = max(bounds.top() + 2 - label_rect.top(), 0)
        if label_rect.right() > bounds.right() - 2:
            dx = bounds.right() - 2 - label_rect.right()
        if label_rect.bottom() > bounds.bottom() - 2:
            dy = bounds.bottom() - 2 - label_rect.bottom()
        if dx or dy:
            anchor_pos = plot.mapFromScene(label.scenePos())
            adjusted = plot.mapToScene(
                QPointF(anchor_pos.x() + dx, anchor_pos.y() + dy).toPoint()
            )
            label.setPos(view_box.mapSceneToView(adjusted))

    @staticmethod
    def _separate_cursor_labels(plot, cursor_name):
        if cursor_name == "single":
            return
        labels = getattr(plot, "cursor_labels", {})
        records = getattr(plot, "signal_records", ())
        for index, (a_label, b_label) in enumerate(zip(
            labels.get("A", ()), labels.get("B", ())
        )):
            if not (a_label.isVisible() and b_label.isVisible()):
                continue
            a_rect = a_label.sceneBoundingRect()
            b_rect = b_label.sceneBoundingRect()
            if a_rect.intersects(b_rect):
                view_box = (
                    records[index].get("view_box")
                    if index < len(records) else plot.getViewBox()
                )
                anchor = plot.mapFromScene(b_label.scenePos())
                bounds = plot.mapFromScene(view_box.sceneBoundingRect()).boundingRect()
                target_y = anchor.y() + a_rect.height() + 4
                if target_y + b_rect.height() > bounds.bottom() - 2:
                    target_y = anchor.y() - a_rect.height() - 4
                target_y = min(max(target_y, bounds.top() + 2), bounds.bottom() - b_rect.height() - 2)
                scene_pos = plot.mapToScene(QPointF(anchor.x(), target_y).toPoint())
                b_label.setPos(view_box.mapSceneToView(scene_pos))

    def _update_measurement_display(self):
        blocker = QSignalBlocker(self.selected_signal_list_widget)
        dual_enabled = self.cursor_mode == "dual"
        a_time = getattr(self, "_cursor_a_time", None) if dual_enabled else None
        b_time = getattr(self, "_cursor_b_time", None) if dual_enabled else None
        a_label = f"{a_time:.6f} s" if a_time is not None else "—"
        b_label = f"{b_time:.6f} s" if b_time is not None else "—"
        delta_label = f"{abs(b_time - a_time):.6f} s" if a_time is not None and b_time is not None else "—"
        self.measurement_bar.setText(
            f"A: {a_label}    B: {b_label}\nΔt: {delta_label}"
        )
        self.measurement_bar.setVisible(dual_enabled)
        for plot in tuple(getattr(self, "plot_widgets", ())):
            lines = getattr(plot, "measurement_lines", {})
            has_visible_curve = any(
                record.get("curve_visible", True)
                for record in getattr(plot, "signal_records", ())
            )
            for name, cursor_time in (("A", a_time), ("B", b_time)):
                line = lines.get(name)
                if line is not None:
                    line.setVisible(
                        dual_enabled and cursor_time is not None and has_visible_curve
                    )
                    if cursor_time is not None:
                        line.setPos(cursor_time)
                labels = getattr(plot, "cursor_labels", {}).get(name, ())
                for record_index, label in enumerate(labels):
                    if cursor_time is None or record_index >= len(plot.signal_records):
                        label.setVisible(False)
                        continue
                    record = plot.signal_records[record_index]
                    value = self._format_measurement_value(record, cursor_time)
                    if value is None:
                        label.setVisible(False)
                        continue
                    shown = f"{value:.3f}" if isinstance(value, (int, float)) else str(value)
                    label.setText(f"{cursor_time:.6f}, {shown}")
                    label.setVisible(
                        dual_enabled and record.get("curve_visible", True)
                    )
                    self._position_cursor_label(
                        plot, label, cursor_time,
                        self._measurement_sample_y(record, cursor_time),
                        view_box=record.get("view_box"),
                    )
                self._separate_cursor_labels(plot, name)
        for row in range(self.selected_signal_list_widget.count()):
            item = self.selected_signal_list_widget.item(row)
            key = item.data(KEY_ROLE)
            record = next((
                record for plot in self.plot_widgets
                for record in getattr(plot, "signal_records", ())
                if record.get("signal_key") == key
            ), None)
            if record is None or a_time is None or b_time is None:
                item.setData(MEASUREMENT_ROLE, None)
                item.setData(INTERVAL_ROLE, None)
                continue
            a_value = self._format_measurement_value(record, a_time)
            b_value = self._format_measurement_value(record, b_time)
            if isinstance(a_value, (int, float)) and isinstance(b_value, (int, float)):
                display = f"A {a_value:.3f}  B {b_value:.3f}  Δ {b_value-a_value:+.3f}"
            else:
                display = f"A {a_value if a_value is not None else '—'}  B {b_value if b_value is not None else '—'}"
            item.setData(MEASUREMENT_ROLE, display)
            interval = self._interval_statistics(record, a_time, b_time)
            item.setData(INTERVAL_ROLE, interval)
            item.setSizeHint(QSize(0, 54))
        del blocker

    @staticmethod
    def _interval_statistics(record, start_time, end_time):
        if record.get("is_text_signal"):
            return "区间统计：文本信号不适用"
        signal = record.get("signal_data")
        if signal is None or not len(signal.timestamps):
            return "区间统计：无有效数据"
        lower, upper = sorted((start_time, end_time))
        timestamps = np.asarray(signal.timestamps)
        # Match the cursor readout: interval statistics include the samples
        # nearest to both cursor positions, even when a drag lands a fraction
        # of a pixel beside an exact sample timestamp.
        start = int(np.searchsorted(timestamps, lower, side="left"))
        if start > 0 and abs(timestamps[start] - lower) > abs(lower - timestamps[start - 1]):
            start -= 1
        stop = int(np.searchsorted(timestamps, upper, side="left"))
        if stop < len(timestamps) and abs(timestamps[stop] - upper) <= abs(upper - timestamps[stop - 1]):
            stop += 1
        if stop < start:
            stop = start
        try:
            values = np.asarray(signal.samples[start:stop], dtype=float)
        except (TypeError, ValueError):
            return "区间统计：无有效数值"
        values = values[np.isfinite(values)]
        if not len(values):
            return "区间统计：无有效样本"
        rms = float(np.sqrt(np.mean(np.square(values))))
        return (
            f"区间 min {np.min(values):.3f}  max {np.max(values):.3f}  "
            f"平均 {np.mean(values):.3f}  RMS {rms:.3f}"
        )

    def _register_plot_stats_responsiveness(self, plot):
        """Keep statistics readable using the individual plot's usable width."""
        view_box = plot.getViewBox()
        plot._stats_are_visible = True
        plot._stats_last_width = self._plot_usable_width(plot)
        view_box.sigResized.connect(
            lambda *_args, widget=plot: self._plot_stats_resize_changed(widget)
        )
        self._plot_stats_visibility_changed(plot)

    def _plot_stats_resize_changed(self, plot):
        """Ignore height-only changes so plot-height controls remain side-effect free."""
        try:
            width = self._plot_usable_width(plot)
            previous = float(getattr(plot, "_stats_last_width", width))
            plot._stats_last_width = width
            if abs(width - previous) >= 1.0:
                self._plot_stats_visibility_changed(plot)
                self._align_shared_time_axis_geometry()
        except RuntimeError:
            return

    def _plot_usable_width(self, plot):
        view_box = plot.getViewBox()
        return float(view_box.sceneBoundingRect().width()) if view_box is not None else 0.0

    def _plot_stats_visibility_changed(self, plot):
        try:
            stats_item = plot.stats_text_item
            width = self._plot_usable_width(plot)
            visible = bool(getattr(plot, "_stats_are_visible", True))
            if width <= 0:
                return visible
            if visible and width < self._STATS_HIDE_WIDTH:
                visible = False
            elif not visible and width > self._STATS_SHOW_WIDTH:
                visible = True
            plot._stats_are_visible = visible
            stats_item.setVisible(visible)
            return visible
        except RuntimeError:
            return False

    def _update_stats(self, plot):
        self._plot_stats_visibility_changed(plot)
        super()._update_stats(plot)

    def update_cursor_positions(self, evt, source_plot=None):
        if (
            self.cursor_mode != "single"
            or self._dragging_cursor is None
            or self._dragging_cursor.get("name") != "single"
        ):
            return
        super().update_cursor_positions(evt, source_plot)
        if self._single_cursor_time is None:
            return
        self._mouse_in_plot = True
        if self._view_mode == "selected":
            self._current_cursor_time = self._single_cursor_time
            self._list_update_timer.start()

    @staticmethod
    def _plot_axis_scroll_reserve(plot):
        scroll_bar = getattr(plot, "y_axis_scroll_bar", None)
        return scroll_bar.height() if scroll_bar is not None else 0

    def _apply_plot_height(self, plot):
        """Apply the height control as a real height after the user changes it."""
        if plot is None:
            return
        try:
            if self._plot_height_explicit and hasattr(plot, "setFixedHeight"):
                plot.setFixedHeight(self.plot_min_height)
            else:
                plot.setMinimumHeight(self.plot_min_height)
                if hasattr(plot, "setMaximumHeight"):
                    plot.setMaximumHeight(16_777_215)
            container = getattr(plot, "axis_scroll_container", None)
            if container is not None:
                target = self.plot_min_height + self._plot_axis_scroll_reserve(plot)
                if self._plot_height_explicit and hasattr(container, "setFixedHeight"):
                    container.setFixedHeight(target)
                else:
                    container.setMinimumHeight(target)
                    if hasattr(container, "setMaximumHeight"):
                        container.setMaximumHeight(16_777_215)
        except RuntimeError:
            # A queued height signal can arrive while Qt is destroying plots.
            return

    def _refresh_plot_scroll_content(self):
        container = getattr(self, "plot_container", None)
        if container is None:
            return
        layout = getattr(self, "plot_layout", None)
        if layout is not None:
            layout.activate()
        container.adjustSize()
        scroll_area = getattr(self, "plot_scroll_area", None)
        if scroll_area is not None:
            scroll_area.widget().updateGeometry()

    def _plot_height_changed(self, value):
        self.plot_min_height = value
        self._plot_height_explicit = True
        for plot in tuple(getattr(self, "plot_widgets", ())):
            self._apply_plot_height(plot)
        self._refresh_plot_scroll_content()
