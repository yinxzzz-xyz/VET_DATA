"""Curve interaction enhancements."""

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSpinBox, QWidget


class PlotPanelMixin:
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

    def plot_selected_signals(self):
        super().plot_selected_signals()
        for plot in self.plot_widgets:
            plot.setMinimumHeight(self.plot_min_height)
            plot.getViewBox().setMouseEnabled(x=True, y=True)

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
        if self.plot_widgets: self.plot_selected_signals()
