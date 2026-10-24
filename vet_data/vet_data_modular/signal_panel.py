"""High-performance signal selection, filtering, values and ordering."""

import numpy as np
import pyqtgraph as pg
import re
from PyQt6.QtCore import QEvent, QSignalBlocker, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QColorDialog, QStyledItemDelegate, QStyle, QWidget,
)


KEY_ROLE = Qt.ItemDataRole.UserRole
NAME_ROLE = Qt.ItemDataRole.UserRole + 1
VALUE_ROLE = Qt.ItemDataRole.UserRole + 2
COMMENT_ROLE = Qt.ItemDataRole.UserRole + 3
AVAILABLE_ROLE = Qt.ItemDataRole.UserRole + 4
COLOR_ROLE = Qt.ItemDataRole.UserRole + 5
MEASUREMENT_ROLE = Qt.ItemDataRole.UserRole + 6
INTERVAL_ROLE = Qt.ItemDataRole.UserRole + 7
_UNCHANGED_VALUE = object()
SELECTED_TEXT_COLOR = QColor("#2c3e50")
SIGNAL_COLORS = (
    "#2563EB", "#16A34A", "#DC2626", "#9333EA", "#D97706", "#0891B2",
    "#DB2777", "#4F46E5", "#65A30D", "#EA580C", "#0F766E", "#7C3AED",
)


class SignalItemDelegate(QStyledItemDelegate):
    colorRequested = pyqtSignal(str)

    _CHECKBOX_REGION = 22
    _CHECKBOX_COLOR_GAP = 9
    _COLOR_SIZE = 10
    _COLOR_TEXT_GAP = 7

    @staticmethod
    def color_rect(rect):
        left = rect.left() + SignalItemDelegate._CHECKBOX_REGION + SignalItemDelegate._CHECKBOX_COLOR_GAP
        return QRect(left, rect.center().y() - SignalItemDelegate._COLOR_SIZE // 2,
                     SignalItemDelegate._COLOR_SIZE, SignalItemDelegate._COLOR_SIZE)

    @staticmethod
    def selected_name_value_rects(rect):
        split_y = rect.top() + rect.height() // 2
        return (
            QRect(rect.left(), rect.top(), rect.width(), split_y - rect.top()),
            QRect(rect.left(), split_y, rect.width(), rect.bottom() - split_y + 1),
        )

    @staticmethod
    def selected_value_text(value):
        return "—" if value is None else str(value)

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.Type.MouseButtonRelease:
            position = event.position().toPoint()
            if self.color_rect(option.rect).contains(position):
                key = index.data(KEY_ROLE)
                if key is not None:
                    self.colorRequested.emit(str(key))
                return True
        return super().editorEvent(event, model, option, index)

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
        color = item.data(COLOR_ROLE)
        if color is not None:
            painter.fillRect(self.color_rect(option.rect), color)
        if item.data(AVAILABLE_ROLE) is False:
            painter.setPen(QColor("#9e9e9e"))
        else:
            painter.setPen(self.text_color(option))
        name = item.data(NAME_ROLE) or item.text()
        value = item.data(VALUE_ROLE)
        measurement = item.data(MEASUREMENT_ROLE)
        interval = item.data(INTERVAL_ROLE)
        comment = item.data(COMMENT_ROLE) or ""
        is_selected_list = self.parent().objectName() == "selectedSignalList"
        text_left = (
            self._CHECKBOX_REGION + self._CHECKBOX_COLOR_GAP
            + self._COLOR_SIZE + self._COLOR_TEXT_GAP
        )
        left = option.rect.adjusted(text_left, 0, -8, 0)
        value_width = 0 if measurement else (105 if value is not None else 0)
        show_comment = not is_selected_list
        comment_width = min(150, max(0, left.width() // 3)) if show_comment else 0
        name_rect = left.adjusted(0, 0, -(value_width + comment_width), 0)
        if is_selected_list and not (measurement or interval):
            name_line_rect, value_line_rect = self.selected_name_value_rects(left)
            display_name = option.fontMetrics.elidedText(
                str(name), Qt.TextElideMode.ElideRight, max(0, name_line_rect.width())
            )
            painter.setPen(self.text_color(option))
            painter.drawText(
                name_line_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                display_name,
            )
            painter.setPen(QColor("#1565c0"))
            display_value = self.selected_value_text(value)
            display_value = option.fontMetrics.elidedText(
                display_value, Qt.TextElideMode.ElideRight, max(0, value_line_rect.width())
            )
            painter.drawText(
                value_line_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                display_value,
            )
            painter.restore()
            return
        display_name = option.fontMetrics.elidedText(
            str(name), Qt.TextElideMode.ElideRight, max(0, name_rect.width())
        )
        name_flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        if measurement or interval:
            name_rect.setBottom(name_rect.top() + max(1, option.rect.height() // 3))
        painter.drawText(name_rect, name_flags, display_name)
        if comment:
            painter.setPen(QColor("#7f8c8d"))
            comment_rect = left.adjusted(left.width() - value_width - comment_width, 0, -value_width, 0)
            painter.drawText(comment_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(comment))
        if measurement:
            painter.setPen(QColor("#1565c0"))
            painter.drawText(
                left.adjusted(0, option.rect.height() // 3, 0, -option.rect.height() // 3),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                str(measurement),
            )
        elif value is not None:
            painter.setPen(QColor("#1565c0"))
            value_rect = left.adjusted(left.width() - value_width, 0, 0, 0)
            painter.drawText(value_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(value))
        if interval:
            painter.setPen(QColor("#52616b"))
            painter.drawText(
                left.adjusted(0, option.rect.height() * 2 // 3, 0, 0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                str(interval),
            )
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
        self._signal_color_indices = {}
        self._current_cursor_time = None
        self._mouse_in_plot = False
        self._sig_value_cache = {}
        self._last_signal_list_snapshot = None
        self._signal_panel_ready = True

        group = next((g for g in self.findChildren(QWidget) if getattr(g, "title", lambda: "")() == "可用信号"), None)
        if group is None:
            raise RuntimeError("Cannot locate signal panel")
        controls = QWidget(self.signal_ribbon_page)
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
        self.signal_empty_label = QLabel("加载数据后，可在此选择和筛选信号")
        self.signal_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.signal_empty_label.setProperty("uiEmptyState", True)
        self.signal_layout.addWidget(self.signal_empty_label)

        self.signal_view_controls = controls
        self.signal_ribbon_layout.insertWidget(2, controls)

        selected_header = QWidget(self.selected_signals_page)
        selected_header_layout = QHBoxLayout(selected_header)
        selected_header_layout.setContentsMargins(0, 0, 0, 0)
        self.selected_count_label = QLabel("已选 0 个信号")
        self.selected_count_label.setProperty("uiTextRole", "secondary")
        selected_header_layout.addWidget(self.selected_count_label)
        selected_header_layout.addStretch(1)
        self.selected_signals_layout.addWidget(selected_header)

        self.selected_signal_empty_label = QLabel("勾选可用信号后，会显示在这里")
        self.selected_signal_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.selected_signal_empty_label.setProperty("uiEmptyState", True)
        self.selected_signals_layout.addWidget(self.selected_signal_empty_label)
        self.selected_signal_list_widget = QListWidget()
        self.selected_signal_list_widget.setObjectName("selectedSignalList")
        self.selected_signal_list_widget.setUniformItemSizes(True)
        self.selected_signal_list_widget.setSpacing(1)
        self.selected_signal_delegate = SignalItemDelegate(self.selected_signal_list_widget)
        self.selected_signal_delegate.colorRequested.connect(self._choose_signal_color)
        self.selected_signal_list_widget.setItemDelegate(self.selected_signal_delegate)
        self.selected_signal_list_widget.setDragDropMode(
            QListWidget.DragDropMode.InternalMove
        )
        self.selected_signals_layout.addWidget(self.selected_signal_list_widget, 1)
        self.selected_signal_list_widget.itemChanged.connect(
            self._selected_signal_item_changed
        )
        self.selected_signal_list_widget.model().rowsMoved.connect(
            self._selected_signal_rows_moved
        )

        self.signal_list_widget = QListWidget()
        self.signal_list_widget.setUniformItemSizes(True)
        self.signal_list_widget.setSpacing(1)
        self.signal_list_delegate = SignalItemDelegate(self.signal_list_widget)
        self.signal_list_delegate.colorRequested.connect(self._choose_signal_color)
        self.signal_list_widget.setItemDelegate(self.signal_list_delegate)
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
        self.signal_list_widget.itemChanged.connect(
            lambda item: self._selection_changed(item=item)
        )
        self.signal_list_widget.model().rowsMoved.connect(self._signal_rows_moved)
        self.left_tabs.currentChanged.connect(self._left_tab_changed)

    def _signal_display_name(self, key):
        info = self.signals[key]
        name = str(info.get("display_name", key))
        bus_id = info.get("bus_id")
        if bus_id is not None:
            bus_name = self.can_bus_data.get(bus_id, {}).get("name", f"Bus {bus_id}")
            prefix = re.match(r"^\s*bus\s*(\d+)\s*[—–-]\s*", name, re.IGNORECASE)
            suffix = re.search(r"\s*\[\s*bus\s*(\d+)\s*\]\s*$", name, re.IGNORECASE)
            expected_bus_numbers = {str(bus_id)}
            named_number = re.search(r"(\d+)\s*$", str(bus_name))
            if named_number:
                expected_bus_numbers.add(named_number.group(1))
            if (
                prefix and suffix
                and prefix.group(1) in expected_bus_numbers
                and suffix.group(1) in expected_bus_numbers
            ):
                return name[:suffix.start()].rstrip()
            if prefix and prefix.group(1) in expected_bus_numbers:
                return name
            if "[" not in name:
                name = f"{name} [🚌 {bus_name}]"
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
                info.get("available", True),
                info.get("unavailable_reason", ""),
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
        self._signal_color_indices = {
            key: index for index, key in enumerate(keys)
        }
        self.lat_combo.blockSignals(True); self.lon_combo.blockSignals(True)
        lat_key = self.lat_combo.currentData(); lon_key = self.lon_combo.currentData()
        self.lat_combo.clear(); self.lon_combo.clear()
        self.search_index = {}
        for key in keys:
            info = self.signals[key]
            name = self._signal_display_name(key)
            comment = info.get("comment", "") or ""
            unavailable_reason = info.get("unavailable_reason", "") or ""
            item = QListWidgetItem("")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            item.setCheckState(Qt.CheckState.Checked if key in checked else Qt.CheckState.Unchecked)
            item.setData(KEY_ROLE, key); item.setData(NAME_ROLE, name); item.setData(COMMENT_ROLE, comment)
            item.setData(COLOR_ROLE, self._signal_color(key))
            item.setData(AVAILABLE_ROLE, info.get("available", True))
            item.setToolTip("\n".join(part for part in (name, comment, unavailable_reason) if part))
            self.signal_list_widget.addItem(item)
            self.signal_widgets[key] = {"checkbox": _CheckAdapter(item), "comment_label": None}
            self.search_index[key] = f"{info.get('name','')} {name} {comment} {unavailable_reason}".lower()
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

    def _signal_color(self, key):
        info = self.signals.get(key, {})
        configured = info.get("display_color")
        if configured:
            color = QColor(configured)
            if color.isValid():
                return color
        index = self._signal_color_indices.get(key, 0)
        return QColor(SIGNAL_COLORS[index % len(SIGNAL_COLORS)])

    def _choose_signal_color(self, key):
        if key not in self.signals:
            return
        initial = self._signal_color(key)
        color = QColorDialog.getColor(initial, self, "选择信号曲线颜色")
        if not color.isValid():
            return
        self.signals[key]["display_color"] = color.name()
        for widget in (self.signal_list_widget, self.selected_signal_list_widget):
            for row in range(widget.count()):
                item = widget.item(row)
                if item.data(KEY_ROLE) == key:
                    item.setData(COLOR_ROLE, color)
            widget.viewport().update()
        for plot in getattr(self, "plot_widgets", ()):
            for record in getattr(plot, "signal_records", ()):
                if record.get("signal_key") != key:
                    continue
                record["data_curve"].setPen(pg.mkPen(color, width=1.4))
                record["spot_item"].setBrush(pg.mkBrush(color))
        for plot in getattr(self, "plot_widgets", ()):
            plot.update()

    def _refresh_selected_signal_list(self):
        if not hasattr(self, "selected_signal_list_widget"):
            return
        selected = self.get_selected_signals()
        widget = self.selected_signal_list_widget
        scrollbar = widget.verticalScrollBar()
        scroll_value = scrollbar.value()
        current_item = widget.currentItem()
        current_key = current_item.data(KEY_ROLE) if current_item is not None else None
        widget.blockSignals(True)
        widget.setUpdatesEnabled(False)
        try:
            source_items = {
                self.signal_list_widget.item(row).data(KEY_ROLE): self.signal_list_widget.item(row)
                for row in range(self.signal_list_widget.count())
            }
            selected_set = set(selected)
            for row in range(widget.count() - 1, -1, -1):
                if widget.item(row).data(KEY_ROLE) not in selected_set:
                    widget.takeItem(row)

            for target_row, key in enumerate(selected):
                source_item = source_items.get(key)
                if source_item is None:
                    continue
                row = next((
                    index for index in range(target_row, widget.count())
                    if widget.item(index).data(KEY_ROLE) == key
                ), None)
                if row is None:
                    item = self._make_selected_signal_item(source_item)
                    widget.insertItem(target_row, item)
                elif row != target_row:
                    item = widget.takeItem(row)
                    widget.insertItem(target_row, item)
                else:
                    item = widget.item(target_row)
                for role in (KEY_ROLE, NAME_ROLE, COMMENT_ROLE, AVAILABLE_ROLE, COLOR_ROLE, MEASUREMENT_ROLE, INTERVAL_ROLE):
                    item.setData(role, source_item.data(role))
                item.setData(VALUE_ROLE, source_item.data(VALUE_ROLE))
                item.setToolTip(source_item.toolTip())
        finally:
            widget.setUpdatesEnabled(True)
            widget.blockSignals(False)
        if current_key is not None:
            for row in range(widget.count()):
                if widget.item(row).data(KEY_ROLE) == current_key:
                    widget.setCurrentRow(row)
                    break
        scrollbar.setValue(scroll_value)
        self.selected_count_label.setText(f"已选 {len(selected)} 个信号")
        self.selected_signal_empty_label.setVisible(not selected)
        widget.setVisible(bool(selected))
        update_measurements = getattr(self, "_update_measurement_display", None)
        if update_measurements is not None:
            update_measurements()

    @staticmethod
    def _make_selected_signal_item(source_item):
        item = QListWidgetItem("")
        item.setFlags(
            item.flags()
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsDragEnabled
        )
        item.setCheckState(Qt.CheckState.Checked)
        for role in (KEY_ROLE, NAME_ROLE, COMMENT_ROLE, AVAILABLE_ROLE, COLOR_ROLE, VALUE_ROLE):
            item.setData(role, source_item.data(role))
        item.setToolTip(source_item.toolTip())
        item.setData(MEASUREMENT_ROLE, None)
        item.setData(INTERVAL_ROLE, None)
        item.setSizeHint(QSize(0, 54))
        return item

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
                unavailable_reason = info.get("unavailable_reason", "") or ""
                item = self.signal_widgets[key]["checkbox"].item
                item.setData(NAME_ROLE, name)
                item.setData(COMMENT_ROLE, comment)
                item.setData(COLOR_ROLE, self._signal_color(key))
                item.setToolTip("\n".join(part for part in (comment, unavailable_reason) if part))
                self.search_index[key] = f"{info.get('name','')} {name} {comment} {unavailable_reason}".lower()
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
            self._refresh_selected_signal_list()
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

    def _selection_changed(self, replot=True, item=None):
        count = len(self.get_selected_signals())
        self.view_selected_btn.setText(f"已选信号 ({count})")
        self._refresh_selected_signal_list()
        if self._view_mode == "selected": self._apply_signal_filter()

    def _selected_signal_item_changed(self, item):
        key = item.data(KEY_ROLE)
        source_item = self.signal_widgets.get(key, {}).get("checkbox")
        if source_item is None:
            return
        blocker = QSignalBlocker(self.signal_list_widget)
        try:
            source_item.setChecked(item.checkState() == Qt.CheckState.Checked)
        finally:
            del blocker
        self._selection_changed(item=source_item.item)

    def _selected_signal_rows_moved(self, *_args):
        selected_order = [
            self.selected_signal_list_widget.item(row).data(KEY_ROLE)
            for row in range(self.selected_signal_list_widget.count())
        ]
        selected = set(selected_order)
        remaining = [
            self.signal_list_widget.item(row).data(KEY_ROLE)
            for row in range(self.signal_list_widget.count())
            if self.signal_list_widget.item(row).data(KEY_ROLE) not in selected
        ]
        ordered_keys = selected_order + remaining
        self._user_signal_order = {
            key: index for index, key in enumerate(ordered_keys)
        }
        self._reorder_signal_list(ordered_keys)

    def _reorder_signal_list(self, ordered_keys):
        widget = self.signal_list_widget
        widget.blockSignals(True)
        try:
            items = {}
            for row in range(widget.count() - 1, -1, -1):
                item = widget.takeItem(row)
                items[item.data(KEY_ROLE)] = item
            for key in ordered_keys:
                if key in items:
                    widget.addItem(items[key])
        finally:
            widget.blockSignals(False)
        self._apply_signal_filter()
        self._refresh_selected_signal_list()

    def _left_tab_changed(self, index):
        selected_index = self.left_tabs.indexOf(self.selected_signals_page)
        if index == selected_index:
            self._set_view_mode("selected")
            if self._current_cursor_time is not None:
                self._update_signal_list_values()
        elif index == self.left_tabs.indexOf(self.available_signals_page):
            self._set_view_mode("all")
        else:
            self._set_view_mode("all")

    def _signal_rows_moved(self, *_args):
        if self._view_mode != "selected": return
        self._user_signal_order = {self.signal_list_widget.item(i).data(KEY_ROLE): i for i in range(self.signal_list_widget.count())}
        self.reset_order_btn.setVisible(True)

    def _reset_signal_order(self):
        self._user_signal_order.clear(); self.refresh_signal_list_ui()

    def _update_signal_list_values(self):
        if self._current_cursor_time is None or not self._mouse_in_plot or self._view_mode != "selected":
            self._clear_selected_signal_values()
            return
        value_updates = []
        for i in range(self.signal_list_widget.count()):
            item = self.signal_list_widget.item(i)
            if item.checkState() != Qt.CheckState.Checked: continue
            key = item.data(KEY_ROLE)
            if key not in self._sig_value_cache:
                self._sig_value_cache[key] = self._get_signal(
                    self.signals.get(key, {})
                )
            signal = self._sig_value_cache[key]
            if (
                signal is None
                or len(signal.timestamps) == 0
                or len(signal.samples) == 0
            ):
                value_updates.append((item, _UNCHANGED_VALUE, None))
                continue
            idx = int(np.clip(np.searchsorted(signal.timestamps, self._current_cursor_time), 0, len(signal.timestamps)-1))
            if idx >= len(signal.samples):
                value_updates.append((item, _UNCHANGED_VALUE, None))
                continue
            value = signal.samples[idx]
            if hasattr(signal, "is_text_signal") and signal.is_text_signal and hasattr(signal, "raw_text_values"):
                shown = signal.raw_text_values[idx] if idx < len(signal.raw_text_values) else None
            else:
                try: shown = "NaN" if np.isnan(value) else f"{value:.3f}"
                except TypeError: shown = str(value)
            out_of_range = (
                self._current_cursor_time < signal.timestamps[0]
                or self._current_cursor_time > signal.timestamps[-1]
            )
            value_updates.append((item, shown, None if out_of_range else shown))
        blocker = QSignalBlocker(self.signal_list_widget)
        selected_items = {
            self.selected_signal_list_widget.item(row).data(KEY_ROLE): self.selected_signal_list_widget.item(row)
            for row in range(self.selected_signal_list_widget.count())
        }
        selected_blocker = QSignalBlocker(self.selected_signal_list_widget)
        try:
            for item, source_value, selected_value in value_updates:
                if source_value is not _UNCHANGED_VALUE:
                    item.setData(VALUE_ROLE, source_value)
                selected_item = selected_items.get(item.data(KEY_ROLE))
                if selected_item is not None:
                    selected_item.setData(VALUE_ROLE, selected_value)
        finally:
            del blocker
            del selected_blocker
        self.signal_list_widget.viewport().update()
        self.selected_signal_list_widget.viewport().update()

    def _clear_selected_signal_values(self):
        blocker = QSignalBlocker(self.selected_signal_list_widget)
        try:
            for row in range(self.selected_signal_list_widget.count()):
                self.selected_signal_list_widget.item(row).setData(VALUE_ROLE, None)
        finally:
            del blocker
        self.selected_signal_list_widget.viewport().update()

    def _clear_signal_values(self):
        if not hasattr(self, "signal_list_widget"): return
        blocker = QSignalBlocker(self.signal_list_widget)
        selected_blocker = QSignalBlocker(self.selected_signal_list_widget)
        try:
            for i in range(self.signal_list_widget.count()):
                self.signal_list_widget.item(i).setData(VALUE_ROLE, None)
            for i in range(self.selected_signal_list_widget.count()):
                self.selected_signal_list_widget.item(i).setData(VALUE_ROLE, None)
        finally:
            del blocker
            del selected_blocker
        self._sig_value_cache.clear()
        self.signal_list_widget.viewport().update()
        self.selected_signal_list_widget.viewport().update()
