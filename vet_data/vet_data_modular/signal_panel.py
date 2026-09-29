"""High-performance signal selection, filtering, values and ordering."""

import numpy as np
from PyQt6.QtCore import QSignalBlocker, Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QStyledItemDelegate, QStyle, QWidget,
)


KEY_ROLE = Qt.ItemDataRole.UserRole
NAME_ROLE = Qt.ItemDataRole.UserRole + 1
VALUE_ROLE = Qt.ItemDataRole.UserRole + 2
COMMENT_ROLE = Qt.ItemDataRole.UserRole + 3
SELECTED_TEXT_COLOR = QColor("#2c3e50")


class SignalItemDelegate(QStyledItemDelegate):
    @staticmethod
    def text_color(option):
        if option.state & QStyle.StateFlag.State_Selected:
            return SELECTED_TEXT_COLOR
        return option.palette.text().color()

    def paint(self, painter, option, index):
        # The model's display text is intentionally empty, so the base delegate
        # paints selection/focus/checkbox without duplicating our columns.
        super().paint(painter, option, index)
        painter.save()
        item = self.parent().item(index.row())
        painter.setPen(self.text_color(option))
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
        self._last_signal_list_snapshot = None
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
            button.setProperty("uiRole", "signalView")
        self.view_all_btn.setChecked(True)
        self.reset_order_btn.setVisible(False)
        row.addWidget(self.view_all_btn)
        row.addWidget(self.view_selected_btn)
        row.addWidget(self.reset_order_btn)
        group.layout().insertWidget(0, controls)

        self.signal_empty_label = QLabel("加载数据后，可在此选择和筛选信号")
        self.signal_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.signal_empty_label.setProperty("uiEmptyState", True)
        self.signal_layout.addWidget(self.signal_empty_label)

        self.signal_list_widget = QListWidget()
        self.signal_list_widget.setUniformItemSizes(True)
        self.signal_list_widget.setSpacing(1)
        self.signal_list_widget.setItemDelegate(SignalItemDelegate(self.signal_list_widget))
        self.signal_layout.addWidget(self.signal_list_widget)
        self.signal_list_widget.setVisible(False)

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

    def _signal_display_name(self, key):
        info = self.signals[key]
        name = info.get("display_name", key)
        bus_id = info.get("bus_id")
        if bus_id is not None and "[" not in name:
            bus_name = self.can_bus_data.get(bus_id, {}).get("name", f"Bus {bus_id}")
            name = f"{name} [\U0001F68C {bus_name}]"
        return name

    def _signal_list_default_key(self, key):
        info = self.signals[key]
        kind = 0 if key.startswith("MATH_") else 1 if key.startswith("CAN_") else 2
        return kind, info.get("display_name", key).lower()

    def _ordered_signal_keys(self):
        return sorted(
            self.signals,
            key=lambda key: (
                self._user_signal_order.get(key, 10**9),
                self._signal_list_default_key(key),
            ),
        )

    def _signal_list_snapshot(self):
        signals = tuple(
            (
                key,
                info.get("name", ""),
                info.get("display_name", key),
                info.get("comment", "") or "",
                info.get("bus_id"),
                self.can_bus_data.get(info.get("bus_id"), {}).get("name")
                if info.get("bus_id") is not None else None,
                self._signal_list_default_key(key)[0],
            )
            for key, info in self.signals.items()
        )
        return signals, tuple(sorted(self._user_signal_order.items()))

    def refresh_signal_list_ui(self):
        if not getattr(self, "_signal_panel_ready", False):
            return super().refresh_signal_list_ui()
        snapshot = self._signal_list_snapshot()
        if snapshot == self._last_signal_list_snapshot:
            has_signals = bool(self.signals)
            self.signal_empty_label.setVisible(not has_signals)
            self.signal_list_widget.setVisible(has_signals)
            self.set_buttons_enabled(bool(self.signals))
            return
        checked = set(self.get_selected_signals())
        self.signal_list_widget.blockSignals(True)
        self.signal_list_widget.setUpdatesEnabled(False)
        self.signal_list_widget.clear()
        self.signal_widgets.clear()
        valid = set(self.signals)
        self._user_signal_order = {k: v for k, v in self._user_signal_order.items() if k in valid}

        keys = self._ordered_signal_keys()
        self.lat_combo.blockSignals(True); self.lon_combo.blockSignals(True)
        lat_key = self.lat_combo.currentData(); lon_key = self.lon_combo.currentData()
        self.lat_combo.clear(); self.lon_combo.clear()
        self.search_index = {}
        for key in keys:
            info = self.signals[key]
            name = self._signal_display_name(key)
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
        has_signals = bool(self.signals)
        self.signal_empty_label.setVisible(not has_signals)
        self.signal_list_widget.setVisible(has_signals)
        self._selection_changed(replot=False)
        self._apply_signal_filter()
        self.set_buttons_enabled(bool(self.signals))
        self._last_signal_list_snapshot = self._signal_list_snapshot()

    def _move_signal_item(self, key, target_row):
        item = self.signal_widgets[key]["checkbox"].item
        source_row = self.signal_list_widget.row(item)
        if source_row == target_row:
            return
        item = self.signal_list_widget.takeItem(source_row)
        self.signal_list_widget.insertItem(target_row, item)

    def _sync_combo_metadata(self, combo, names):
        rows = {combo.itemData(row): row for row in range(combo.count())}
        for key, name in names.items():
            row = rows.get(key)
            if row is not None:
                combo.setItemText(row, name)

    def update_signal_metadata_ui(self, key):
        return self.update_signal_metadata_batch((key,))

    def update_signal_metadata_batch(self, keys):
        """Update existing signal metadata without recreating list items."""
        if not getattr(self, "_signal_panel_ready", False):
            self.refresh_signal_list_ui()
            return False
        keys = tuple(dict.fromkeys(keys))
        if not keys:
            return True
        if any(key not in self.signals or key not in self.signal_widgets for key in keys):
            self.refresh_signal_list_ui()
            return False

        current_item = self.signal_list_widget.currentItem()
        scroll_value = self.signal_list_widget.verticalScrollBar().value()
        lat_key = self.lat_combo.currentData()
        lon_key = self.lon_combo.currentData()
        names = {}

        self.signal_list_widget.blockSignals(True)
        self.signal_list_widget.setUpdatesEnabled(False)
        self.lat_combo.blockSignals(True)
        self.lon_combo.blockSignals(True)
        try:
            for key in keys:
                info = self.signals[key]
                name = self._signal_display_name(key)
                comment = info.get("comment", "") or ""
                item = self.signal_widgets[key]["checkbox"].item
                item.setData(NAME_ROLE, name)
                item.setData(COMMENT_ROLE, comment)
                item.setToolTip(comment)
                self.search_index[key] = f"{info.get('name','')} {name} {comment}".lower()
                names[key] = name

            self._sync_combo_metadata(self.lat_combo, names)
            self._sync_combo_metadata(self.lon_combo, names)

            desired_keys = self._ordered_signal_keys()
            desired_rows = {key: row for row, key in enumerate(desired_keys)}
            if len(keys) == 1:
                self._move_signal_item(keys[0], desired_rows[keys[0]])
            elif any(
                self.signal_list_widget.item(row).data(KEY_ROLE) != key
                for row, key in enumerate(desired_keys)
            ):
                items = {}
                for row in range(self.signal_list_widget.count() - 1, -1, -1):
                    item = self.signal_list_widget.takeItem(row)
                    items[item.data(KEY_ROLE)] = item
                for desired_key in desired_keys:
                    self.signal_list_widget.addItem(items[desired_key])

            if current_item is not None:
                self.signal_list_widget.setCurrentItem(current_item)
            for combo, selected in ((self.lat_combo, lat_key), (self.lon_combo, lon_key)):
                row = combo.findData(selected)
                if row >= 0:
                    combo.setCurrentIndex(row)
            self._apply_signal_filter()
            self.signal_list_widget.verticalScrollBar().setValue(scroll_value)
            self._last_signal_list_snapshot = self._signal_list_snapshot()
        finally:
            self.lat_combo.blockSignals(False)
            self.lon_combo.blockSignals(False)
            self.signal_list_widget.blockSignals(False)
            self.signal_list_widget.setUpdatesEnabled(True)
        return True

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
        value_updates = []
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
            value_updates.append((item, shown))
        blocker = QSignalBlocker(self.signal_list_widget)
        try:
            for item, shown in value_updates:
                item.setData(VALUE_ROLE, shown)
        finally:
            del blocker
        self.signal_list_widget.viewport().update()

    def _clear_signal_values(self):
        if not hasattr(self, "signal_list_widget"): return
        blocker = QSignalBlocker(self.signal_list_widget)
        try:
            for i in range(self.signal_list_widget.count()):
                self.signal_list_widget.item(i).setData(VALUE_ROLE, None)
        finally:
            del blocker
        self._sig_value_cache.clear()
        self.signal_list_widget.viewport().update()
