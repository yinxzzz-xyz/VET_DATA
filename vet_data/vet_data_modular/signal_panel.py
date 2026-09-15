"""High-performance signal selection, filtering, values and ordering."""

import numpy as np
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QHBoxLayout, QListWidget, QListWidgetItem, QPushButton,
    QStyledItemDelegate, QStyle, QWidget,
)


KEY_ROLE = Qt.ItemDataRole.UserRole
NAME_ROLE = Qt.ItemDataRole.UserRole + 1
VALUE_ROLE = Qt.ItemDataRole.UserRole + 2
COMMENT_ROLE = Qt.ItemDataRole.UserRole + 3


class SignalItemDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        # The model's display text is intentionally empty, so the base delegate
        # paints selection/focus/checkbox without duplicating our columns.
        super().paint(painter, option, index)
        painter.save()
        item = self.parent().item(index.row())
        text_color = option.palette.highlightedText().color() if option.state & QStyle.StateFlag.State_Selected else option.palette.text().color()
        painter.setPen(text_color)
        name = item.data(NAME_ROLE) or item.text()
        value = item.data(VALUE_ROLE)
        comment = item.data(COMMENT_ROLE) or ""
        left = option.rect.adjusted(24, 0, -8, 0)
        value_width = 105 if value is not None else 0
        comment_width = min(150, max(0, left.width() // 3))
        name_rect = left.adjusted(0, 0, -(value_width + comment_width), 0)
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, str(name))
        if comment:
            painter.setPen(QColor("#7f8c8d"))
            comment_rect = left.adjusted(left.width() - value_width - comment_width, 0, -value_width, 0)
            painter.drawText(comment_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(comment))
        if value is not None:
            painter.setPen(QColor("#1565c0"))
            value_rect = left.adjusted(left.width() - value_width, 0, 0, 0)
            painter.drawText(value_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(value))
        painter.restore()


class _CheckAdapter:
    """Compatibility bridge for baseline code that expects QCheckBox objects."""
    def __init__(self, item): self.item = item
    def isChecked(self): return self.item.checkState() == Qt.CheckState.Checked
    def setChecked(self, value): self.item.setCheckState(Qt.CheckState.Checked if value else Qt.CheckState.Unchecked)
    def show(self): self.item.setHidden(False)
    def hide(self): self.item.setHidden(True)
    def setParent(self, _): pass
    def deleteLater(self): pass


class SignalPanelMixin:
    def _setup_signal_panel(self):
        self._view_mode = "all"
        self._user_signal_order = {}
        self._current_cursor_time = None
        self._mouse_in_plot = False
        self._sig_value_cache = {}
        self._signal_panel_ready = True

        group = next((g for g in self.findChildren(QWidget) if getattr(g, "title", lambda: "")() == "可用信号"), None)
        if group is None:
            raise RuntimeError("Cannot locate signal panel")
        controls = QWidget(group)
        row = QHBoxLayout(controls)
        row.setContentsMargins(0, 0, 0, 0)
        self.view_all_btn = QPushButton("全部信号")
        self.view_selected_btn = QPushButton("已选信号 (0)")
        self.reset_order_btn = QPushButton("↺ 恢复默认排序")
        for button in (self.view_all_btn, self.view_selected_btn):
            button.setCheckable(True)
        self.view_all_btn.setChecked(True)
        self.reset_order_btn.setVisible(False)
        row.addWidget(self.view_all_btn)
        row.addWidget(self.view_selected_btn)
        row.addWidget(self.reset_order_btn)
        group.layout().insertWidget(0, controls)

        self.signal_list_widget = QListWidget()
        self.signal_list_widget.setUniformItemSizes(True)
        self.signal_list_widget.setSpacing(1)
        self.signal_list_widget.setItemDelegate(SignalItemDelegate(self.signal_list_widget))
        self.signal_layout.addWidget(self.signal_list_widget)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(100)
        self._search_timer.timeout.connect(self._apply_signal_filter)
        self._list_update_timer = QTimer(self)
        self._list_update_timer.setSingleShot(True)
        self._list_update_timer.setInterval(50)
        self._list_update_timer.timeout.connect(self._update_signal_list_values)

        self.view_all_btn.clicked.connect(lambda: self._set_view_mode("all"))
        self.view_selected_btn.clicked.connect(lambda: self._set_view_mode("selected"))
        self.reset_order_btn.clicked.connect(self._reset_signal_order)
        self.signal_list_widget.itemChanged.connect(lambda _item: self._selection_changed())
        self.signal_list_widget.model().rowsMoved.connect(self._signal_rows_moved)

    def refresh_signal_list_ui(self):
        if not getattr(self, "_signal_panel_ready", False):
            return super().refresh_signal_list_ui()
        checked = set(self.get_selected_signals())
        self.signal_list_widget.blockSignals(True)
        self.signal_list_widget.setUpdatesEnabled(False)
        self.signal_list_widget.clear()
        self.signal_widgets.clear()
        valid = set(self.signals)
        self._user_signal_order = {k: v for k, v in self._user_signal_order.items() if k in valid}

        def default_key(key):
            info = self.signals[key]
            kind = 0 if key.startswith("MATH_") else 1 if key.startswith("CAN_") else 2
            return kind, info.get("display_name", key).lower()
        keys = sorted(self.signals, key=lambda k: (self._user_signal_order.get(k, 10**9), default_key(k)))
        self.lat_combo.blockSignals(True); self.lon_combo.blockSignals(True)
        lat_key = self.lat_combo.currentData(); lon_key = self.lon_combo.currentData()
        self.lat_combo.clear(); self.lon_combo.clear()
        self.search_index = {}
        for key in keys:
            info = self.signals[key]
            name = info.get("display_name", key)
            bus_id = info.get("bus_id")
            if bus_id is not None and "[" not in name:
                bus_name = self.can_bus_data.get(bus_id, {}).get("name", f"Bus {bus_id}")
                name = f"{name} [🚌 {bus_name}]"
            comment = info.get("comment", "") or ""
            item = QListWidgetItem("")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            item.setCheckState(Qt.CheckState.Checked if key in checked else Qt.CheckState.Unchecked)
            item.setData(KEY_ROLE, key); item.setData(NAME_ROLE, name); item.setData(COMMENT_ROLE, comment)
            item.setToolTip(comment)
            self.signal_list_widget.addItem(item)
            self.signal_widgets[key] = {"checkbox": _CheckAdapter(item), "comment_label": None}
            self.search_index[key] = f"{info.get('name','')} {name} {comment}".lower()
            self.lat_combo.addItem(name, key); self.lon_combo.addItem(name, key)
        for combo, selected in ((self.lat_combo, lat_key), (self.lon_combo, lon_key)):
            idx = combo.findData(selected)
            if idx >= 0: combo.setCurrentIndex(idx)
            combo.blockSignals(False)
        self.signal_list_widget.blockSignals(False)
        self.signal_list_widget.setUpdatesEnabled(True)
        self._selection_changed(replot=False)
        self._apply_signal_filter()
        self.set_buttons_enabled(bool(self.signals))

    def search_signals(self, text):
        self._search_pending_text = text.strip().lower()
        if getattr(self, "_signal_panel_ready", False): self._search_timer.start()
        else: super().search_signals(text)

    def _apply_signal_filter(self):
        query = getattr(self, "_search_pending_text", "")
        self.signal_list_widget.setUpdatesEnabled(False)
        for row in range(self.signal_list_widget.count()):
            item = self.signal_list_widget.item(row)
            key = item.data(KEY_ROLE)
            visible = (not query or query in self.search_index.get(key, ""))
            if self._view_mode == "selected": visible = visible and item.checkState() == Qt.CheckState.Checked
            item.setHidden(not visible)
        self.signal_list_widget.setUpdatesEnabled(True)

    def get_selected_signals(self):
        if not getattr(self, "_signal_panel_ready", False): return super().get_selected_signals()
        return [self.signal_list_widget.item(i).data(KEY_ROLE) for i in range(self.signal_list_widget.count()) if self.signal_list_widget.item(i).checkState() == Qt.CheckState.Checked]

    def select_all_signals(self):
        for i in range(self.signal_list_widget.count()):
            item = self.signal_list_widget.item(i)
            if not item.isHidden(): item.setCheckState(Qt.CheckState.Checked)

    def deselect_all_signals(self):
        for i in range(self.signal_list_widget.count()): self.signal_list_widget.item(i).setCheckState(Qt.CheckState.Unchecked)

    def _set_view_mode(self, mode):
        self._view_mode = mode
        self.view_all_btn.setChecked(mode == "all"); self.view_selected_btn.setChecked(mode == "selected")
        self.signal_list_widget.setDragDropMode(QListWidget.DragDropMode.InternalMove if mode == "selected" else QListWidget.DragDropMode.NoDragDrop)
        self.reset_order_btn.setVisible(mode == "selected" and bool(self._user_signal_order))
        if mode != "selected": self._clear_signal_values()
        self._apply_signal_filter()

    def _selection_changed(self, replot=True):
        count = len(self.get_selected_signals())
        self.view_selected_btn.setText(f"已选信号 ({count})")
        if self._view_mode == "selected": self._apply_signal_filter()

    def _signal_rows_moved(self, *_args):
        if self._view_mode != "selected": return
        self._user_signal_order = {self.signal_list_widget.item(i).data(KEY_ROLE): i for i in range(self.signal_list_widget.count())}
        self.reset_order_btn.setVisible(True)

    def _reset_signal_order(self):
        self._user_signal_order.clear(); self.refresh_signal_list_ui()

    def _update_signal_list_values(self):
        if self._current_cursor_time is None or not self._mouse_in_plot or self._view_mode != "selected": return
        for i in range(self.signal_list_widget.count()):
            item = self.signal_list_widget.item(i)
            if item.checkState() != Qt.CheckState.Checked: continue
            key = item.data(KEY_ROLE)
            if key not in self._sig_value_cache:
                signal = self._get_signal(self.signals.get(key, {}))
                if signal is None or len(signal.timestamps) == 0: continue
                self._sig_value_cache[key] = signal
            signal = self._sig_value_cache[key]
            idx = int(np.clip(np.searchsorted(signal.timestamps, self._current_cursor_time), 0, len(signal.timestamps)-1))
            value = signal.samples[idx]
            if hasattr(signal, "is_text_signal") and signal.is_text_signal and hasattr(signal, "raw_text_values"):
                shown = signal.raw_text_values[idx]
            else:
                try: shown = "NaN" if np.isnan(value) else f"{value:.3f}"
                except TypeError: shown = str(value)
            item.setData(VALUE_ROLE, shown)
        self.signal_list_widget.viewport().update()

    def _clear_signal_values(self):
        if not hasattr(self, "signal_list_widget"): return
        for i in range(self.signal_list_widget.count()): self.signal_list_widget.item(i).setData(VALUE_ROLE, None)
        self._sig_value_cache.clear()
