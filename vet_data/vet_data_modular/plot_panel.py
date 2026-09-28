"""Curve interaction enhancements."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSpinBox, QWidget


class PlotPanelMixin:
    _STATS_HIDE_WIDTH = 300
    _STATS_SHOW_WIDTH = 340

    def _setup_plot_panel(self):
        self.plot_min_height = 150
        panel = next((g for g in self.findChildren(QWidget) if getattr(g, "title", lambda: "")() == "信号曲线"), None)
        if panel is None: return
        controls = QWidget(panel)
        row = QHBoxLayout(controls); row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("信号高度:"))
        self.height_spin = QSpinBox()
        self.height_spin.setRange(80, 900); self.height_spin.setSingleStep(10)
        self.height_spin.setValue(self.plot_min_height); self.height_spin.setSuffix(" px")
        row.addWidget(self.height_spin); row.addStretch()
        panel.layout().insertWidget(0, controls)
        self.height_spin.valueChanged.connect(self._plot_height_changed)
        self.plot_empty_label = QLabel("选择信号并点击“绘制所选信号”")
        self.plot_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.plot_empty_label.setProperty("uiEmptyState", True)
        self.plot_layout.addWidget(self.plot_empty_label)

    def _set_plot_empty_state(self, message):
        self.plot_empty_label.setText(message or "")
        self.plot_empty_label.setVisible(bool(message))

    def plot_selected_signals(self):
        self._set_plot_empty_state(None)
        super().plot_selected_signals()
        for plot in self.plot_widgets:
            plot.setMinimumHeight(self.plot_min_height)
            plot.getViewBox().setMouseEnabled(x=True, y=True)
            self._register_plot_stats_responsiveness(plot)

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

    def update_cursor_positions(self, evt):
        super().update_cursor_positions(evt)
        if not self.plot_widgets or self.master_viewbox is None: return
        point = self.master_viewbox.mapSceneToView(evt)
        master_range = self.master_viewbox.viewRange()
        inside = master_range[0][0] <= point.x() <= master_range[0][1] and master_range[1][0] <= point.y() <= master_range[1][1]
        self._mouse_in_plot = inside
        if not inside:
            self._clear_signal_values()
            return
        for plot in self.plot_widgets:
            x_min, x_max = plot.getViewBox().viewRange()[0]
            x_range = x_max - x_min
            plot.text_item.setAnchor((1, 1) if x_range > 0 and x_max - point.x() < x_range * .30 else (0, 1))
        if self._view_mode == "selected":
            self._current_cursor_time = point.x()
            self._list_update_timer.start()

    def _plot_height_changed(self, value):
        self.plot_min_height = value
        for plot in tuple(getattr(self, "plot_widgets", ())):
            if plot is None:
                continue
            try:
                plot.setMinimumHeight(value)
            except RuntimeError:
                # A queued height signal can arrive while Qt is destroying plots.
                continue
