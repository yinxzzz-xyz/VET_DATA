#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
VET_DATA_v26.08.25_CAN_MultiBus_UI - 支持容器帧解析
完整移植 main_py.py 的容器帧解析逻辑
"""
import subprocess
import sys
import os

import asammdf
import pyqtgraph as pg
import pandas as pd
import json
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QCheckBox, QPushButton, QScrollArea, QGroupBox,
                             QLabel, QFileDialog, QGridLayout, QLineEdit, QDialog,
                             QInputDialog, QMessageBox, QComboBox,
                             QFrame, QProgressBar,
                             QSplitter, QSizePolicy, QTreeWidget, QTreeWidgetItem,
                             QTextEdit, QHeaderView, QTabWidget, QScrollBar)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtCore import QPointF, QSize, QSignalBlocker, Qt, QTimer, pyqtSignal, QThread
import numpy as np
from PyQt6.QtWebEngineCore import QWebEngineSettings
from collections import defaultdict
import warnings
import logging
from io import StringIO
import re
from PyQt6.QtGui import QColor, QFontMetrics, QIcon, QPainter, QPen, QPixmap, QPolygonF
import time
import multiprocessing

from .signal_resolver import SignalResolutionError, SignalResolver
from .multi_y_axis import MultiYAxisManager
from .cutout_dialog import CutoutDialog, CutoutProgressDialog, load_last_mode
from .cutout_worker import CutoutWorker
from .data_cutout import (
    CutoutContext, CutoutJob, CutoutWindow, OutputFormat,
    OutputScope, SignalSeries, SourceKind, clean_signal_name,
    execute_cutout_job, source_kind,
)
from .theme import (
    DEFAULT_THEME, build_arxml_converter_stylesheet, build_can_config_stylesheet,
    build_signal_filter_stylesheet,
)
from .workers import (
    CanDetectionSnapshot,
    CanRawMessageCache,
    capture_can_file_identity,
    detect_can_bus_info,
    message_raw_channel,
    normalized_can_path,
)

# 抑制警告
warnings.filterwarnings("ignore")
logging.getLogger("cantools").setLevel(logging.ERROR)


def _quick_action_icon(kind, color):
    """Draw small, dependency-free icons for the always-visible shortcuts."""
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(color), 1.7))
    if kind == "open":
        painter.drawPolyline(QPolygonF([QPointF(2, 6), QPointF(8, 6), QPointF(10, 8), QPointF(18, 8), QPointF(16, 16), QPointF(2, 16), QPointF(2, 6)]))
        painter.drawLine(2, 8, 18, 8)
    elif kind == "plot":
        painter.drawLine(3, 16, 3, 4)
        painter.drawLine(3, 16, 18, 16)
        painter.drawPolyline(QPolygonF([QPointF(5, 13), QPointF(9, 10), QPointF(12, 12), QPointF(17, 5)]))
    elif kind == "formula":
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "ƒx")
    elif kind == "single":
        painter.drawLine(10, 3, 10, 17)
        painter.drawLine(2, 10, 8, 10)
        painter.drawLine(6, 8, 8, 10)
        painter.drawLine(6, 12, 8, 10)
        painter.drawLine(18, 10, 12, 10)
        painter.drawLine(14, 8, 12, 10)
        painter.drawLine(14, 12, 12, 10)
    elif kind == "dual":
        painter.drawLine(8, 3, 8, 17)
        painter.drawLine(12, 3, 12, 17)
        painter.drawLine(1, 10, 6, 10)
        painter.drawLine(4, 8, 6, 10)
        painter.drawLine(4, 12, 6, 10)
        painter.drawLine(19, 10, 14, 10)
        painter.drawLine(16, 8, 14, 10)
        painter.drawLine(16, 12, 14, 10)
    painter.end()
    return QIcon(pixmap)


class _AdaptiveRibbonWidget(QWidget):
    """Place shortcuts beside tabs when they fit, otherwise on a stable row below."""
    def __init__(self, tabs, shortcuts):
        super().__init__()
        self._tabs = tabs
        self._shortcuts = shortcuts
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self._row = QWidget(self)
        self._row_layout = QHBoxLayout(self._row)
        self._row_layout.setContentsMargins(0, 0, 0, 0)
        self._row_layout.setSpacing(8)
        self._row_layout.addWidget(tabs, 7)
        self._row_layout.addWidget(shortcuts, 3)
        self._layout.addWidget(self._row)
        self._wrapped = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        needed = max(1, self._shortcuts.minimumSizeHint().width())
        wrap = self.width() < needed / 0.30 + 8
        if wrap == self._wrapped:
            return
        if wrap:
            self._row_layout.removeWidget(self._shortcuts)
            self._layout.addWidget(self._shortcuts)
        else:
            self._layout.removeWidget(self._shortcuts)
            self._row_layout.addWidget(self._shortcuts, 3)
        self._wrapped = wrap

# CAN 相关库检查
try:
    import cantools
    import can
except ImportError:
    print(
        "警告: 未检测到 cantools 或 python-can 库，CAN 报文解析功能将不可用。请执行 'pip install cantools python-can' 安装。")
    can = None
    cantools = None


def decode_signal_motorola(data, start_bit, length, is_little_endian, header_total_bits):
    """
    按照 Vector/CANoe DBC 的 StartBit 规则解析 PDU 信号。
    从 main_py.py 完整移植

    对 AUTOSAR PDU Container：
        DBC StartBit = PDU payload 内 StartBit + Header总长度
        所以实际传入的 data 是不含 Header 的 PDU payload，需要：
            pdu_start_bit = dbc_start_bit - header_total_bits

    Motorola DBC bit traversal：
        bit 7 -> 6 -> ... -> 0 -> 下一字节 bit 7 -> ...
    """
    try:
        if not data or length <= 0:
            return None

        start_bit = int(start_bit)
        length = int(length)
        header_total_bits = int(header_total_bits or 0)

        # DBC StartBit 是相对于完整 Container/PDU 布局的
        pdu_start_bit = start_bit - header_total_bits
        if pdu_start_bit < 0:
            return None

        data_bits = len(data) * 8

        if is_little_endian:
            # Intel / Little Endian：StartBit 是 LSB 位置
            if pdu_start_bit + length > data_bits:
                return None
            raw = int.from_bytes(data, byteorder='little', signed=False)
            raw >>= pdu_start_bit
            return raw & ((1 << length) - 1)

        # ============================================================
        # Motorola / Big Endian (@0)
        # ============================================================
        bit_pos = pdu_start_bit
        value = 0

        for _ in range(length):
            byte_index = bit_pos // 8
            bit_index = bit_pos % 8

            if byte_index < 0 or byte_index >= len(data):
                return None

            value = (value << 1) | ((data[byte_index] >> bit_index) & 0x01)

            # Motorola saw-tooth traversal
            if bit_index == 0:
                bit_pos += 15  # 当前 byte bit0 -> 下一 byte bit7
            else:
                bit_pos -= 1

        return value

    except Exception:
        return None


class LimitedViewBox(pg.ViewBox):
    def wheelEvent(self, ev, axis=0):
        super().wheelEvent(ev, axis)
        x_min_limit = self.state.get('limits', {}).get('xMin')
        x_max_limit = self.state.get('limits', {}).get('xMax')
        if x_min_limit is not None and x_max_limit is not None:
            current_x_range = self.viewRange()[0]
            x_min_current = current_x_range[0]
            x_max_current = current_x_range[1]
            if x_min_current < x_min_limit or x_max_current > x_max_limit:
                self.setRange(
                    xRange=(max(x_min_current, x_min_limit), min(x_max_current, x_max_limit)),
                    padding=0
                )


class SignalFilterDialog(QDialog):
    """DBC信号筛选对话框 - 点击行勾选，紧凑显示"""

    def __init__(self, db, bus_name, existing_ids=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.bus_name = bus_name
        self.existing_ids = set(existing_ids) if existing_ids else set()

        # 打印调试信息
        print(f"\n🔍 SignalFilterDialog 初始化:")
        print(f"  - existing_ids 数量: {len(self.existing_ids)}")
        if self.existing_ids:
            print(f"  - existing_ids 前10个: {list(self.existing_ids)[:10]}")
        print(f"  - DBC 消息数量: {len(db.messages)}")
        print()

        self._selected_state = {}
        self.frame_items = {}
        self.signal_items = []
        self.item_keys = []
        self.frame_signal_count = {}
        self._frame_signal_keys = {}
        self._signal_item_by_key = {}
        self._frame_id_by_item = {}
        self._signal_key_by_item = {}
        self._search_index = {}
        self._search_pending_text = ""
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(100)
        self._search_timer.timeout.connect(self._apply_signal_filter)
        self._updating = False
        self.search_box = QLineEdit()
        self.select_all_btn = QPushButton()
        self.deselect_all_btn = QPushButton()
        self.selection_count_label = QLabel()
        self.tree = QTreeWidget()
        self.select_btn = QPushButton()
        self.cancel_btn = QPushButton()

        self._init_selected_state()
        self.init_ui()

    def _init_selected_state(self):
        """初始化所有信号的选中状态为False"""
        for msg in self.db.messages:
            for sig in msg.signals:
                key = f"{msg.frame_id}_{sig.name}"
                self._selected_state[key] = False

    def init_ui(self):
        self.setWindowTitle(f"筛选信号 - {self.bus_name}")
        self.resize(800, 650)
        self.setProperty("uiDialog", "signalFilter")
        self.setStyleSheet(build_signal_filter_stylesheet(DEFAULT_THEME))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            DEFAULT_THEME.spacing.medium, DEFAULT_THEME.spacing.medium,
            DEFAULT_THEME.spacing.medium, DEFAULT_THEME.spacing.medium,
        )
        layout.setSpacing(DEFAULT_THEME.spacing.xsmall)

        # 统计信息 - 显示存在的ID数量
        total_msgs = len(self.db.messages)
        total_sigs = self._count_total_signals()
        existing_count = len(self.existing_ids)

        info_text = f"数据库包含 {total_msgs} 个消息, 共 {total_sigs} 个信号"
        if existing_count > 0:
            info_text += f" | 日志中存在 {existing_count} 个消息 ID"
        self.info_label = QLabel(info_text)
        self.info_label.setProperty("uiTextRole", "summary")
        layout.addWidget(self.info_label)

        # 搜索框
        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("🔍 搜索:"))
        self.search_box.setPlaceholderText("搜索信号名、CAN ID、注释...")
        self.search_box.textChanged.connect(self.filter_signals)
        search_layout.addWidget(self.search_box)
        layout.addLayout(search_layout)

        # 按钮行
        btn_layout = QHBoxLayout()
        self.select_all_btn.setText("✅ 全选 (当前筛选)")
        self.deselect_all_btn.setText("❌ 全部取消")
        self.select_all_btn.setProperty("uiRole", "secondary")
        self.deselect_all_btn.setProperty("uiRole", "secondary")
        self.select_all_btn.clicked.connect(self.select_all)
        self.deselect_all_btn.clicked.connect(self.deselect_all)

        self.selection_count_label.setText("已选: 0 / 0")
        self.selection_count_label.setProperty("uiTextRole", "selection")

        btn_layout.addWidget(self.select_all_btn)
        btn_layout.addWidget(self.deselect_all_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self.selection_count_label)
        layout.addLayout(btn_layout)

        # 信号列表 - 使用QTreeWidget
        self.tree.setHeaderLabels(["信号名", "ID", "帧名", "端序", "位数", "注释"])
        self.tree.setIndentation(15)
        self.tree.setAlternatingRowColors(True)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(2, 180)

        self.tree.itemClicked.connect(self.on_item_clicked)
        self.tree.itemChanged.connect(self.on_item_changed)
        layout.addWidget(self.tree)

        # 底部按钮
        btn_layout2 = QHBoxLayout()
        self.select_btn.setText("✅ 使用选中的信号")
        self.cancel_btn.setText("取消")
        self.select_btn.setProperty("uiRole", "primary")
        self.cancel_btn.setProperty("uiRole", "secondary")
        btn_layout2.addWidget(self.select_btn)
        btn_layout2.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout2)

        self._populate_tree()
        self.select_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)

    def _count_total_signals(self):
        count = 0
        for msg in self.db.messages:
            count += len(msg.signals)
        return count

    def _populate_tree(self, filter_text=""):
        """Build the tree once; later calls only apply the cached filter."""
        if self.frame_items:
            self._apply_filter_text(filter_text)
            return
        self.tree.clear()
        self.signal_items = []
        self.item_keys = []
        self.frame_signal_count = {}
        self._frame_signal_keys = {}
        self._signal_item_by_key = {}
        self._frame_id_by_item = {}
        self._signal_key_by_item = {}
        self._search_index = {}
        filter_text = filter_text.strip().lower()

        # 将 existing_ids 统一转换为整数集合
        existing_ids_set = set()
        for eid in self.existing_ids:
            try:
                existing_ids_set.add(int(eid))
            except (ValueError, TypeError):
                existing_ids_set.add(eid)

        print(f"🔍 _populate_tree - existing_ids 数量: {len(self.existing_ids)}")
        print(f"🔍 _populate_tree - existing_ids_set 数量: {len(existing_ids_set)}")
        if existing_ids_set:
            print(f"🔍 _populate_tree - existing_ids_set 前10个: {list(existing_ids_set)[:10]}")

        for msg in self.db.messages:
            frame_id = msg.frame_id
            frame_id_hex = f"0x{frame_id:X}"
            frame_name = msg.name

            # 检查该帧ID是否在日志中存在
            frame_exists = frame_id in existing_ids_set
            if frame_exists:
                print(f"✅ 匹配到帧: 0x{frame_id:X} - {frame_name}")

            for sig in msg.signals:
                sig_name = sig.name
                sig_comment = getattr(sig, 'comment', '') or ''
                search_text = f"{sig_name} {frame_id_hex} {frame_name} {sig_comment}".lower()
                self._search_index[f"{frame_id}_{sig_name}"] = search_text

            # 记录该帧的信号总数
            self.frame_signal_count[frame_id] = len(msg.signals)

            frame_item = QTreeWidgetItem(self.tree)
            frame_item.setFlags(frame_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)

            # 设置帧名（第0列）
            frame_item.setText(0, f"📦 {frame_name}")

            if frame_exists:
                frame_item.setToolTip(0, f"该帧ID (0x{frame_id:X}) 在日志中存在 ✓")
            else:
                frame_item.setToolTip(0, f"该帧ID (0x{frame_id:X}) 在日志中不存在")
            frame_item.setToolTip(2, frame_name)

            frame_item.setText(1, frame_id_hex)

            # 检查该帧下所有信号的选中状态
            frame_checked_count = 0
            frame_keys = []
            for sig in msg.signals:
                key = f"{frame_id}_{sig.name}"
                frame_keys.append(key)
                if self._selected_state.get(key, False):
                    frame_checked_count += 1

            total_sigs = len(msg.signals)
            # "已选 X 个" 用绿色显示
            if frame_checked_count > 0:
                frame_item.setText(2, f"({total_sigs} 个信号，已选 {frame_checked_count} 个)")
            else:
                frame_item.setText(2, f"({total_sigs} 个信号)")

            font = frame_item.font(0)
            font.setBold(True)
            frame_item.setFont(0, font)

            # 设置帧的勾选状态
            if frame_checked_count == total_sigs and total_sigs > 0:
                frame_item.setCheckState(0, Qt.CheckState.Checked)
            elif frame_checked_count > 0:
                frame_item.setCheckState(0, Qt.CheckState.PartiallyChecked)
            else:
                frame_item.setCheckState(0, Qt.CheckState.Unchecked)

            self.frame_items[frame_id] = frame_item
            self._frame_id_by_item[id(frame_item)] = frame_id
            self._frame_signal_keys[frame_id] = frame_keys

            for sig in msg.signals:
                sig_name = sig.name
                sig_comment = getattr(sig, 'comment', '') or ''
                is_little = getattr(sig, 'is_little_endian', False)
                endian = "Intel" if is_little else "Motorola"
                key = f"{frame_id}_{sig_name}"

                sig_item = QTreeWidgetItem(frame_item)
                sig_item.setFlags(sig_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                sig_item.setText(0, sig_name)
                sig_item.setText(1, frame_id_hex)
                sig_item.setText(2, frame_name)
                sig_item.setText(3, endian)
                sig_item.setText(4, str(sig.length))
                sig_item.setText(5, sig_comment[:50] + ('...' if len(sig_comment) > 50 else ''))
                sig_item.setToolTip(0, sig_name)
                sig_item.setToolTip(2, frame_name)
                sig_item.setToolTip(5, sig_comment)

                self.signal_items.append(sig_item)
                self.item_keys.append(key)
                self._signal_item_by_key[key] = sig_item
                self._signal_key_by_item[id(sig_item)] = key

                if self._selected_state.get(key, False):
                    sig_item.setCheckState(0, Qt.CheckState.Checked)
                else:
                    sig_item.setCheckState(0, Qt.CheckState.Unchecked)

        self.update_selection_count()
        self._apply_filter_text(filter_text)

    def _apply_filter_text(self, text):
        """Filter cached rows without rebuilding the QTreeWidget hierarchy."""
        query = text.strip().lower()
        self.tree.setUpdatesEnabled(False)
        try:
            for frame_id, frame_item in self.frame_items.items():
                visible_count = 0
                for key in self._frame_signal_keys.get(frame_id, ()):
                    item = self._signal_item_by_key[key]
                    visible = not query or query in self._search_index[key]
                    item.setHidden(not visible)
                    visible_count += visible
                frame_item.setHidden(visible_count == 0)
        finally:
            self.tree.setUpdatesEnabled(True)

    def _apply_signal_filter(self):
        self._apply_filter_text(self._search_pending_text)

    def on_item_clicked(self, item, column):
        """点击行：信号项切换状态，帧项切换展开/折叠"""
        # 帧项：只切换展开/折叠，不干扰复选框
        if item.parent() is None:
            # 只有点击非复选框列时才切换展开/折叠
            if column != 0:
                if item.isExpanded():
                    item.setExpanded(False)
                else:
                    item.setExpanded(True)
            return

        # 信号项：点击非复选框列时切换状态
        if column != 0:
            try:
                key = self._signal_key_by_item.get(id(item))
                if key is None:
                    return
                current = self._selected_state.get(key, False)
                new_state = not current
                self._selected_state[key] = new_state
                item.setCheckState(0, Qt.CheckState.Checked if new_state else Qt.CheckState.Unchecked)

                parent = item.parent()
                if parent:
                    self.update_frame_display(parent)
                self.update_selection_count()
            except ValueError:
                pass

    def on_item_changed(self, item, column):
        """复选框状态变化时触发"""
        if column != 0:
            return

        # 防止递归
        if hasattr(self, '_updating') and self._updating:
            return

        self._updating = True

        try:
            # 帧级别的勾选变化
            if item.parent() is None:
                check_state = item.checkState(0)
                frame_id = self._frame_id_by_item.get(id(item))

                if frame_id is not None:
                    # 更新该帧下所有信号的选中状态
                    for key in self._frame_signal_keys.get(frame_id, ()):
                        sig_item = self._signal_item_by_key[key]
                        if sig_item.isHidden():
                            continue
                        if check_state == Qt.CheckState.Checked:
                            self._selected_state[key] = True
                            sig_item.setCheckState(0, Qt.CheckState.Checked)
                        else:
                            self._selected_state[key] = False
                            sig_item.setCheckState(0, Qt.CheckState.Unchecked)

                    # 更新帧显示（不更新复选框，避免递归）
                    self.update_frame_display(item, update_checkbox=False)
                    self.update_selection_count()
                return

            # 信号级别的变化
            try:
                key = self._signal_key_by_item.get(id(item))
                if key is None:
                    return
                check_state = item.checkState(0)
                self._selected_state[key] = (check_state == Qt.CheckState.Checked)

                parent = item.parent()
                if parent:
                    self.update_frame_display(parent, update_checkbox=True)
                self.update_selection_count()
            except ValueError:
                pass

        finally:
            self._updating = False

    def update_frame_display(self, frame_item, update_checkbox=True):
        """更新帧的显示文本和勾选状态"""
        if frame_item is None:
            return

        frame_id = self._frame_id_by_item.get(id(frame_item))

        if frame_id is None:
            return

        total_count = self.frame_signal_count.get(frame_id, 0)

        checked_count = 0
        for key, is_selected in self._selected_state.items():
            if key.startswith(f"{frame_id}_") and is_selected:
                checked_count += 1

        if checked_count > 0:
            frame_item.setText(2, f"({total_count} 个信号，已选 {checked_count} 个)")
        else:
            frame_item.setText(2, f"({total_count} 个信号)")

        # 更新勾选状态（如果需要）
        if update_checkbox:
            if checked_count == total_count and total_count > 0:
                frame_item.setCheckState(0, Qt.CheckState.Checked)
            elif checked_count > 0:
                frame_item.setCheckState(0, Qt.CheckState.PartiallyChecked)
            else:
                frame_item.setCheckState(0, Qt.CheckState.Unchecked)

    def filter_signals(self, text):
        self._search_pending_text = text
        self._search_timer.start()

    def select_all(self):
        """全选当前筛选显示的所有信号"""
        for key in self._selected_state:
            self._selected_state[key] = True
        self._refresh_frame_selection_states()

    def deselect_all(self):
        """取消全选所有信号"""
        for key in self._selected_state:
            self._selected_state[key] = False
        self._refresh_frame_selection_states()

    def _refresh_frame_selection_states(self):
        self.tree.setUpdatesEnabled(False)
        try:
            for frame_item in self.frame_items.values():
                self.update_frame_display(frame_item)
        finally:
            self.tree.setUpdatesEnabled(True)
        self.update_selection_count()

    def update_selection_count(self):
        total = len(self._selected_state)
        selected = sum(1 for v in self._selected_state.values() if v)
        self.selection_count_label.setText(f"已选: {selected} / {total}")
        self.selection_count_label.setProperty(
            "summaryState", "selected" if selected else "empty"
        )
        self.selection_count_label.style().unpolish(self.selection_count_label)
        self.selection_count_label.style().polish(self.selection_count_label)

    def get_selected_signals_by_frame(self):
        """按帧分组返回选中的信号"""
        result = {}
        for key, is_selected in self._selected_state.items():
            if is_selected:
                parts = key.split('_', 1)
                if len(parts) == 2:
                    try:
                        frame_id = int(parts[0])
                        sig_name = parts[1]
                        if frame_id not in result:
                            result[frame_id] = []
                        result[frame_id].append(sig_name)
                    except ValueError:
                        continue
        return result


class MathChannelDialog(QDialog):
    def __init__(self, available_signals, parent=None):
        super().__init__(parent)
        self.available_signals = available_signals
        self.combo_a = QComboBox()
        self.combo_op = QComboBox()
        self.combo_b = QComboBox()
        self.combo_zero_policy = QComboBox()
        self.name_edit = QLineEdit()
        self.btn_ok = QPushButton()
        self.btn_cancel = QPushButton()
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("创建自定义计算通道")
        self.resize(450, 250)
        layout = QVBoxLayout(self)
        grid = QGridLayout()

        grid.addWidget(QLabel("信号 A (被除数/被减数):"), 0, 0)
        grid.addWidget(self.combo_a, 0, 1)

        grid.addWidget(QLabel("运算符:"), 1, 0)
        self.combo_op.addItems(["+ (加)", "- (减)", "* (乘)", "/ (除)"])
        grid.addWidget(self.combo_op, 1, 1)

        grid.addWidget(QLabel("信号 B (除数/减数):"), 2, 0)
        grid.addWidget(self.combo_b, 2, 1)

        grid.addWidget(QLabel("除零/无效值保护策略:"), 3, 0)
        self.combo_zero_policy.addItem("分母为0时结果赋 0.0", "zero")
        self.combo_zero_policy.addItem("分母为0时结果赋 NaN (曲线断开)", "nan")
        grid.addWidget(self.combo_zero_policy, 3, 1)

        grid.addWidget(QLabel("新信号名称:"), 4, 0)
        self.name_edit.setPlaceholderText("例如: Math_Signal_Result")
        grid.addWidget(self.name_edit, 4, 1)

        layout.addLayout(grid)

        sorted_keys = sorted(self.available_signals.keys(), key=lambda k: self.available_signals[k]['display_name'])
        for key in sorted_keys:
            display_name = self.available_signals[key]['display_name']
            self.combo_a.addItem(display_name, key)
            self.combo_b.addItem(display_name, key)

        self.combo_op.currentIndexChanged.connect(self.toggle_policy_visibility)
        self.toggle_policy_visibility()

        btn_layout = QHBoxLayout()
        self.btn_ok.setText("生成通道")
        self.btn_cancel.setText("取消")
        btn_layout.addWidget(self.btn_ok)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)

        self.btn_ok.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)

    def toggle_policy_visibility(self):
        is_div = "/" in self.combo_op.currentText()
        self.combo_zero_policy.setEnabled(is_div)

    def get_config(self):
        return {
            "key_a": self.combo_a.currentData(),
            "key_b": self.combo_b.currentData(),
            "op": self.combo_op.currentText()[0],
            "policy": self.combo_zero_policy.currentData(),
            "new_name": self.name_edit.text().strip()
        }


def _prepare_dbc_signal_names(databases):
    """Avoid canmatrix's 32-character DBC name truncation collisions."""
    from collections import defaultdict

    for database in databases.values():
        for frame in database.frames:
            names_by_export_name = defaultdict(dict)
            for signal in frame.signals:
                names_by_export_name[signal.name[:32]][signal.name] = signal

            collisions = [
                signals
                for signals in names_by_export_name.values()
                if len(signals) > 1
            ]
            if not collisions:
                continue

            if "SystemSignalLongSymbol" not in database.signal_defines:
                database.add_signal_defines("SystemSignalLongSymbol", "STRING")

            used_names = {signal.name[:32] for signal in frame.signals}
            alias_index = 1
            for signals in collisions:
                for signal in signals.values():
                    original_name = signal.name
                    while True:
                        alias = f"VET_SIG_{alias_index:06d}"
                        alias_index += 1
                        if alias not in used_names:
                            break
                    used_names.add(alias)
                    signal.add_attribute("SystemSignalLongSymbol", original_name)
                    signal.name = alias


def _guard_canmatrix_arxml_parent_traversal(canmatrix):
    """Keep canmatrix ARXML lookups safe for detached or unresolved elements."""
    arxml = canmatrix.formats.arxml
    earxml = arxml.Earxml
    if getattr(earxml, "_vet_data_parent_guard", False):
        return

    def get_short_name_path_of_element(self, xml_element):
        path = []
        while xml_element is not None and xml_element is not self.root:
            short_name = self.get_short_name(xml_element)
            if short_name:
                path.append(short_name)
            xml_element = xml_element.getparent()
        return "/".join(reversed(path))

    def get_referencable_parent(self, xml_element):
        while xml_element is not None and xml_element is not self.root:
            if self.get_short_name(xml_element):
                return xml_element
            xml_element = xml_element.getparent()
        return xml_element

    def find_references_of_type(self, element, xml_tag, referencable_parent=False):
        current_ar_path = self.get_short_name_path_of_element(element)
        references = []
        for reference in self.path_cache.get(current_ar_path, ()):
            parent = self.get_referencable_parent(reference)
            if parent is not None and parent.tag == self.ns + xml_tag:
                references.append(reference)
        if referencable_parent:
            references = [self.get_referencable_parent(item) for item in references]
            references = [item for item in references if item is not None]
        return references

    earxml.get_short_name_path_of_element = get_short_name_path_of_element
    earxml.get_referencable_parent = get_referencable_parent
    earxml.find_references_of_type = find_references_of_type
    earxml._vet_data_parent_guard = True


def _convert_arxml_models_to_dbc(canmatrix, arxml_file, dbc_file):
    """Load, prepare and split-export ARXML databases like canmatrix.convert."""
    _guard_canmatrix_arxml_parent_traversal(canmatrix)
    databases = canmatrix.formats.loadp(arxml_file)
    if not databases:
        raise RuntimeError("加载 ARXML 返回空数据库")

    # Match canmatrix.convert.convert()'s default PDU-container handling.
    for database in databases.values():
        for frame in list(database.frames):
            if frame.is_pdu_container:
                converted = canmatrix.convert.convert_pdu_container_to_multiplexed(frame)
                database.del_frame(frame)
                database.add_frame(converted)

    _prepare_dbc_signal_names(databases)
    canmatrix.formats.dumpp(
        databases,
        dbc_file,
        export_type="dbc",
        dbcExportEncoding="ascii",
        ignoreEncodingErrors="ignore",
    )


def _convert_arxml_to_dbc(arxml_path, dbc_path, log_callback):
    """Run the existing canmatrix conversion without accessing Qt widgets."""
    arxml_file = os.path.abspath(arxml_path)
    dbc_file = os.path.abspath(dbc_path)

    log_callback("=" * 50)
    log_callback("🔧 ARXML → DBC 转换开始")
    log_callback(f"输入文件: {arxml_file}")
    log_callback(f"输出文件: {dbc_file}")

    output_dir = os.path.dirname(dbc_file)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    if os.path.exists(dbc_file):
        try:
            os.remove(dbc_file)
            log_callback("🗑️ 已删除旧的 DBC 文件")
        except Exception as exc:
            log_callback(f"⚠️ 删除旧文件失败: {exc}")

    try:
        import canmatrix
        import canmatrix.convert
        import canmatrix.formats
        import canmatrix.formats.arxml
        log_callback(f"✅ canmatrix 版本: {getattr(canmatrix, '__version__', 'unknown')}")
    except ImportError as exc:
        log_callback(f"❌ 导入 canmatrix 失败: {exc}")
        raise RuntimeError("请确保已安装 canmatrix: pip install canmatrix") from exc

    if not os.path.exists(arxml_file):
        raise FileNotFoundError(f"ARXML 文件不存在: {arxml_file}")
    if os.path.getsize(arxml_file) == 0:
        raise ValueError(f"ARXML 文件为空: {arxml_file}")

    log_callback("🔄 正在转换 (使用 canmatrix ARXML 加载和 DBC 导出)...")
    try:
        _convert_arxml_models_to_dbc(canmatrix, arxml_file, dbc_file)
        log_callback("✅ canmatrix ARXML 加载和 DBC 导出完成")
    except Exception as exc:
        log_callback(f"⚠️ 转换调用失败: {exc}")
        raise RuntimeError(f"转换失败: {exc}") from exc

    def check_output(filepath):
        if not os.path.exists(filepath):
            return False, "文件不存在"
        if os.path.getsize(filepath) == 0:
            return False, "文件为空"
        try:
            with open(filepath, 'r', encoding='utf-8') as stream:
                if 'BO_' not in stream.read():
                    return False, "文件中没有找到 BO_ (消息定义)"
            return True, ""
        except Exception as exc:
            return False, f"读取失败: {exc}"

    success, message = check_output(dbc_file)
    if success:
        file_size = os.path.getsize(dbc_file)
        log_callback(f"✅ 转换成功！文件大小: {file_size} 字节")
        try:
            with open(dbc_file, 'r', encoding='utf-8') as stream:
                message_count = stream.read().count('BO_')
            log_callback(f"📊 包含 {message_count} 个消息定义")
        except Exception:
            pass
        return

    candidates = []
    try:
        for filename in os.listdir(output_dir):
            if filename.lower().endswith('.dbc'):
                candidate = os.path.join(output_dir, filename)
                if os.path.getsize(candidate) > 0:
                    candidates.append(filename)
    except Exception:
        pass
    if candidates:
        log_callback(f"✅ 在输出目录找到 DBC 文件: {', '.join(candidates)}")
        return
    raise RuntimeError(f"转换完成但未生成有效的 DBC 文件: {message}")


class ARXMLConversionWorker(QThread):
    """Execute canmatrix work off the GUI thread and report through signals."""

    log_emitted = pyqtSignal(str)
    succeeded = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, arxml_path, dbc_path, parent=None):
        super().__init__(parent)
        self.arxml_path = str(arxml_path)
        self.dbc_path = str(dbc_path)

    def run(self):
        try:
            _convert_arxml_to_dbc(
                self.arxml_path, self.dbc_path, self.log_emitted.emit
            )
        except Exception as exc:
            import traceback
            self.log_emitted.emit("❌ ARXML → DBC 转换异常:")
            self.log_emitted.emit(traceback.format_exc())
            self.failed.emit(str(exc))
            return
        self.succeeded.emit()


class ARXMLConverterDialog(QDialog):
    """ARXML 转 DBC 工具对话框

    注意：PyInstaller --onefile 模式下不能再通过 sys.executable -m
    canmatrix.cli.convert 启动子进程，因为 sys.executable 此时指向当前 EXE，
    会再次启动自己，导致转换一直停留在“转换中”。因此这里直接调用
    canmatrix.convert.convert()。
    """

    conversion_success = pyqtSignal()
    conversion_error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.arxml_path = ""
        self.dbc_path = ""
        self.process = None
        self.is_running = False
        self._conversion_worker = None
        self._close_after_conversion = False
        self.arxml_path_edit = QLineEdit()
        self.arxml_select_btn = QPushButton()
        self.dbc_path_edit = QLineEdit()
        self.convert_btn = QPushButton()
        self.status_label = QLabel()
        self.progress_bar = QProgressBar()
        self.log_text = QTextEdit()
        self.clear_log_btn = QPushButton()
        self.open_folder_btn = QPushButton()
        self.close_btn = QPushButton()
        self.setWindowTitle("ARXML to DBC Converter")
        self.resize(600, 450)
        self.init_ui()
        # 从后台 Python 线程安全地回到 Qt 主线程更新界面
        self.conversion_success.connect(self._on_success)
        self.conversion_error.connect(self._on_error)

    def init_ui(self):
        self.setProperty("uiDialog", "arxmlConverter")
        self.setMinimumSize(640, 520)
        self.resize(720, 620)
        self.setStyleSheet(build_arxml_converter_stylesheet(DEFAULT_THEME))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            DEFAULT_THEME.spacing.medium, DEFAULT_THEME.spacing.medium,
            DEFAULT_THEME.spacing.medium, DEFAULT_THEME.spacing.medium,
        )
        layout.setSpacing(DEFAULT_THEME.spacing.small)

        # 标题
        self.title_label = QLabel("🔧 ARXML 转 DBC 工具")
        self.title_label.setProperty("uiArxmlTitle", "true")
        layout.addWidget(self.title_label)

        # 说明
        self.desc_label = QLabel("DBC 文件将自动保存到 ARXML 文件所在目录的 dbc_output 文件夹中")
        self.desc_label.setProperty("uiTextRole", "secondary")
        self.desc_label.setWordWrap(True)
        layout.addWidget(self.desc_label)

        # ===== 输入与只读输出 =====
        self.path_group = QGroupBox("输入与输出")
        self.path_group.setObjectName("arxmlPathSection")
        self.path_group.setProperty("uiArxmlSection", "true")
        path_layout = QGridLayout(self.path_group)
        path_layout.setHorizontalSpacing(DEFAULT_THEME.spacing.small)
        path_layout.setVerticalSpacing(DEFAULT_THEME.spacing.xsmall)
        self.arxml_path_edit.setPlaceholderText("请选择 ARXML 文件...")
        self.arxml_path_edit.setReadOnly(True)
        self.arxml_path_edit.setProperty("uiReadonly", "true")
        self.arxml_path_edit.textChanged.connect(self.arxml_path_edit.setToolTip)
        self.arxml_select_btn.setText("浏览...")
        self.arxml_select_btn.setProperty("uiRole", "secondary")
        self.arxml_select_btn.clicked.connect(self.select_arxml)
        self.dbc_path_edit.setPlaceholderText("选择 ARXML 文件后自动生成...")
        self.dbc_path_edit.setReadOnly(True)
        self.dbc_path_edit.setProperty("uiReadonly", "true")
        self.dbc_path_edit.textChanged.connect(self.dbc_path_edit.setToolTip)
        path_layout.addWidget(QLabel("ARXML 输入"), 0, 0)
        path_layout.addWidget(self.arxml_path_edit, 0, 1)
        path_layout.addWidget(self.arxml_select_btn, 0, 2)
        path_layout.addWidget(QLabel("DBC 输出"), 1, 0)
        path_layout.addWidget(self.dbc_path_edit, 1, 1)
        path_layout.setColumnStretch(1, 1)
        layout.addWidget(self.path_group)

        # ===== 转换与状态 =====
        self.conversion_group = QGroupBox("转换")
        self.conversion_group.setObjectName("arxmlConversionSection")
        self.conversion_group.setProperty("uiArxmlSection", "true")
        conversion_layout = QVBoxLayout(self.conversion_group)
        conversion_layout.setSpacing(DEFAULT_THEME.spacing.xsmall)
        conversion_actions = QHBoxLayout()
        self.convert_btn.setText("开始转换")
        self.convert_btn.setProperty("uiRole", "primary")
        self.convert_btn.clicked.connect(self.start_conversion)
        self.convert_btn.setEnabled(False)
        conversion_actions.addWidget(self.convert_btn)
        conversion_actions.addSpacing(DEFAULT_THEME.spacing.medium)
        conversion_actions.addWidget(QLabel("状态:"))
        self.status_label.setProperty("uiArxmlStatus", "true")
        conversion_actions.addWidget(self.status_label)
        conversion_actions.addStretch()
        conversion_layout.addLayout(conversion_actions)
        self._set_conversion_status("就绪", "idle")

        # 进度条 - 无限模式（与附件一致）
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        conversion_layout.addWidget(self.progress_bar)
        layout.addWidget(self.conversion_group)

        # 日志
        self.log_group = QGroupBox("转换日志")
        self.log_group.setObjectName("arxmlLogSection")
        self.log_group.setProperty("uiArxmlSection", "true")
        log_layout = QVBoxLayout(self.log_group)
        self.log_text.setReadOnly(True)
        self.log_text.setProperty("uiArxmlLog", "true")
        self.log_text.setMinimumHeight(160)
        log_layout.addWidget(self.log_text)
        layout.addWidget(self.log_group, 1)

        # 底部按钮
        btn_layout = QHBoxLayout()
        self.clear_log_btn.setText("清空日志")
        self.clear_log_btn.setProperty("uiRole", "secondary")
        self.clear_log_btn.clicked.connect(self.clear_log)
        btn_layout.addWidget(self.clear_log_btn)
        btn_layout.addStretch()
        self.open_folder_btn.setText("📁 打开输出文件夹")
        self.open_folder_btn.setProperty("uiRole", "secondary")
        self.open_folder_btn.setEnabled(False)
        self.open_folder_btn.clicked.connect(self.open_output_folder)
        btn_layout.addWidget(self.open_folder_btn)
        self.close_btn.setText("关闭")
        self.close_btn.setProperty("uiRole", "secondary")
        self.close_btn.clicked.connect(self.close)
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)

    def _set_conversion_status(self, text, kind):
        self.status_label.setText(text)
        self.status_label.setProperty("statusKind", kind)
        _refresh_dynamic_style(self.status_label)

    def select_arxml(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择 ARXML 文件", "",
            "ARXML files (*.arxml);;All files (*.*)"
        )
        if file_path:
            self.arxml_path = os.path.abspath(file_path)
            self.arxml_path_edit.setText(self.arxml_path)

            # 自动生成 DBC 输出路径
            arxml_dir = os.path.dirname(self.arxml_path)
            base_name = os.path.splitext(os.path.basename(self.arxml_path))[0]
            output_dir = os.path.join(arxml_dir, "dbc_output")
            if not os.path.exists(output_dir):
                try:
                    os.makedirs(output_dir)
                except OSError:
                    pass

            self.dbc_path = os.path.join(output_dir, base_name + ".dbc")
            self.dbc_path_edit.setText(self.dbc_path)

            self.update_convert_button()

    def open_output_folder(self):
        if self.dbc_path:
            folder = os.path.dirname(self.dbc_path)
            if os.path.exists(folder):
                if sys.platform == 'win32':
                    os.startfile(folder)
                elif sys.platform == 'darwin':
                    subprocess.Popen(['open', folder])
                else:
                    subprocess.Popen(['xdg-open', folder])

    def update_convert_button(self):
        self.convert_btn.setEnabled(bool(self.arxml_path and self.dbc_path and not self.is_running))

    def clear_log(self):
        self.log_text.clear()

    def log(self, message):
        self.log_text.append(f"{message}")
        cursor = self.log_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.log_text.setTextCursor(cursor)

    def start_conversion(self):
        if not self.arxml_path or not os.path.exists(self.arxml_path):
            QMessageBox.warning(self, "错误", "请先选择 ARXML 文件！")
            return
        if not self.dbc_path:
            QMessageBox.warning(self, "错误", "DBC 输出路径无效！")
            return

        # 确保输出目录存在
        output_dir = os.path.dirname(self.dbc_path)
        if not os.path.exists(output_dir):
            try:
                os.makedirs(output_dir)
            except Exception as e:
                QMessageBox.warning(self, "错误", f"无法创建输出目录：{e}")
                return

        self.is_running = True
        self.convert_btn.setEnabled(False)
        self.open_folder_btn.setEnabled(False)
        self._set_conversion_status("转换中...", "running")
        self.progress_bar.setVisible(True)

        # 删除可能存在的旧输出文件
        if os.path.exists(self.dbc_path):
            try:
                os.remove(self.dbc_path)
            except OSError:
                pass

        self.log("=" * 50)
        self.log(f"开始转换: {os.path.basename(self.arxml_path)}")
        self.log(f"输出路径: {self.dbc_path}")

        worker = ARXMLConversionWorker(self.arxml_path, self.dbc_path, self)
        worker.log_emitted.connect(self.log)
        worker.succeeded.connect(self.conversion_success)
        worker.failed.connect(self.conversion_error)
        worker.finished.connect(
            lambda owner=worker: self._conversion_worker_finished(owner)
        )
        self._conversion_worker = worker
        self.arxml_select_btn.setEnabled(False)
        worker.start()

    def _run_conversion(self):
        """Compatibility entry for synchronous callers; production uses QThread."""
        try:
            _convert_arxml_to_dbc(self.arxml_path, self.dbc_path, self.log)
        except Exception as exc:
            import traceback
            self.log("❌ ARXML → DBC 转换异常:")
            self.log(traceback.format_exc())
            self.conversion_error.emit(str(exc))
            return
        self.conversion_success.emit()

    def _conversion_worker_finished(self, worker):
        if worker is not self._conversion_worker:
            worker.deleteLater()
            return
        self._conversion_worker = None
        worker.deleteLater()
        if self._close_after_conversion:
            self._close_after_conversion = False
            self.close()

    def _run_conversion_module(self):
        """兼容旧代码的入口。

        旧版本这里通过 ``sys.executable -m canmatrix.cli.convert`` 启动
        子进程。PyInstaller --onefile 下这是错误的，因为 sys.executable
        已经是当前 EXE。

        现在统一改成直接调用 Python API。
        """
        self._run_conversion()

    def _on_success(self):
        """转换成功"""
        self.is_running = False
        self.arxml_select_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.convert_btn.setEnabled(True)
        self.open_folder_btn.setEnabled(True)
        self.update_convert_button()
        self._set_conversion_status("转换成功！", "success")

        abs_path = os.path.abspath(self.dbc_path)

        # 等待文件完全写入（最多等待5秒）
        file_size = 0
        for _ in range(10):
            if os.path.exists(abs_path):
                try:
                    file_size = os.path.getsize(abs_path)
                    if file_size > 0:
                        break
                except OSError:
                    pass
            time.sleep(0.5)

        self.log(f"✅ 转换完成！DBC 文件已保存到: {abs_path}")
        self.log(f"✅ 文件大小: {file_size} 字节")
        self.log("=" * 50)

        QMessageBox.information(
            self,
            "成功",
            f"转换完成！\n\nDBC 文件已保存到:\n{abs_path}\n\n文件大小: {file_size} 字节"
        )

    def _on_error(self, error_msg):
        """转换失败"""
        self.is_running = False
        self.arxml_select_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.convert_btn.setEnabled(True)
        self.update_convert_button()
        self._set_conversion_status("转换失败", "error")
        self.log(f"❌ 转换失败: {error_msg}")
        self.log("=" * 50)
        QMessageBox.critical(self, "错误", f"转换失败：\n{error_msg}")

    def closeEvent(self, event):
        """关闭窗口时清理资源"""
        print("🔄 关闭 ARXML 转换工具...")

        if self._conversion_worker is not None and self._conversion_worker.isRunning():
            self._close_after_conversion = True
            self.log("转换仍在进行，完成后将安全关闭窗口。")
            event.ignore()
            return

        # 如果有正在运行的进程，尝试终止
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass

        # 清理进度条
        self.progress_bar.setVisible(False)

        print("✅ ARXML 转换工具已关闭")
        event.accept()


CAN_COLUMN_WIDTHS = (52, 40, 42, 36, 146, 34)


def _refresh_dynamic_style(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


class ElidedLabel(QLabel):
    """Compact label that keeps its complete value available as a tooltip."""

    def __init__(self, text="", parent=None):
        self._full_text = ""
        super().__init__("", parent)
        self.setText(text)

    def setText(self, text):
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._update_elided_text()

    def fullText(self):
        return self._full_text

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided_text()

    def _update_elided_text(self):
        width = max(0, self.contentsRect().width() - 2)
        display = self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideMiddle, width
        )
        QLabel.setText(self, display)


def create_bus_config_header(parent=None):
    header = QWidget(parent)
    header.setObjectName("busConfigHeader")
    header.setProperty("uiCanHeader", "true")
    layout = QGridLayout(header)
    layout.setContentsMargins(2, 2, 2, 2)
    layout.setHorizontalSpacing(1)
    labels = ("Bus", "CAN类型", "ID数量", "协议状态", "操作", "状态")
    for column, (text, width) in enumerate(zip(labels, CAN_COLUMN_WIDTHS)):
        label = QLabel(text)
        label.setProperty("uiCanHeaderCell", "true")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setMinimumWidth(width)
        layout.addWidget(label, 0, column)
    layout.setColumnStretch(3, 1)
    return header


class BusConfigWidget(QWidget):
    """单个总线的配置组件"""
    bus_id_changed = pyqtSignal(int, str)
    protocol_changed = pyqtSignal(int, str)
    parse_requested = pyqtSignal(int)
    protocol_removed = pyqtSignal(int)

    def __init__(self, bus_id, parent=None):
        super().__init__(parent)
        self.bus_id = bus_id
        self._instance_id = id(self)
        self.protocol_path = ""
        self._cached_filtered_db = None
        self._filtered_dbc_path = None
        self.arxml_sub_bus = ""
        self.id_label = QLabel()
        self.name_edit = QLineEdit()
        self.type_label = QLabel()
        self.id_count_label = QLabel()
        self.protocol_label = ElidedLabel()
        self.select_protocol_btn = QPushButton()
        self.protocol_clear_btn = QPushButton()
        self.parse_btn = QPushButton()
        self.status_indicator = QLabel()
        self.init_ui()

    def init_ui(self):
        self.setObjectName(f"busConfigRow{self.bus_id}")
        self.setProperty("uiCanRow", "true")
        self.setStyleSheet(build_can_config_stylesheet(DEFAULT_THEME))
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 3, 0, 3)
        self.card_group = QGroupBox("CAN 通道")
        self.card_group.setObjectName(f"busConfigCard{self.bus_id}")
        self.card_group.setProperty("uiCanCard", "true")
        layout = QVBoxLayout(self.card_group)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        channel_row = QWidget(self.card_group)
        channel_layout = QHBoxLayout(channel_row)
        channel_layout.setContentsMargins(0, 0, 0, 0)
        channel_layout.setSpacing(6)
        self.id_label.setText(f"Bus {self.bus_id}")
        self.id_label.setProperty("uiCanCell", "true")
        self.id_label.setMinimumWidth(54)
        channel_layout.addWidget(self.id_label)
        name_caption = QLabel("通道名称")
        name_caption.setProperty("uiCanCell", "true")
        channel_layout.addWidget(name_caption)
        self.name_edit.setPlaceholderText(f"Bus {self.bus_id}")
        self.name_edit.setProperty("uiCanCell", "bus")
        self.name_edit.setToolTip(f"Bus {self.bus_id}")
        self.name_edit.setMinimumWidth(70)
        self.name_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.name_edit.textChanged.connect(self.on_name_changed)
        self.name_edit.textChanged.connect(self.name_edit.setToolTip)
        channel_layout.addWidget(self.name_edit, 1)
        layout.addWidget(channel_row)

        state_row = QWidget(self.card_group)
        state_layout = QHBoxLayout(state_row)
        state_layout.setContentsMargins(0, 0, 0, 0)
        state_layout.setSpacing(12)
        self.type_label.setText("CAN2.0")
        self.type_label.setProperty("uiCanCell", "true")
        state_layout.addWidget(self.type_label)
        self.id_count_label.setText("0 IDs")
        self.id_count_label.setProperty("uiCanCell", "true")
        state_layout.addWidget(self.id_count_label)
        state_layout.addStretch(1)
        self.status_indicator.setProperty("uiCanCell", "status")
        self.status_indicator.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        state_layout.addWidget(self.status_indicator)
        layout.addWidget(state_row)

        self.protocol_label.setText("未配置")
        self.protocol_label.setProperty("uiCanCell", "protocol")
        self.protocol_label.setProperty("statusKind", "missing")
        self.protocol_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.protocol_label.setMinimumWidth(0)
        protocol_row = QWidget(self.card_group)
        protocol_layout = QHBoxLayout(protocol_row)
        protocol_layout.setContentsMargins(0, 0, 0, 0)
        protocol_layout.setSpacing(6)
        protocol_caption = QLabel("DBC 文件")
        protocol_caption.setProperty("uiCanCell", "true")
        protocol_layout.addWidget(protocol_caption)
        protocol_layout.addWidget(self.protocol_label, 1)
        layout.addWidget(protocol_row)

        self.select_protocol_btn.setText("选择协议")
        self.select_protocol_btn.setToolTip("选择 DBC 协议文件")
        self.select_protocol_btn.setProperty("uiRole", "secondary")
        self.select_protocol_btn.setMinimumWidth(72)
        self.select_protocol_btn.clicked.connect(self.select_protocol)
        self.protocol_clear_btn.setText("清除")
        self.protocol_clear_btn.setToolTip("清除当前 Bus 的 DBC 选择")
        self.protocol_clear_btn.setProperty("uiRole", "secondary")
        self.protocol_clear_btn.setMinimumWidth(52)
        self.protocol_clear_btn.clicked.connect(self.clear_protocol)
        self.protocol_clear_btn.setEnabled(False)
        self.parse_btn.setText("解析")
        self.parse_btn.setProperty("uiRole", "primary")
        self.parse_btn.setMinimumWidth(56)
        self.parse_btn.clicked.connect(lambda: self.parse_requested.emit(self.bus_id))
        action_row = QWidget(self.card_group)
        action_layout = QHBoxLayout(action_row)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(6)
        action_layout.addStretch(1)
        action_layout.addWidget(self.select_protocol_btn)
        action_layout.addWidget(self.parse_btn)
        action_layout.addWidget(self.protocol_clear_btn)
        layout.addWidget(action_row)
        outer_layout.addWidget(self.card_group)
        self.set_status('idle')

    def on_name_changed(self, text):
        self.bus_id_changed.emit(self.bus_id, text.strip() if text.strip() else f"Bus {self.bus_id}")

    def set_protocol_display(self, text):
        self.protocol_label.setText(text)
        self.protocol_clear_btn.setEnabled(
            text != "未配置" or bool(self.protocol_path)
        )
        self.protocol_label.setProperty(
            "statusKind", "missing" if text == "未配置" else "configured"
        )
        _refresh_dynamic_style(self.protocol_label)

    def select_protocol(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, f"选择 Bus {self.bus_id} 的协议文件", "",
            "DBC Files (*.dbc);;All Files (*)"
        )
        if not file_path:
            return

        ext = os.path.splitext(file_path)[1].lower()
        if ext != '.dbc':
            QMessageBox.warning(self, "不支持的文件格式", "请选择 DBC 文件")
            return

        try:
            if cantools is None:
                raise ImportError("cantools 库未安装")

            db = cantools.database.load_file(file_path)

            # The selected file path is parsing data, while any filtering
            # suffix is display-only.  Reset the previous bus-local cache
            # before building a replacement so a re-selection cannot reuse it.
            self.protocol_path = file_path
            self._cached_filtered_db = None
            self._filtered_dbc_path = None

            bus_name = os.path.basename(file_path).replace('.dbc', '')

            # 调试：向上查找 MDFPlotter
            print(f"\n{'=' * 50}")
            print(f"🔍 select_protocol 调试:")
            print(f"  - self.bus_id: {self.bus_id}")

            parent = self.parent()
            depth = 0
            while parent is not None and depth < 5:
                print(f"  - 父级 {depth}: {type(parent)}")
                if hasattr(parent, 'can_bus_data'):
                    print(f"  ✅ 找到 MDFPlotter (含 can_bus_data)!")
                    break
                parent = parent.parent()
                depth += 1
            print(f"{'=' * 50}\n")

            filtered_db = self.filter_dbc_signals(db, bus_name)

            if filtered_db is not db:
                self._cached_filtered_db = filtered_db
                if self.protocol_clear_btn:
                    self.protocol_clear_btn.setEnabled(True)
                self.protocol_changed.emit(self.bus_id, self.protocol_path)
            else:
                self.set_protocol_display(os.path.basename(file_path))
                if self.protocol_clear_btn:
                    self.protocol_clear_btn.setEnabled(True)
                self.protocol_changed.emit(self.bus_id, file_path)

        except ImportError as e:
            QMessageBox.warning(self, "依赖缺失", f"请安装 cantools 库: pip install cantools\n\n错误: {e}")
        except Exception as e:
            QMessageBox.warning(self, "DBC加载失败", f"加载DBC时出错: {e}")
            import traceback
            traceback.print_exc()

    def clear_protocol(self):
        self.protocol_path = ""
        self._cached_filtered_db = None
        self._filtered_dbc_path = None
        self.arxml_sub_bus = ""
        self.set_protocol_display("未配置")
        if self.protocol_clear_btn:
            self.protocol_clear_btn.setEnabled(False)
        self.protocol_removed.emit(self.bus_id)
        self.set_status('idle')

    def set_id_count(self, count, parsed_count=None):
        if parsed_count is not None and parsed_count > 0:
            self.id_count_label.setText(f"{parsed_count}/{count} IDs")
        elif count > 0:
            self.id_count_label.setText(f"{count} IDs")
        else:
            self.id_count_label.setText("0 IDs")

    def set_bus_type(self, is_fd):
        if is_fd:
            self.type_label.setText("CANFD")
        else:
            self.type_label.setText("CAN2.0")

    def set_status(self, status, extra_info=""):
        if status == 'idle':
            self.status_indicator.setText("就绪")
            self.status_indicator.setToolTip("就绪")
        elif status == 'loading':
            self.status_indicator.setText("解析中")
            self.status_indicator.setToolTip("解析中...")
        elif status == 'success':
            self.status_indicator.setText("完成")
            self.status_indicator.setToolTip(f"解析完成{extra_info}")
        elif status == 'error':
            self.status_indicator.setText("失败")
            self.status_indicator.setToolTip("解析失败")
        elif status == 'stopped':
            self.status_indicator.setText("解析停止")
            self.status_indicator.setToolTip("解析已停止，本次未生成解析结果")
        self.status_indicator.setProperty("statusKind", status)
        _refresh_dynamic_style(self.status_indicator)

    def get_bus_display_name(self):
        name = self.name_edit.text().strip()
        return name if name else f"Bus {self.bus_id}"

    def filter_dbc_signals(self, db, bus_name):
        """显示信号筛选对话框，返回筛选后的数据库 - 自动保留Header信号"""

        # 安全获取该总线实际存在的CAN ID - 向上遍历查找 MDFPlotter
        existing_ids = []
        try:
            # 向上遍历父级链查找 MDFPlotter
            parent = self.parent()
            mdf_plotter = None

            while parent is not None:
                if hasattr(parent, 'can_bus_data'):
                    mdf_plotter = parent
                    print(f"✅ 找到 MDFPlotter: {type(parent)}")
                    break
                parent = parent.parent()

            if mdf_plotter is not None and hasattr(mdf_plotter, 'can_bus_data'):
                print(f"🔍 can_bus_data keys: {list(mdf_plotter.can_bus_data.keys())}")
                print(f"🔍 当前 bus_id: {self.bus_id} (类型: {type(self.bus_id)})")

                # 尝试匹配 bus_id
                bus_id_int = int(self.bus_id) if isinstance(self.bus_id, str) else self.bus_id

                # 遍历所有总线数据
                for key, bus_data in mdf_plotter.can_bus_data.items():
                    print(f"  - 检查 key: {key} (类型: {type(key)})")
                    if int(key) == bus_id_int:
                        existing_ids = bus_data.get('actual_ids', [])
                        print(f"✅ 匹配到总线 {key}, actual_ids 数量: {len(existing_ids)}")
                        if existing_ids:
                            print(f"  - actual_ids 前10个: {list(existing_ids)[:10]}")
                        break

                # 如果上面没匹配到，尝试直接获取
                if not existing_ids:
                    bus_data = mdf_plotter.can_bus_data.get(bus_id_int, {})
                    existing_ids = bus_data.get('actual_ids', [])
                    if existing_ids:
                        print(f"✅ 通过直接获取匹配到总线 {bus_id_int}, actual_ids 数量: {len(existing_ids)}")
            else:
                print("⚠️ 未找到 MDFPlotter 或 can_bus_data")

        except Exception as e:
            print(f"⚠️ 获取总线ID列表失败: {e}")
            import traceback
            traceback.print_exc()
            existing_ids = []

        # 创建筛选对话框，传入 existing_ids
        dialog = SignalFilterDialog(db, bus_name, existing_ids, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected_signals = dialog.get_selected_signals_by_frame()

            if not selected_signals:
                QMessageBox.warning(self, "筛选信号", "未选择任何信号，将使用所有信号")
                return db

            total_original = sum(len(message.signals) for message in db.messages)
            new_db, auto_added = self._build_filtered_database(db, selected_signals)
            total_filtered = sum(len(message.signals) for message in new_db.messages)

            print(f"✅ 信号筛选完成:")
            print(f"   原始: {len(db.messages)} 个消息, {total_original} 个信号")
            print(f"   保留: {len(new_db.messages)} 个消息, {total_filtered} 个信号")
            print(f"   自动添加Header信号: {auto_added} 个")

            self._offer_save_filtered_dbc(new_db, self.protocol_path)

            return new_db
        return db

    @staticmethod
    def _build_filtered_database(db, selected_signals):
        """Build a ready-to-decode cantools database from selected signal names."""
        if not selected_signals:
            return db, 0

        from cantools.database.can import Database, Message

        new_messages = []
        auto_added = 0
        for msg_def in db.messages:
            frame_key = (
                int(msg_def.frame_id),
                bool(getattr(msg_def, 'is_extended_frame', False)),
            )
            selected_names = set(selected_signals.get(
                frame_key, selected_signals.get(msg_def.frame_id, ())
            ))
            if not selected_names:
                continue

            for signal in msg_def.signals:
                name_lower = signal.name.lower()
                is_header = (
                    'header_id' in name_lower
                    or 'pdu_id' in name_lower
                    or 'header_dlc' in name_lower
                    or 'pdu_len' in name_lower
                )
                if is_header and signal.name not in selected_names:
                    selected_names.add(signal.name)
                    auto_added += 1
                    print(
                        f"  🔄 自动保留Header信号: {signal.name} "
                        f"(帧 0x{msg_def.frame_id:X})"
                    )

            filtered_signals = [
                signal for signal in msg_def.signals
                if signal.name in selected_names
            ]
            if not filtered_signals:
                continue

            new_message = Message(
                frame_id=msg_def.frame_id,
                name=msg_def.name,
                length=msg_def.length,
                signals=filtered_signals,
                contained_messages=(
                    list(msg_def.contained_messages)
                    if msg_def.contained_messages is not None
                    else None
                ),
                header_id=msg_def.header_id,
                header_byte_order=msg_def.header_byte_order,
                unused_bit_pattern=msg_def.unused_bit_pattern,
                comment=msg_def.comment,
                senders=list(msg_def.senders),
                send_type=msg_def.send_type,
                cycle_time=msg_def.cycle_time,
                dbc_specifics=msg_def.dbc,
                autosar_specifics=msg_def.autosar,
                is_extended_frame=msg_def.is_extended_frame,
                is_fd=msg_def.is_fd,
                bus_name=msg_def.bus_name,
                signal_groups=(
                    list(msg_def.signal_groups)
                    if msg_def.signal_groups is not None
                    else None
                ),
                protocol=msg_def.protocol,
                sort_signals=None,
            )
            new_message.refresh()
            new_messages.append(new_message)

        new_db = Database(
            messages=new_messages,
            nodes=list(db.nodes),
            buses=list(db.buses),
            version=db.version,
            dbc_specifics=db.dbc,
            autosar_specifics=db.autosar,
            frame_id_mask=getattr(db, '_frame_id_mask', None),
            sort_signals=None,
        )
        new_db.refresh()
        return new_db, auto_added

    def _use_filtered_dbc_in_memory(self, original_path):
        """Keep a filtered database for parsing without changing its source path."""
        self.protocol_path = original_path
        self.set_protocol_display(
            os.path.basename(original_path) + " [已筛选-内存]"
        )
        self._filtered_dbc_path = None

    def _offer_save_filtered_dbc(self, db, original_path):
        """Ask before opening the filtered-DBC save dialog."""
        choice = QMessageBox.question(
            self,
            "保存筛选后的DBC文件",
            "是否将筛选后的 DBC 保存为新文件？\n\n"
            "选择“否”将继续使用内存中的筛选结果，不会修改原 DBC。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice == QMessageBox.StandardButton.Yes:
            self._save_filtered_dbc_with_custom_name(db, original_path)
        else:
            self._use_filtered_dbc_in_memory(original_path)

    def _save_filtered_dbc_with_custom_name(self, db, original_path):
        """保存筛选后的DBC，支持自定义文件名"""
        try:
            from datetime import datetime

            original_dir = os.path.dirname(original_path)
            original_basename = os.path.splitext(os.path.basename(original_path))[0]

            default_name = f"{original_basename}_filtered.dbc"
            save_path, _ = QFileDialog.getSaveFileName(
                self,
                "保存筛选后的DBC文件",
                os.path.join(original_dir, default_name),
                "DBC Files (*.dbc)"
            )

            if not save_path:
                QMessageBox.information(
                    self,
                    "保存取消",
                    "已取消保存筛选后的DBC文件，将使用筛选后的数据库进行解析，但不会保存到文件。"
                )
                self._use_filtered_dbc_in_memory(original_path)
                return

            if not save_path.endswith('.dbc'):
                save_path += '.dbc'

            dbc_content = self._generate_dbc_content(db)

            with open(save_path, 'w', encoding='utf-8') as f:
                f.write(dbc_content)

            print(f"✅ 筛选后的DBC已保存: {save_path}")

            filter_info = {
                'original_file': os.path.basename(original_path),
                'original_path': original_path,
                'filtered_file': os.path.basename(save_path),
                'filtered_path': save_path,
                'total_messages': len(db.messages),
                'total_signals': sum(len(m.signals) for m in db.messages),
                'auto_added_headers': True,
                'created_at': datetime.now().isoformat()
            }

            info_path = save_path.replace('.dbc', '_info.json')
            with open(info_path, 'w', encoding='utf-8') as f:
                json.dump(filter_info, f, indent=2, ensure_ascii=False)

            self.protocol_path = save_path
            self.set_protocol_display(os.path.basename(save_path) + " [已筛选]")
            self._filtered_dbc_path = save_path

            print(f"📄 筛选信息: {info_path}")

            QMessageBox.information(
                self,
                "保存成功",
                f"✅ 筛选后的DBC已保存到:\n{save_path}\n\n"
                f"包含 {len(db.messages)} 个消息, "
                f"{sum(len(m.signals) for m in db.messages)} 个信号"
            )

        except Exception as e:
            print(f"⚠️ 保存筛选后的DBC失败: {e}")
            import traceback
            traceback.print_exc()
            QMessageBox.warning(
                self,
                "保存失败",
                f"保存筛选后的DBC失败:\n{str(e)}\n\n将使用筛选后的数据库进行解析，但不会保存到文件。"
            )
            self._use_filtered_dbc_in_memory(original_path)

    def _generate_dbc_content(self, db):
        """生成DBC文件内容，保持信号顺序"""
        lines = []

        lines.append('VERSION ""')
        lines.append('')

        lines.append('NS_ :')
        lines.append('  NS_DESC_')
        lines.append('  CM_')
        lines.append('  BA_DEF_')
        lines.append('  BA_')
        lines.append('  VAL_')
        lines.append('  CAT_DEF_')
        lines.append('  CAT_')
        lines.append('  FILTER')
        lines.append('  BA_DEF_DEF_')
        lines.append('  EV_DATA_')
        lines.append('  ENVVAR_DATA_')
        lines.append('  SGTYPE_')
        lines.append('  SGTYPE_VAL_')
        lines.append('  BA_DEF_SGTYPE_')
        lines.append('  BA_SGTYPE_')
        lines.append('  SIG_TYPE_REF_')
        lines.append('  VAL_TABLE_')
        lines.append('  SIG_GROUP_')
        lines.append('  SIG_VALTYPE_')
        lines.append('  SIGTYPE_VALTYPE_')
        lines.append('  BO_TX_BU_')
        lines.append('  BA_DEF_REL_')
        lines.append('  BA_REL_')
        lines.append('  BA_DEF_DEF_REL_')
        lines.append('  BU_SG_REL_')
        lines.append('  BU_EV_REL_')
        lines.append('  BU_BO_REL_')
        lines.append('  SG_MUL_VAL_')
        lines.append('')

        for msg in db.messages:
            senders = getattr(msg, 'senders', [])
            sender_str = ', '.join(senders) if senders else 'Vector__XXX'
            line = f'BO_ {msg.frame_id} {msg.name}: {msg.length} {sender_str}'
            if hasattr(msg, 'comment') and msg.comment:
                line += f' // {msg.comment}'
            lines.append(line)

            for sig in msg.signals:
                sig_line = self._format_signal_line(sig)
                lines.append(f'  {sig_line}')

            lines.append('')

        return '\n'.join(lines)

    @staticmethod
    def _format_signal_line(sig):
        """格式化单个信号行，保持原始DBC格式"""
        name = sig.name
        start = getattr(sig, 'start', 0)
        length = getattr(sig, 'length', 1)
        is_little = getattr(sig, 'is_little_endian', True)
        endian = '0' if is_little else '1'
        signed = '0'
        if hasattr(sig, 'is_signed'):
            signed = '1' if sig.is_signed else '0'
        scale = getattr(sig, 'scale', 1.0)
        offset = getattr(sig, 'offset', 0.0)
        unit = getattr(sig, 'unit', '') or ''

        minimum = getattr(sig, 'minimum', None)
        maximum = getattr(sig, 'maximum', None)
        if minimum is None or maximum is None:
            if signed == '1':
                max_val = (1 << (length - 1)) - 1
                min_val = -(1 << (length - 1))
            else:
                max_val = (1 << length) - 1
                min_val = 0
        else:
            min_val = minimum
            max_val = maximum

        receivers = getattr(sig, 'receivers', [])
        receiver_str = ','.join(receivers) if receivers else 'Vector__XXX'

        multiplexer = ''
        if hasattr(sig, 'multiplexer_id'):
            multiplexer = f' m{sig.multiplexer_id}'
        elif hasattr(sig, 'multiplex'):
            multiplexer = f' m{sig.multiplex}'
        elif hasattr(sig, 'multiplex_switch'):
            multiplexer = f' m{sig.multiplex_switch}'
        elif hasattr(sig, 'multiplexer_ids') and sig.multiplexer_ids:
            multiplexer = f' m{sig.multiplexer_ids[0]}'

        sig_line = f'SG_{name}{multiplexer} : {start}|{length}@{endian}{signed} ({scale},{offset}) [{min_val}|{max_val}] "{unit}" {receiver_str}'

        if hasattr(sig, 'comment') and sig.comment:
            sig_line += f' // {sig.comment}'

        return sig_line


class ParseProgressDialog(QDialog):
    """解析进度对话框"""
    cancel_requested = pyqtSignal(int)

    def __init__(self, bus_id, parent=None):
        super().__init__(parent)
        self.bus_id = bus_id
        self._instance_id = id(self)
        self._cancelled = False
        self._finished = False
        self.status_label = QLabel()
        self.progress_bar = QProgressBar()
        self.detail_label = QLabel()
        self.cancel_btn = QPushButton()

        self.setWindowTitle(f"解析 Bus {bus_id} 中...")
        self.setModal(False)
        self.setFixedSize(420, 160)

        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.CustomizeWindowHint |
            Qt.WindowType.WindowTitleHint |
            Qt.WindowType.WindowCloseButtonHint
        )

        if parent:
            self.setParent(parent)
            parent_geo = parent.geometry()
            x = parent_geo.x() + (parent_geo.width() - self.width()) // 2
            y = parent_geo.y() + (parent_geo.height() - self.height()) // 2
            self.move(x, y)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        self.status_label.setText("正在初始化解析...")
        self.status_label.setStyleSheet("font-size: 12px; font-weight: bold; color: #2c3e50;")
        layout.addWidget(self.status_label)

        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #bdc3c7;
                border-radius: 4px;
                text-align: center;
                height: 20px;
            }
            QProgressBar::chunk {
                background-color: #27ae60;
                border-radius: 4px;
            }
        """)
        layout.addWidget(self.progress_bar)

        self.detail_label.setText("准备开始...")
        self.detail_label.setStyleSheet("color: #7f8c8d; font-size: 10px;")
        layout.addWidget(self.detail_label)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.cancel_btn.setText("取消")
        self.cancel_btn.setFixedWidth(80)
        self.cancel_btn.setProperty("uiRole", "secondary")
        self.cancel_btn.clicked.connect(self.on_cancel)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

    def update_progress(self, progress, status_text):
        self.progress_bar.setValue(progress)
        if status_text:
            self.status_label.setText(status_text)

    def set_detail(self, detail_text):
        self.detail_label.setText(detail_text)

    def set_finished(self, success=True, stats=None):
        self._finished = True
        if success and stats:
            self.status_label.setText("✅ 解析完成!")
            self.progress_bar.setValue(100)
            detail = (f"处理消息: {stats.get('messages_processed', 0)}, "
                      f"解码信号: {stats.get('signals_decoded', 0)}, "
                      f"CAN ID: {stats.get('msg_ids_with_signals', 0)}")
            self.detail_label.setText(detail)
            self.cancel_btn.setText("关闭")
            self.cancel_btn.clicked.disconnect()
            self.cancel_btn.clicked.connect(self.close)
        else:
            self.status_label.setText("❌ 解析失败")
            self.progress_bar.setValue(0)
            self.cancel_btn.setText("关闭")
            self.cancel_btn.clicked.disconnect()
            self.cancel_btn.clicked.connect(self.close)

    def set_empty_result(self, reason):
        """Show an expected no-result outcome without claiming a worker crash."""
        self._finished = True
        self.status_label.setText("❌ 未生成可用信号")
        self.detail_label.setText(reason)
        self.cancel_btn.setText("关闭")
        self.cancel_btn.clicked.disconnect()
        self.cancel_btn.clicked.connect(self.close)

    def set_cancelled(self):
        self._finished = True
        self._cancelled = True
        self.status_label.setText("⏹ 解析停止")
        self.detail_label.setText("当前通道已停止解析，本次未生成解析结果。")
        self.cancel_btn.setText("关闭")
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.clicked.disconnect()
        self.cancel_btn.clicked.connect(self.close)

    def on_cancel(self):
        if self._cancelled or self._finished:
            return
        self._cancelled = True
        self.status_label.setText("⏹ 正在取消...")
        self.cancel_btn.setEnabled(False)
        self.cancel_requested.emit(self.bus_id)

    def is_cancelled(self):
        return self._cancelled

    def showEvent(self, event):
        super().showEvent(event)

    def hideEvent(self, event):
        super().hideEvent(event)

    def closeEvent(self, event):
        if not self._finished:
            self.on_cancel()
            event.ignore()
            return
        event.accept()


class ParseWorker(QThread):
    """后台解析线程 - 从 main_py.py 完整移植容器帧解析逻辑"""
    progress_updated = pyqtSignal(int, int, str)
    parse_finished = pyqtSignal(int, dict)
    parse_error = pyqtSignal(int, str)
    parse_cancelled = pyqtSignal(int)

    def __init__(self, bus_id, target_raw_channel, msg_count, log_path, file_identity, db, arxml_sub_bus="", parent=None):
        super().__init__(parent)
        if isinstance(msg_count, bool) or not isinstance(msg_count, int) or msg_count <= 0:
            raise ValueError("msg_count 必须是大于0的整数")
        self.bus_id = bus_id
        self.target_raw_channel = target_raw_channel
        self.msg_count = msg_count
        self.log_path = log_path
        self.file_identity = file_identity
        self.db = db
        self.arxml_sub_bus = arxml_sub_bus
        self._is_running = True
        self.raw_message_cache = None
        self.raw_cache_collectors = {}
        self.raw_cache_frame_keys = frozenset()
        self.progress_message_total = msg_count

        # ===== 多路复用映射（从 main_py.py 移植） =====
        self.multiplexer_signals = {}
        self.is_multiplexer = False
        self.header_id_signal = None
        self.header_dlc_signal = None
        self.header_total_bits = 0
        self.container_frames_info = {}

        if db:
            self._build_multiplexer_mapping(db)

    @staticmethod
    def _get_signal_endianness(sig):
        """获取信号端序"""
        if hasattr(sig, 'is_little_endian'):
            return sig.is_little_endian
        if hasattr(sig, 'byte_order'):
            return sig.byte_order == 'little_endian'
        return False

    def _build_multiplexer_mapping(self, db):
        """
        构建多路复用信号映射 - 支持多个容器帧
        增强 ARXML 转换后的 DBC 支持
        """
        print(f"  📌 检测 Multiplexer 信号...")

        self.container_frames_info = {}
        self.is_multiplexer = False

        if not db:
            self.container_frames_info = {}
            self.is_multiplexer = False
            return

        # ===== 第一步：检测所有容器帧 =====
        for msg_def in db.messages:
            header_id_sig = None
            header_dlc_sig = None
            for sig in msg_def.signals:
                name_lower = sig.name.lower()
                if 'header_id' in name_lower or 'pdu_id' in name_lower:
                    header_id_sig = sig
                if 'header_dlc' in name_lower or 'pdu_len' in name_lower:
                    header_dlc_sig = sig

            if header_id_sig and header_dlc_sig:
                frame_id = msg_def.frame_id
                frame_key = (
                    frame_id,
                    bool(getattr(msg_def, 'is_extended_frame', False)),
                )
                header_total_bits = header_id_sig.length + header_dlc_sig.length

                self.container_frames_info[frame_key] = {
                    'frame_id': frame_id,
                    'frame_name': msg_def.name,
                    'header_id_sig': header_id_sig,
                    'header_dlc_sig': header_dlc_sig,
                    'header_total_bits': header_total_bits,
                    'multiplexer_signals': {}
                }
                print(f"  📦 检测到容器帧: 0x{frame_id:X} ({msg_def.name})")
                print(f"     Header_ID: start={header_id_sig.start}, length={header_id_sig.length} bits")
                print(f"     Header_DLC: start={header_dlc_sig.start}, length={header_dlc_sig.length} bits")
                print(f"     Header总位数: {header_total_bits} bits")

        if not self.container_frames_info:
            print(f"  ⚠️ 未检测到任何容器帧定义")
            self.is_multiplexer = False
            return

        # ===== 第二步：为每个容器帧构建 multiplexer 信号映射 =====
        for frame_key, frame_info in self.container_frames_info.items():
            frame_id = frame_info['frame_id']
            multiplexer_signals = {}

            for msg_def in db.messages:
                message_key = (
                    msg_def.frame_id,
                    bool(getattr(msg_def, 'is_extended_frame', False)),
                )
                if message_key != frame_key:
                    continue

                # 打印信号信息以便调试
                print(f"  📋 帧 0x{frame_id:X} 包含 {len(msg_def.signals)} 个信号")
                for sig in msg_def.signals:
                    # 跳过 Header_ID 和 Header_DLC
                    name_lower = sig.name.lower()
                    if 'header_id' in name_lower or 'pdu_id' in name_lower:
                        print(f"    跳过 Header_ID: {sig.name}")
                        continue
                    if 'header_dlc' in name_lower or 'pdu_len' in name_lower:
                        print(f"    跳过 Header_DLC: {sig.name}")
                        continue

                    try:
                        multiplexer_id = None

                        # ===== 多种方式提取 multiplexer_id =====
                        # 方式1: 直接属性
                        if hasattr(sig, 'multiplexer_id'):
                            multiplexer_id = sig.multiplexer_id
                            print(f"    方式1: {sig.name} -> multiplexer_id={multiplexer_id}")

                        # 方式2: multiplexer_ids (列表)
                        if multiplexer_id is None and hasattr(sig, 'multiplexer_ids'):
                            mids = sig.multiplexer_ids
                            if mids:
                                multiplexer_id = mids[0] if isinstance(mids, (list, tuple)) else mids
                                print(f"    方式2: {sig.name} -> multiplexer_ids={multiplexer_id}")

                        # 方式3: multiplex 属性 (ARXML 转换后常用)
                        if multiplexer_id is None and hasattr(sig, 'multiplex'):
                            multiplexer_id = sig.multiplex
                            print(f"    方式3: {sig.name} -> multiplex={multiplexer_id}")

                        # 方式4: 从信号名提取 m<数字> (如 m65538)
                        if multiplexer_id is None:
                            match = re.search(r'_?m(\d+)', sig.name)
                            if match:
                                multiplexer_id = int(match.group(1))
                                print(f"    方式4: {sig.name} -> 从名称提取 m{multiplexer_id}")

                        # 方式5: 从信号注释中提取
                        if multiplexer_id is None and hasattr(sig, 'comment') and sig.comment:
                            comment_match = re.search(r'm(\d+)', str(sig.comment))
                            if comment_match:
                                multiplexer_id = int(comment_match.group(1))
                                print(f"    方式5: {sig.name} -> 从注释提取 m{multiplexer_id}")

                        if multiplexer_id is not None:
                            if multiplexer_id not in multiplexer_signals:
                                multiplexer_signals[multiplexer_id] = []

                            is_little_endian = self._get_signal_endianness(sig)
                            is_signed = self._get_signal_is_signed(sig)

                            multiplexer_signals[multiplexer_id].append({
                                'name': sig.name,
                                'start': sig.start,
                                'length': sig.length,
                                'scale': sig.scale,
                                'offset': sig.offset,
                                'unit': getattr(sig, 'unit', '') or '',
                                'is_little_endian': is_little_endian,
                                'is_signed': is_signed,
                            })

                            print(f"    ✅ {sig.name} -> m{multiplexer_id} (start={sig.start}, len={sig.length})")
                        else:
                            print(f"    ⚠️ {sig.name} 没有 multiplexer_id")

                    except Exception as e:
                        print(f"    ❌ 处理信号 {sig.name} 失败: {e}")
                        pass

            frame_info['multiplexer_signals'] = multiplexer_signals
            print(f"  📊 帧 0x{frame_id:X} 分组数: {len(multiplexer_signals)}")
            for mid, sigs in list(multiplexer_signals.items())[:10]:
                print(f"     m{mid}: {len(sigs)} 个信号")
                for sig_info in sigs[:3]:
                    print(f"        - {sig_info['name']}: start={sig_info['start']}, len={sig_info['length']}")

        self.is_multiplexer = bool(self.container_frames_info)

    @staticmethod
    def decode_signal(data, start_bit, length, is_little_endian, header_total_bits, is_signed=False):
        """
        按照 Vector/CANoe DBC 的 StartBit 规则解析 PDU 信号。
        从 main_py.py 完整移植，增加有符号数支持

        对 AUTOSAR PDU Container：
            DBC StartBit = PDU payload 内 StartBit + Header总长度
            所以实际传入的 data 是不含 Header 的 PDU payload，需要：
                pdu_start_bit = dbc_start_bit - header_total_bits

        Motorola DBC bit traversal：
            bit 7 -> 6 -> ... -> 0 -> 下一字节 bit 7 -> ...

        有符号数处理：
            如果信号是有符号的，检查最高位 (MSB) 是否为 1，如果是则进行符号扩展
        """
        try:
            if not data or length <= 0:
                return None

            start_bit = int(start_bit)
            length = int(length)
            header_total_bits = int(header_total_bits or 0)

            # DBC StartBit 是相对于完整 Container/PDU 布局的
            pdu_start_bit = start_bit - header_total_bits
            if pdu_start_bit < 0:
                return None

            data_bits = len(data) * 8

            if is_little_endian:
                # Intel / Little Endian：StartBit 是 LSB 位置
                if pdu_start_bit + length > data_bits:
                    return None
                raw = int.from_bytes(data, byteorder='little', signed=False)
                raw >>= pdu_start_bit
                value = raw & ((1 << length) - 1)
            else:
                # ============================================================
                # Motorola / Big Endian (@0)
                # ============================================================
                bit_pos = pdu_start_bit
                value = 0

                for _ in range(length):
                    byte_index = bit_pos // 8
                    bit_index = bit_pos % 8

                    if byte_index < 0 or byte_index >= len(data):
                        return None

                    value = (value << 1) | ((data[byte_index] >> bit_index) & 0x01)

                    # Motorola saw-tooth traversal
                    if bit_index == 0:
                        bit_pos += 15  # 当前 byte bit0 -> 下一 byte bit7
                    else:
                        bit_pos -= 1

            # ===== 有符号数处理：符号扩展 =====
            if is_signed and length > 0:
                # 检查最高位（符号位）是否为 1
                sign_bit = 1 << (length - 1)
                if value & sign_bit:
                    # 负数：进行符号扩展
                    value = value - (1 << length)

            return value

        except Exception:
            return None

    @staticmethod
    def _get_signal_is_signed(sig):
        """获取信号是否为有符号数"""
        # 检查信号的最小值是否为负数
        if hasattr(sig, 'minimum') and sig.minimum is not None:
            if sig.minimum < 0:
                return True
        # 检查信号的最大值是否为负数（有些DBC只定义了最大值）
        if hasattr(sig, 'maximum') and sig.maximum is not None:
            if sig.maximum < 0:
                return True
        # 检查信号名是否包含负值提示
        if hasattr(sig, 'comment') and sig.comment:
            if 'negative' in sig.comment.lower() or 'signed' in sig.comment.lower():
                return True
        # 默认：如果范围包含负数，则是有符号数
        if hasattr(sig, 'minimum') and hasattr(sig, 'maximum'):
            if sig.minimum is not None and sig.maximum is not None:
                if sig.minimum < 0:
                    return True
        return False

    def stop(self):
        self._is_running = False

    def _parse_stream(self):
        try:
            # ===== 构建消息映射 =====
            msg_shard = {}
            signal_identity = {}
            for msg_def in self.db.messages:
                frame_id = msg_def.frame_id
                message_key = (frame_id, bool(getattr(msg_def, 'is_extended_frame', False)))
                msg_shard[message_key] = msg_def
                for signal in msg_def.signals:
                    signal_identity.setdefault(signal.name, []).append({
                        'raw_channel': int(self.target_raw_channel),
                        'frame_id': int(frame_id),
                        'is_extended': bool(getattr(msg_def, 'is_extended_frame', False)),
                        'message_name': str(msg_def.name),
                        'signal_name': str(signal.name),
                    })
                print(f"  📨 注册消息: ID=0x{frame_id:X} ({frame_id}), 信号数={len(msg_def.signals)}")

            print(f"\n总线 {self.bus_id}: 数据库中有 {len(msg_shard)} 个消息定义")
            message_labels = [
                f"{hex(frame_id)}/{'EXT' if is_extended else 'STD'}"
                for frame_id, is_extended in sorted(msg_shard.keys())
            ]
            print(
                "  消息ID列表: "
                f"{message_labels}"
            )

            total_msgs = self.progress_message_total
            print(f"总线 {self.bus_id}: 探测消息总数: {total_msgs}")

            t0 = None
            pool = defaultdict(lambda: {'t': [], 'v': [], 'unit': '', 'comment': ''})

            total_messages_processed = 0
            scanned_frames = 0
            dbc_undefined_frames = 0
            decoded_frames = 0
            empty_decoded_frames = 0
            truncated_frames = 0
            decode_failed_frames = 0
            decode_error_counts = defaultdict(int)
            total_signals_decoded = 0
            msg_ids_with_signals = set()
            matched_msg_ids = set()
            matched_frames = set()
            unmatched_msg_ids = set()
            container_frame_counts = defaultdict(int)  # 每个容器帧的计数

            if total_msgs > 0:
                progress_interval = max(1, total_msgs // 200)
            else:
                progress_interval = 1000

            last_progress_update = 0
            last_progress_percent = 0
            frame_match_seconds = 0.0
            signal_decode_seconds = 0.0
            result_append_seconds = 0.0

            # ===== 遍历所有消息 =====
            print(f"\n📖 开始遍历消息...")

            while True:
                stream_item = yield
                if stream_item is None:
                    break
                msg, file_t0 = stream_item
                if t0 is None:
                    t0 = file_t0

                raw_channel = message_raw_channel(msg)
                if raw_channel != self.target_raw_channel:
                    continue

                scanned_frames += 1
                if total_msgs > 0:
                    progress = int(scanned_frames / total_msgs * 100)
                    if progress > last_progress_percent:
                        progress_to_show = min(99, progress)
                        if progress_to_show > last_progress_percent:
                            self.progress_updated.emit(
                                self.bus_id,
                                progress_to_show,
                                f"扫描中... {scanned_frames:,} / {total_msgs:,} 条报文"
                            )
                            last_progress_percent = progress_to_show
                    elif scanned_frames - last_progress_update >= progress_interval:
                        self.progress_updated.emit(
                            self.bus_id,
                            min(99, last_progress_percent),
                            f"扫描中... {scanned_frames:,} / {total_msgs:,} 条报文"
                        )
                        last_progress_update = scanned_frames

                rel_t = msg.timestamp - t0
                msg_id = msg.arbitration_id
                is_extended_id = bool(getattr(msg, 'is_extended_id', False))

                # ===== 专门打印 0x13E 帧 =====
                if msg_id == 0x13E:
                    print(f"\n{'=' * 60}")
                    print(f"🔍 [0x13E 帧] 时间: {rel_t:.6f}s, 数据长度: {len(msg.data)}, 数据: {msg.data.hex()}")
                    print(f"   raw_channel: {raw_channel}, bus_id: {self.bus_id}")

                message_key = (msg_id, is_extended_id)
                match_started = time.perf_counter()
                msg_def = msg_shard.get(message_key)
                frame_match_seconds += time.perf_counter() - match_started
                if msg_def is None:
                    unmatched_msg_ids.add(msg_id)
                    dbc_undefined_frames += 1
                    continue

                matched_msg_ids.add(msg_id)
                matched_frames.add(message_key)
                total_messages_processed += 1
                decode_started = time.perf_counter()
                frame_signals_before = total_signals_decoded
                frame_decode_failed = False
                is_truncated = (
                    bool(getattr(msg, 'is_fd', False))
                    and len(msg.data) < msg_def.length
                )
                if is_truncated:
                    truncated_frames += 1

                # ===== 检测是否为容器帧 =====
                if self.is_multiplexer and message_key in self.container_frames_info:
                    container_frame_counts[message_key] += 1
                    frame_info = self.container_frames_info[message_key]
                    header_total_bits = frame_info['header_total_bits']
                    multiplexer_signals = frame_info['multiplexer_signals']
                    header_bytes = (header_total_bits + 7) // 8
                    frame_name = frame_info['frame_name']

                    data = msg.data
                    i = 0
                    data_len = len(data)
                    current_count = container_frame_counts[message_key]

                    # ===== 专门打印 0x13E 帧的详细信息 =====
                    if msg_id == 0x13E:
                        print(f"\n{'=' * 60}")
                        print(f"🔍 [0x13E 容器帧 #{current_count}]")
                        print(f"   时间: {rel_t:.6f}s")
                        print(f"   数据长度: {data_len} 字节")
                        print(f"   原始数据: {data.hex()}")
                        print(f"   Header总位数: {header_total_bits} bits")
                        print(f"   Header字节数: {header_bytes} bytes")
                        print(f"   帧名: {frame_name}")
                        print(f"   可用的 Header_ID: {list(multiplexer_signals.keys())}")

                    sub_pdu_count = 0
                    while i + header_bytes <= data_len:
                        if not self._is_running:
                            break
                        try:
                            # 读取 Header_ID (24位，大端)
                            header_id = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2]
                            header_dlc = data[i + 3]

                            payload_start = i + header_bytes
                            payload_end = payload_start + header_dlc

                            if payload_end > data_len:
                                if msg_id == 0x13E:
                                    print(f"      ⚠️ Payload 超出数据长度: {payload_end} > {data_len}")
                                break

                            payload = data[payload_start:payload_end]
                            sub_pdu_count += 1

                            # ===== 0x13E 子PDU信息打印 =====
                            if msg_id == 0x13E:
                                print(f"\n      📨 子PDU #{sub_pdu_count}:")
                                print(f"         Header_ID: 0x{header_id:06X} ({header_id})")
                                print(f"         Header_DLC: {header_dlc}")
                                print(f"         Payload起始偏移: {payload_start}")
                                print(f"         Payload数据: {payload.hex().upper() if payload else '(空)'}")

                            # ===== 使用该帧对应的 multiplexer 信号映射 =====
                            if header_id in multiplexer_signals:
                                signals = multiplexer_signals[header_id]

                                if msg_id == 0x13E:
                                    print(f"         ✅ 匹配到 {len(signals)} 个信号")
                                    print(f"         信号列表: {[s['name'] for s in signals]}")

                                for sig_info in signals:
                                    try:
                                        raw_value = self.decode_signal(
                                            payload,
                                            sig_info['start'],
                                            sig_info['length'],
                                            sig_info['is_little_endian'],
                                            header_total_bits,
                                            sig_info.get('is_signed', False)
                                        )
                                        if raw_value is not None:
                                            physical_value = raw_value * sig_info['scale'] + sig_info['offset']

                                            # ===== 打印 0x13E 帧的所有解析信号值 =====
                                            if msg_id == 0x13E:
                                                print(f"            {sig_info['name']}:")
                                                print(f"               raw={raw_value}")
                                                print(f"               physical={physical_value:.6f}")
                                                print(f"               unit={sig_info.get('unit', '')}")
                                                print(
                                                    f"               start={sig_info['start']}, length={sig_info['length']}")

                                            pack = pool[sig_info['name']]
                                            append_started = time.perf_counter()
                                            pack['t'].append(rel_t)
                                            pack['v'].append(physical_value)
                                            result_append_seconds += time.perf_counter() - append_started
                                            if not pack['unit']:
                                                pack['unit'] = sig_info['unit']
                                                pack['comment'] = f"ID: 0x{msg_id:X}, Header_ID: 0x{header_id:X}"

                                            total_signals_decoded += 1
                                            msg_ids_with_signals.add(msg_id)
                                    except Exception as e:
                                        if msg_id == 0x13E:
                                            print(f"            ❌ 解码失败: {e}")

                            else:
                                if msg_id == 0x13E and header_id != 0:
                                    print(f"         ⚠️ 未匹配 Header_ID: 0x{header_id:06X}")
                                    print(f"            可用的 Header_ID: {list(multiplexer_signals.keys())}")

                            i = payload_end

                        except Exception as e:
                            if msg_id == 0x13E:
                                print(f"      ⚠️ 解析子PDU失败: {e}")
                            i += 1
                            continue

                    if msg_id == 0x13E:
                        print(f"\n      📊 本帧包含 {sub_pdu_count} 个子PDU")
                        print(f"{'=' * 60}\n")

                    # 打印前10个容器帧的详细信息（每个帧单独计数）
                    if current_count <= 10:
                        print(
                            f"\n🔍 [容器帧 #{current_count}] ID=0x{msg_id:X} ({frame_name}), "
                            f"时间: {rel_t:.3f}s, 数据长度: {data_len}, Header总位数: {header_total_bits}")
                        print(f"   原始数据: {data.hex()}")

                    sub_pdu_count = 0
                    while i + header_bytes <= data_len:
                        if not self._is_running:
                            break
                        try:
                            # 读取 Header_ID (24位，大端)
                            header_id = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2]
                            header_dlc = data[i + 3]

                            payload_start = i + header_bytes
                            payload_end = payload_start + header_dlc

                            if payload_end > data_len:
                                if current_count <= 10:
                                    print(f"   ⚠️ Payload 超出数据长度: {payload_end} > {data_len}")
                                break

                            payload = data[payload_start:payload_end]
                            sub_pdu_count += 1

                            if current_count <= 10:
                                print(
                                    f"   子PDU #{sub_pdu_count}: Header_ID=0x{header_id:06X} ({header_id}), DLC={header_dlc}")
                                print(f"      Payload: {payload.hex() if payload else '(空)'}")

                            # ===== 使用该帧对应的 multiplexer 信号映射 =====
                            if header_id in multiplexer_signals:
                                signals = multiplexer_signals[header_id]

                                if current_count <= 10:
                                    print(f"      ✅ 匹配 Header_ID=0x{header_id:06X} -> {len(signals)} 个信号")
                                    for sig in signals[:3]:
                                        print(f"         - {sig['name']}: start={sig['start']}, len={sig['length']}")
                                    if len(signals) > 3:
                                        print(f"         ... 还有 {len(signals) - 3} 个")

                                for sig_info in signals:
                                    try:
                                        raw_value = self.decode_signal(
                                            payload,
                                            sig_info['start'],
                                            sig_info['length'],
                                            sig_info['is_little_endian'],
                                            header_total_bits,
                                            sig_info.get('is_signed', False)
                                        )
                                        if raw_value is not None:
                                            physical_value = raw_value * sig_info['scale'] + sig_info['offset']
                                            pack = pool[sig_info['name']]
                                            append_started = time.perf_counter()
                                            pack['t'].append(rel_t)
                                            pack['v'].append(physical_value)
                                            result_append_seconds += time.perf_counter() - append_started
                                            if not pack['unit']:
                                                pack['unit'] = sig_info['unit']
                                                pack['comment'] = f"ID: 0x{msg_id:X}, Header_ID: 0x{header_id:X}"

                                            if sig_info['name'] == 'isVehSpdAvg' and current_count <= 10:
                                                print(
                                                    f"      🎯 isVehSpdAvg = {physical_value:.6f} {pack.get('unit', '')}")
                                            elif current_count <= 10:
                                                if len(pool[sig_info['name']]['t']) <= 3:
                                                    print(
                                                        f"         {sig_info['name']} = {physical_value:.6f} {pack.get('unit', '')}")
                                    except Exception as e:
                                        if current_count <= 10:
                                            print(f"      ❌ 解码信号 {sig_info['name']} 失败: {e}")

                                if total_signals_decoded > frame_signals_before:
                                    msg_ids_with_signals.add(msg_id)

                            else:
                                if current_count <= 10 and header_id != 0:
                                    print(f"      ⚠️ 未匹配 Header_ID=0x{header_id:06X} ({header_id})")
                                    print(f"      可用的 Header_ID: {list(multiplexer_signals.keys())[:10]}...")

                            i = payload_end

                        except Exception as e:
                            if current_count <= 10:
                                print(f"   ⚠️ 解析子PDU失败: {e}")
                            i += 1
                            continue

                    if current_count <= 10:
                        print(f"   📊 子PDU总数: {sub_pdu_count}")

                else:
                    # ===== 普通消息：解析所有信号 =====
                    try:
                        if is_truncated:
                            decoded = msg_def.decode(
                                msg.data,
                                decode_choices=False,
                                allow_truncated=True,
                            )
                        else:
                            decoded = msg_def.decode(
                                msg.data,
                                decode_choices=False,
                            )
                        for sig_name, sig_val in decoded.items():
                            if not isinstance(sig_val, (int, float)):
                                continue
                            pack = pool[sig_name]
                            append_started = time.perf_counter()
                            pack['t'].append(rel_t)
                            pack['v'].append(float(sig_val))
                            result_append_seconds += time.perf_counter() - append_started
                            if not pack['unit']:
                                sig_obj = msg_def.get_signal_by_name(sig_name)
                                if sig_obj:
                                    pack['unit'] = getattr(sig_obj, 'unit', '')
                                    pack['comment'] = f"ID: 0x{msg_id:X}"
                        total_signals_decoded += len(decoded)
                        if decoded:
                            msg_ids_with_signals.add(msg_id)
                    except Exception as exc:
                        frame_decode_failed = True
                        decode_failed_frames += 1
                        decode_error_counts[type(exc).__name__] += 1

                if total_signals_decoded > frame_signals_before:
                    decoded_frames += 1
                elif not frame_decode_failed:
                    empty_decoded_frames += 1
                signal_decode_seconds += time.perf_counter() - decode_started

            # ===== 打印统计 =====
            print(f"\n{'=' * 70}")
            print(f"📊 总线 {self.bus_id} 解析统计:")
            print(f"  - 匹配到的消息ID数: {len(matched_msg_ids)}")
            print(f"  - 未匹配的消息ID数: {len(unmatched_msg_ids)}")
            if unmatched_msg_ids:
                print(f"  - 未匹配ID示例: {[hex(i) for i in list(unmatched_msg_ids)[:20]]}")
            print(f"  - 处理的报文总数: {total_messages_processed}")
            print(f"  - 扫描总帧数: {scanned_frames}")
            print(f"  - DBC 匹配帧数: {total_messages_processed}")
            print(f"  - DBC 未定义帧数: {dbc_undefined_frames}")
            print(f"  - 成功解码帧数: {decoded_frames}")
            print(f"  - 解码成功但结果为空帧数: {empty_decoded_frames}")
            print(f"  - truncated/长度不符帧数: {truncated_frames}")
            print(f"  - 解码失败帧数: {decode_failed_frames}")
            if decode_error_counts:
                print(f"  - 解码失败类型汇总: {dict(decode_error_counts)}")
            print(f"  - 容器帧数量: {sum(container_frame_counts.values())}")
            if container_frame_counts:
                print(f"  - 各容器帧计数:")
                for frame_key, count in container_frame_counts.items():
                    frame_id, is_extended = frame_key
                    frame_name = self.container_frames_info[frame_key]['frame_name']
                    frame_kind = "EXT" if is_extended else "STD"
                    print(f"     0x{frame_id:X}/{frame_kind} ({frame_name}): {count} 帧")
            print(f"  - 成功解码的信号值: {total_signals_decoded}")
            print(f"  - 包含信号的CAN ID数: {len(msg_ids_with_signals)}")
            print(f"  - 成功注册的信号通道数: {len(pool)}")

            if pool:
                print(f"\n  📋 解析出的信号列表:")
                for sig_name in sorted(pool.keys()):
                    sig_data = pool[sig_name]
                    print(f"    - {sig_name}: {len(sig_data['t'])} 个样本, unit={sig_data.get('unit', '')}")

                if 'isVehSpdAvg' in pool:
                    print(f"  🎯 isVehSpdAvg 已解析！")
                    data = pool['isVehSpdAvg']
                    print(f"     样本数: {len(data['t'])}")
                    if len(data['t']) > 0:
                        print(f"     前5个值: {[(t, v) for t, v in zip(data['t'][:5], data['v'][:5])]}")
                else:
                    print(f"  ❌ isVehSpdAvg 未解析！")
            else:
                print(f"  ❌ 没有任何信号被解析！")
            print(f"{'=' * 70}\n")

            stats = {
                'messages_processed': total_messages_processed,
                'scanned_frames': scanned_frames,
                'dbc_matched_frames': total_messages_processed,
                'dbc_undefined_frames': dbc_undefined_frames,
                'decoded_frames': decoded_frames,
                'empty_decoded_frames': empty_decoded_frames,
                'truncated_frames': truncated_frames,
                'decode_failed_frames': decode_failed_frames,
                'decode_error_counts': dict(decode_error_counts),
                'signals_decoded': total_signals_decoded,
                'generated_signal_count': len(pool),
                'dbc_message_count': len(self.db.messages),
                'dbc_signal_count': sum(len(message.signals) for message in self.db.messages),
                'msg_ids_with_signals': len(msg_ids_with_signals),
                'matched_msg_ids': len(matched_msg_ids),
                'matched_frames': sorted((int(frame_id), bool(is_extended)) for frame_id, is_extended in matched_frames),
                'unmatched_msg_ids': len(unmatched_msg_ids),
                'container_frame_counts': {
                    frame_id | (0x80000000 if is_extended else 0): count
                    for (frame_id, is_extended), count in container_frame_counts.items()
                },
                'pool': pool,
                'signal_identity': signal_identity,
                'target_frame_match_seconds': frame_match_seconds,
                'signal_decode_seconds': signal_decode_seconds,
                'result_append_seconds': result_append_seconds,
                'generated_sample_count': sum(len(pack['t']) for pack in pool.values()),
            }

            return stats

        except GeneratorExit:
            raise

    def run(self):
        reader = None
        context = None
        scan_collectors = ()
        try:
            if capture_can_file_identity(self.log_path) != self.file_identity:
                raise ValueError("CAN日志在解析启动前发生变化，请重新探测")
            print(f"\n{'=' * 70}")
            print(f"🚀 [Bus {self.bus_id}] 开始解析")
            print(f"   日志文件: {self.log_path}")
            print(f"   数据库消息数: {len(self.db.messages)}")
            print(f"   是否多路复用: {self.is_multiplexer}")
            if self.is_multiplexer:
                print(f"   检测到 {len(self.container_frames_info)} 个容器帧:")
                for _frame_key, info in self.container_frames_info.items():
                    print(f"     0x{info['frame_id']:X} ({info['frame_name']}) - Header总位数: {info['header_total_bits']} bits")
                    print(f"     分组数: {len(info['multiplexer_signals'])}")
            context = ParseContext(self)
            context.start()
            cache_replay = self.raw_message_cache
            if cache_replay is not None and cache_replay.complete:
                frame_keys = {
                    (int(message.frame_id), bool(getattr(message, 'is_extended_frame', False)))
                    for message in self.db.messages
                }
                replay_count = cache_replay.count_frame_keys(frame_keys)
                self.progress_message_total = max(1, replay_count)
                self.progress_updated.emit(self.bus_id, 1, "正在从原始报文缓存增量解析…")
                replay_started = time.perf_counter()
                for msg, file_t0 in cache_replay.iter_messages(frame_keys):
                    if not self._is_running:
                        break
                    context.feed(msg, file_t0)
                replay_seconds = time.perf_counter() - replay_started
                if not self._is_running:
                    context.close()
                    for cache in scan_collectors:
                        cache.reset()
                    self.parse_cancelled.emit(self.bus_id)
                    return
                stats = context.finish()
                stats.update({
                    'cache_hit': True,
                    'cache_replayed_frames': replay_count,
                    'full_file_scan_seconds': 0.0,
                    'cache_replay_seconds': replay_seconds,
                    'raw_cache_frame_count': cache_replay.frame_count,
                    'raw_cache_payload_bytes': cache_replay.payload_bytes,
                    'raw_cache_memory_bytes': cache_replay.memory_bytes,
                })
            else:
                reader = can.BLFReader(self.log_path) if self.log_path.lower().endswith('.blf') else can.LogReader(self.log_path)
                collectors_by_raw = self.raw_cache_collectors or {}
                collectors = tuple({
                    id(cache): cache
                    for group in collectors_by_raw.values()
                    for cache in group
                }.values())
                scan_collectors = collectors
                file_t0 = None
                target_processing_seconds = 0.0
                cache_collection_seconds = 0.0
                scan_started = time.perf_counter()
                for msg in reader:
                    if not self._is_running:
                        break
                    if file_t0 is None:
                        file_t0 = msg.timestamp
                    raw_channel = message_raw_channel(msg)
                    cache_started = time.perf_counter()
                    for cache in collectors_by_raw.get(raw_channel, ()):
                        cache.append(msg, file_t0, raw_channel=raw_channel)
                    cache_collection_seconds += time.perf_counter() - cache_started
                    if raw_channel == self.target_raw_channel:
                        target_started = time.perf_counter()
                        context.feed(msg, file_t0)
                        target_processing_seconds += time.perf_counter() - target_started
                scan_seconds = time.perf_counter() - scan_started
                if not self._is_running:
                    context.close()
                    self.parse_cancelled.emit(self.bus_id)
                    return
                stats = context.finish()
                stats.update({
                    'cache_hit': False,
                    'cache_replayed_frames': 0,
                    'full_file_scan_seconds': scan_seconds,
                    'target_processing_seconds': target_processing_seconds,
                    'cache_collection_seconds': cache_collection_seconds,
                    'raw_cache_frame_count': sum(cache.frame_count for cache in collectors),
                    'raw_cache_payload_bytes': sum(cache.payload_bytes for cache in collectors),
                    'raw_cache_memory_bytes': sum(cache.memory_bytes for cache in collectors),
                })
            if capture_can_file_identity(self.log_path) != self.file_identity:
                raise ValueError("CAN日志在解析期间发生变化，请重新探测")
            if not (cache_replay is not None and cache_replay.complete):
                for cache in scan_collectors:
                    cache.finish_scan(file_t0)
            if not (cache_replay is not None and cache_replay.complete) and stats['scanned_frames'] != self.msg_count:
                raise ValueError(f"Bus {self.bus_id} 扫描计数与探测快照不一致: {stats['scanned_frames']} != {self.msg_count}")
            self.parse_finished.emit(self.bus_id, stats)
        except Exception as e:
            if context is not None:
                context.close()
            for cache in scan_collectors:
                if not cache.complete:
                    cache.reset()
            print(f"❌ [Bus {self.bus_id}] 解析异常: {e}")
            import traceback
            traceback.print_exc()
            self.parse_error.emit(self.bus_id, str(e))
        finally:
            if reader is not None:
                try:
                    reader.stop()
                except Exception:
                    pass


class ParseContext:
    """Incremental per-bus parser state shared by standalone and batch workers."""
    def __init__(self, parser):
        self.parser = parser
        self._stream = parser._parse_stream()
        self._started = False
        self._finished = False

    def start(self):
        if not self._started:
            next(self._stream)
            self._started = True

    def feed(self, message, file_t0):
        if self._finished:
            raise RuntimeError("解析上下文已结束")
        self.start()
        try:
            self._stream.send((message, file_t0))
        except StopIteration as exc:
            self._finished = True
            raise RuntimeError("解析上下文提前结束") from exc

    def finish(self):
        if self._finished:
            raise RuntimeError("解析上下文已结束")
        self.start()
        try:
            self._stream.send(None)
        except StopIteration as exc:
            self._finished = True
            return exc.value
        raise RuntimeError("解析上下文未正常结束")

    def close(self):
        if not self._finished:
            self._stream.close()
            self._finished = True


class MultiBusParseWorker(QThread):
    """Scan one CAN log once and feed each raw channel to its own parser."""

    def __init__(self, parsers, parent=None):
        super().__init__(parent)
        self.parsers = {parser.bus_id: parser for parser in parsers}
        self.contexts = {
            bus_id: ParseContext(parser)
            for bus_id, parser in self.parsers.items()
        }
        self._cancelled_bus_ids = set()
        self.raw_cache_collectors = (
            next(iter(self.parsers.values())).raw_cache_collectors
            if self.parsers else {}
        )

    def cancel_bus(self, bus_id):
        if bus_id in self.parsers:
            self._cancelled_bus_ids.add(bus_id)
            self.parsers[bus_id].stop()

    def stop(self):
        for bus_id in self.parsers:
            self.cancel_bus(bus_id)

    def _emit_error(self, bus_id, error):
        self.parsers[bus_id].parse_error.emit(bus_id, str(error))

    def run(self):
        readers = None
        file_identity = None
        terminal_bus_ids = set()
        try:
            if not self.parsers:
                return
            first_parser = next(iter(self.parsers.values()))
            file_identity = first_parser.file_identity
            for parser in self.parsers.values():
                if parser.file_identity != file_identity or parser.log_path != first_parser.log_path:
                    raise ValueError("批量解析目标必须来自同一文件快照")
            if capture_can_file_identity(first_parser.log_path) != file_identity:
                raise ValueError("CAN日志在解析启动前发生变化，请重新探测")

            for context in self.contexts.values():
                context.start()
            parsers_by_raw_channel = defaultdict(list)
            for bus_id, parser in self.parsers.items():
                parsers_by_raw_channel[parser.target_raw_channel].append(
                    (bus_id, parser)
                )
            reader = (
                can.BLFReader(first_parser.log_path)
                if first_parser.log_path.lower().endswith('.blf')
                else can.LogReader(first_parser.log_path)
            )
            readers = reader
            file_t0 = None
            scanned_frames = {bus_id: 0 for bus_id in self.parsers}
            failed_bus_ids = set()
            messages_seen = 0
            target_processing_seconds = {bus_id: 0.0 for bus_id in self.parsers}
            cache_collection_seconds = 0.0
            scan_started = time.perf_counter()
            scan_completed = False

            for msg in reader:
                messages_seen += 1
                if (
                    messages_seen % 256 == 0
                    and len(self._cancelled_bus_ids | failed_bus_ids)
                    >= len(self.parsers)
                ):
                    break
                if file_t0 is None:
                    file_t0 = msg.timestamp
                raw_channel = message_raw_channel(msg)
                cache_started = time.perf_counter()
                for cache in self.raw_cache_collectors.get(raw_channel, ()):
                    cache.append(msg, file_t0, raw_channel=raw_channel)
                cache_collection_seconds += time.perf_counter() - cache_started
                for bus_id, parser in parsers_by_raw_channel.get(raw_channel, ()):
                    if bus_id in self._cancelled_bus_ids or bus_id in failed_bus_ids:
                        continue
                    scanned_frames[bus_id] += 1
                    try:
                        target_started = time.perf_counter()
                        self.contexts[bus_id].feed(msg, file_t0)
                        target_processing_seconds[bus_id] += time.perf_counter() - target_started
                    except Exception as exc:
                        failed_bus_ids.add(bus_id)
                        self.contexts[bus_id].close()
                        self._emit_error(bus_id, exc)
                        terminal_bus_ids.add(bus_id)
            else:
                scan_completed = True
            scan_seconds = time.perf_counter() - scan_started

            if capture_can_file_identity(first_parser.log_path) != file_identity:
                raise ValueError("CAN日志在解析期间发生变化，请重新探测")
            if scan_completed:
                for group in self.raw_cache_collectors.values():
                    for cache in group:
                        cache.finish_scan(file_t0)
            else:
                for group in self.raw_cache_collectors.values():
                    for cache in group:
                        cache.reset()

            for bus_id, parser in self.parsers.items():
                context = self.contexts[bus_id]
                if bus_id in self._cancelled_bus_ids:
                    context.close()
                    parser.parse_cancelled.emit(bus_id)
                    terminal_bus_ids.add(bus_id)
                    continue
                if bus_id in failed_bus_ids:
                    continue
                if scanned_frames[bus_id] != parser.msg_count:
                    context.close()
                    self._emit_error(
                        bus_id,
                        "扫描计数与探测快照不一致: "
                        f"{scanned_frames[bus_id]} != {parser.msg_count}",
                    )
                    terminal_bus_ids.add(bus_id)
                    continue
                try:
                    stats = context.finish()
                except Exception as exc:
                    context.close()
                    self._emit_error(bus_id, exc)
                    terminal_bus_ids.add(bus_id)
                    continue
                stats.update({
                    'cache_hit': False,
                    'cache_replayed_frames': 0,
                    'full_file_scan_seconds': scan_seconds,
                    'target_processing_seconds': target_processing_seconds[bus_id],
                    'cache_collection_seconds': cache_collection_seconds,
                    'raw_cache_frame_count': sum(
                        cache.frame_count
                        for group in self.raw_cache_collectors.values()
                        for cache in group
                    ),
                    'raw_cache_payload_bytes': sum(
                        cache.payload_bytes
                        for group in self.raw_cache_collectors.values()
                        for cache in group
                    ),
                    'raw_cache_memory_bytes': sum(
                        cache.memory_bytes
                        for group in self.raw_cache_collectors.values()
                        for cache in group
                    ),
                })
                parser.parse_finished.emit(bus_id, stats)
                terminal_bus_ids.add(bus_id)

        except Exception as exc:
            for group in self.raw_cache_collectors.values():
                for cache in group:
                    if not cache.complete:
                        cache.reset()
            for bus_id, parser in self.parsers.items():
                if bus_id in terminal_bus_ids:
                    continue
                context = self.contexts[bus_id]
                context.close()
                if bus_id in self._cancelled_bus_ids:
                    parser.parse_cancelled.emit(bus_id)
                else:
                    self._emit_error(bus_id, exc)
                terminal_bus_ids.add(bus_id)
        finally:
            if readers is not None:
                try:
                    readers.stop()
                except Exception:
                    pass


class MDFPlotter(QWidget):
    def __init__(self):
        super().__init__()
        self.mdf_path = ""
        self.signals = {}
        self.signal_widgets = {}
        self.mdf_file = None
        self.plot_widgets = []
        self.master_viewbox = None
        self._overlay_y_axis_state = {}
        self._overlay_current_signal_key = None
        self._overlay_axis_scroll_position = 0
        self.map_dialog = None
        self.web_view = None
        self.gps_df = None
        self._temp_map_dir = None
        self.cursor2_pos = None
        self.cursor_mode = None
        self._single_cursor_time = None
        self._dragging_cursor = None
        self._cursor_a_time = None
        self._cursor_b_time = None
        self._arxml_converter = None

        self.custom_math_data = {}
        self.can_parsed_data = {}

        self.can_bus_data = {}
        self.can_bus_signals = {}
        self.bus_signal_map = {}
        self.db_per_bus = {}
        self.bus_protocols = {}
        self.bus_config_widgets = {}
        self.bus_loaded = False

        self.rescale_timer = None

        self.init_ui()

        self.parse_workers = {}
        self._cancelled_parse_buses = set()
        self.auto_parse_enabled = False

        self.search_index = {}
        self.search_timer = None
        self._search_pending_text = ""
        self._parsing_bus_ids = set()
        self._parse_generation = 0
        self._pending_signal_config = None
        self._reusable_can_selection = {}

        self.progress_dialogs = {}

    def init_ui(self):
        self.setWindowTitle('VET_DATA_v26.08.28')
        self.setGeometry(100, 100, 1450, 950)
        self._ribbon_action_mirrors = {}

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # Keep the ribbon tabs and existing shortcut controls on one row.
        self.ribbon_tabs = QTabWidget()
        self.ribbon_tabs.setObjectName("mainFunctionalTabs")
        self.ribbon_tabs.setDocumentMode(True)
        self.ribbon_tabs.setMaximumHeight(82)

        def add_ribbon_page(title):
            page = QWidget()
            layout = QHBoxLayout(page)
            layout.setContentsMargins(8, 4, 8, 4)
            layout.setSpacing(6)
            layout.addStretch(1)
            self.ribbon_tabs.addTab(page, title)
            return page, layout

        self.file_ribbon_page, self.file_ribbon_layout = add_ribbon_page("文件")
        self.signal_ribbon_page, self.signal_ribbon_layout = add_ribbon_page("信号管理")
        self.curve_ribbon_page, self.curve_ribbon_layout = add_ribbon_page("曲线显示")
        self.analysis_ribbon_page, self.analysis_ribbon_layout = add_ribbon_page("分析工具")
        self.processing_ribbon_page, self.processing_ribbon_layout = add_ribbon_page("数据处理")
        self.export_ribbon_page, self.export_ribbon_layout = add_ribbon_page("导出")
        self.view_ribbon_page, self.view_ribbon_layout = add_ribbon_page("视图")

        self.quick_actions_widget = QWidget()
        self.quick_actions_layout = QHBoxLayout(self.quick_actions_widget)
        self.quick_actions_layout.setContentsMargins(4, 0, 4, 0)
        self.quick_actions_layout.setSpacing(6)
        self.quick_actions_widget.setObjectName("mainQuickActions")
        self.ribbon_widget = _AdaptiveRibbonWidget(
            self.ribbon_tabs, self.quick_actions_widget
        )
        main_layout.addWidget(self.ribbon_widget)

        # ====== 左侧面板 ======
        self.left_panel_widget = QWidget()
        left_panel_shell = QVBoxLayout(self.left_panel_widget)
        left_panel_shell.setContentsMargins(0, 0, 0, 0)
        left_panel_shell.setSpacing(0)

        self.left_panel_scroll = QScrollArea()
        self.left_panel_scroll.setObjectName("mainLeftPanelScrollArea")
        self.left_panel_scroll.setWidgetResizable(True)
        self.left_panel_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.left_panel_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.left_panel_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        self.left_panel_content = QWidget()
        self.left_panel_content.setObjectName("mainLeftPanelContent")
        left_panel = QVBoxLayout(self.left_panel_content)
        left_panel.setContentsMargins(5, 5, 5, 5)
        self.left_panel_scroll.setWidget(self.left_panel_content)
        left_panel_shell.addWidget(self.left_panel_scroll)

        self.current_file_label = ElidedLabel("未选择数据文件")
        self.current_file_label.setMinimumWidth(140)
        self.current_file_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.current_file_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        # Keep the active file visible at the top of the left management panel.
        # The ribbon copy remains for the compact file actions row.
        self.left_current_file_label = ElidedLabel("当前文件: 未选择")
        self.left_current_file_label.setObjectName("leftCurrentFileLabel")
        self.left_current_file_label.setProperty("uiTextRole", "secondary")
        self.left_current_file_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.left_current_file_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.left_current_file_label.setMinimumHeight(28)
        self.left_current_file_label.setContentsMargins(8, 2, 8, 2)
        self.left_current_file_label.setToolTip("当前未选择数据文件")
        left_panel_shell.insertWidget(0, self.left_current_file_label)
        self.load_file_button = QPushButton("打开文件")
        self.load_file_button.setText("")
        self.load_file_button.setToolTip("打开文件")
        self._add_ribbon_mirror(
            self.file_ribbon_layout, "打开数据文件…", self.load_file_button,
            self.load_file_dialog,
        )

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("搜索信号...")

        self.select_all_button = QPushButton("全选")
        self.deselect_all_button = QPushButton("全部取消")
        self.save_config_button = QPushButton("保存配置")
        self.load_config_button = QPushButton("加载配置")
        self.signal_ribbon_layout.insertWidget(0, self.select_all_button)
        self.signal_ribbon_layout.insertWidget(1, self.deselect_all_button)
        self.file_ribbon_layout.insertWidget(1, self.save_config_button)
        self.file_ribbon_layout.insertWidget(2, self.load_config_button)
        self.current_file_status = QWidget(self.file_ribbon_page)
        current_file_status_layout = QHBoxLayout(self.current_file_status)
        current_file_status_layout.setContentsMargins(0, 0, 0, 0)
        current_file_status_layout.addStretch(1)
        current_file_status_layout.addWidget(self.current_file_label)
        self.file_ribbon_layout.takeAt(self.file_ribbon_layout.count() - 1)
        self.file_ribbon_layout.addWidget(self.current_file_status, 1)

        self.math_channel_button = QPushButton("自定义公式计算")
        self.math_channel_button.setText("")
        self.math_channel_button.setToolTip("自定义公式计算")

        self.plot_button = QPushButton("绘制曲线")
        self.plot_button.setText("")
        self.plot_button.setToolTip("绘制曲线")
        self.export_button = QPushButton("导出为CSV")
        self.save_data_button = QPushButton("数据截取")
        self.save_data_button.setToolTip("截取当前曲线视图显示的时间段，另存为 BLF、MDF4 或 CSV")
        self._add_ribbon_mirror(
            self.curve_ribbon_layout, "绘制所选信号",
            self.plot_button, self.plot_selected_signals,
        )
        self.export_ribbon_layout.insertWidget(0, self.export_button)
        self.processing_ribbon_layout.insertWidget(0, self.save_data_button)

        self.single_cursor_button = QPushButton("单游标")
        self.single_cursor_button.setText("")
        self.single_cursor_button.setObjectName("singleCursorToggle")
        self.single_cursor_button.setCheckable(True)
        self.single_cursor_button.setProperty("uiRole", "stateToggle")
        self.single_cursor_button.setToolTip("单游标")
        self.dual_cursor_button = QPushButton("双游标")
        self.dual_cursor_button.setText("")
        self.dual_cursor_button.setObjectName("dualCursorToggle")
        self.dual_cursor_button.setCheckable(True)
        self.dual_cursor_button.setProperty("uiRole", "stateToggle")
        self.dual_cursor_button.setToolTip("双游标")
        self.load_file_button.setIcon(_quick_action_icon("open", DEFAULT_THEME.colors.text_secondary))
        self.plot_button.setIcon(_quick_action_icon("plot", DEFAULT_THEME.colors.text_secondary))
        self.math_channel_button.setIcon(_quick_action_icon("formula", DEFAULT_THEME.colors.text_secondary))
        self.single_cursor_button.setIcon(_quick_action_icon("single", DEFAULT_THEME.colors.success))
        self.dual_cursor_button.setIcon(_quick_action_icon("dual", DEFAULT_THEME.colors.primary))
        for shortcut_button in (
            self.load_file_button, self.plot_button, self.math_channel_button,
            self.single_cursor_button, self.dual_cursor_button,
        ):
            shortcut_button.setIconSize(QSize(18, 18))
        for shortcut_button in (
            self.load_file_button, self.plot_button, self.math_channel_button,
            self.single_cursor_button, self.dual_cursor_button,
        ):
            shortcut_button.setMinimumWidth(32)

        self.plot_mode_combo = QComboBox()
        self.plot_mode_combo.setObjectName("curveDisplayModeSelector")
        self.plot_mode_combo.addItem("单信号独立图表", "independent")
        self.plot_mode_combo.addItem("多信号叠加到同一图表", "overlay")
        self.plot_mode_combo.setToolTip("选择每个信号单独成图，或将所选信号叠加显示")
        self.plot_mode_combo.setProperty("uiControl", "main")
        self.plot_mode_combo.setMinimumWidth(130)

        self.quick_actions_layout.addWidget(self.load_file_button)
        self.quick_actions_layout.addWidget(self.plot_button)
        self.quick_actions_layout.addWidget(self.math_channel_button)
        self._add_ribbon_mirror(
            self.analysis_ribbon_layout, "自定义公式计算…",
            self.math_channel_button, self.create_math_channel,
        )
        self.quick_actions_layout.addWidget(self.single_cursor_button)
        self.quick_actions_layout.addWidget(self.dual_cursor_button)
        self.quick_actions_layout.addWidget(self.plot_mode_combo)

        # ====== 可用信号区域 ======
        signal_groupbox = QGroupBox("可用信号")
        signal_layout_outer = QVBoxLayout()
        signal_groupbox.setLayout(signal_layout_outer)
        signal_layout_outer.addWidget(self.search_box)

        self.signal_container = QWidget()
        self.signal_layout = QVBoxLayout(self.signal_container)
        self.signal_layout.setSpacing(0)
        self.signal_layout.setContentsMargins(0, 0, 0, 0)

        scroll_area_left = QScrollArea()
        scroll_area_left.setWidgetResizable(True)
        scroll_area_left.setWidget(self.signal_container)
        scroll_area_left.setMinimumHeight(200)
        scroll_area_left.setStyleSheet("""
            QScrollArea {
                border: none;
                background-color: transparent;
            }
            QScrollBar:vertical {
                width: 8px;
            }
        """)

        signal_layout_outer.addWidget(scroll_area_left)

        # ====== CAN通道配置区域 ======
        bus_groupbox = QGroupBox("CAN通道配置")
        bus_layout = QVBoxLayout()
        bus_layout.setSpacing(8)
        self.bus_header = None

        self.bus_scroll = QScrollArea()
        self.bus_scroll.setObjectName("busConfigScrollArea")
        self.bus_scroll.setWidgetResizable(True)
        self.bus_scroll.setMinimumHeight(150)
        self.bus_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.bus_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self.bus_container = QWidget()
        self.bus_container_layout = QVBoxLayout()
        self.bus_container_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.bus_container_layout.setSpacing(2)
        self.bus_container_layout.setContentsMargins(0, 0, 0, 0)
        self.bus_container.setLayout(self.bus_container_layout)
        self.bus_scroll.setWidget(self.bus_container)
        bus_layout.addWidget(self.bus_scroll)

        # ARXML2DBC 按钮
        self.arxml2dbc_btn = QPushButton("🔧 ARXML2DBC")
        self.arxml2dbc_btn.setProperty("uiToolEntry", "true")
        self.arxml2dbc_btn.setToolTip("启动 ARXML 转 DBC 工具")
        self.arxml2dbc_btn.clicked.connect(self.open_arxml_converter)
        self.processing_ribbon_layout.insertWidget(1, self.arxml2dbc_btn)

        bus_groupbox.setLayout(bus_layout)

        # ====== GPS controls in the View ribbon ======
        gps_controls = QWidget(self.view_ribbon_page)
        self.gps_controls_widget = gps_controls
        map_layout = QHBoxLayout(gps_controls)
        map_layout.setContentsMargins(0, 0, 0, 0)
        lat_layout = QHBoxLayout()
        lat_label = QLabel("纬度信号:")
        self.lat_combo = QComboBox()
        lat_layout.addWidget(lat_label)
        lat_layout.addWidget(self.lat_combo)
        lon_layout = QHBoxLayout()
        lon_label = QLabel("经度信号:")
        self.lon_combo = QComboBox()
        lon_layout.addWidget(lon_label)
        lon_layout.addWidget(self.lon_combo)
        self.show_map_button = QPushButton("显示GPS轨迹")
        self.show_map_button.setObjectName("gpsViewToggle")
        self.show_map_button.setCheckable(True)
        self.show_map_button.setChecked(False)
        self.show_map_button.setProperty("uiRole", "stateToggle")
        self.show_map_button.setToolTip("显示或隐藏 GPS 轨迹窗口")
        map_layout.addLayout(lat_layout)
        map_layout.addLayout(lon_layout)
        map_layout.addWidget(self.show_map_button)
        self.view_ribbon_layout.insertWidget(0, gps_controls)

        # ====== 左侧信号与 CAN 三选项卡 ======
        self.left_tabs = QTabWidget()
        self.left_tabs.setObjectName("leftManagementTabs")
        self.available_signals_page = signal_groupbox
        self.selected_signals_page = QWidget()
        self.selected_signals_layout = QVBoxLayout(self.selected_signals_page)
        self.selected_signals_layout.setContentsMargins(6, 6, 6, 6)
        self.can_tab_page = bus_groupbox
        self.left_tabs.addTab(self.available_signals_page, "可用信号")
        self.left_tabs.addTab(self.selected_signals_page, "已选信号")
        self.left_tabs.addTab(self.can_tab_page, "CAN 配置")

        left_panel.addWidget(self.left_tabs, 1)

        # ====== 右侧面板 ======
        right_panel = QGroupBox("信号曲线")
        right_panel_layout = QVBoxLayout(right_panel)
        self.plot_container = QWidget()
        self.plot_layout = QVBoxLayout(self.plot_container)
        self.plot_layout.setContentsMargins(9, 9, 9, 9)
        self.plot_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll_area_right = QScrollArea()
        self.plot_scroll_area = scroll_area_right
        scroll_area_right.setWidgetResizable(True)
        scroll_area_right.setWidget(self.plot_container)
        right_panel_layout.addWidget(scroll_area_right)
        self.shared_time_axis_widget = pg.PlotWidget()
        self.shared_time_axis_widget.setObjectName("sharedTimeAxis")
        self.shared_time_axis_widget.setMinimumHeight(48)
        self.shared_time_axis_widget.setMaximumHeight(48)
        self.shared_time_axis_widget.setMenuEnabled(False)
        self.shared_time_axis_widget.showAxis("left")
        self.shared_time_axis_widget.hideAxis("right")
        self.shared_time_axis_widget.hideAxis("top")
        self.shared_time_axis_widget.setLabel("bottom", "时间", units="s")
        shared_left_axis = self.shared_time_axis_widget.getPlotItem().getAxis("left")
        shared_left_axis.setStyle(showValues=False, tickLength=0, autoExpandTextSpace=False)
        shared_left_axis.setPen(None)
        shared_left_axis.setTextPen(None)
        self.shared_time_axis_widget.getPlotItem().layout.setContentsMargins(2, 2, 2, 2)
        shared_viewbox = self.shared_time_axis_widget.getViewBox()
        shared_viewbox.setMouseEnabled(x=True, y=False)
        shared_viewbox.setMouseMode(shared_viewbox.PanMode)
        self.shared_time_axis_widget.setVisible(False)
        self.shared_time_axis_row = QWidget(right_panel)
        self.shared_time_axis_row.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.shared_time_axis_row_layout = QHBoxLayout(self.shared_time_axis_row)
        shared_left_inset = scroll_area_right.frameWidth() + self.plot_layout.contentsMargins().left()
        self.shared_time_axis_row_layout.setContentsMargins(shared_left_inset, 0, 0, 0)
        self.shared_time_axis_row_layout.setSpacing(0)
        self.shared_time_axis_row_layout.addWidget(self.shared_time_axis_widget)
        self.shared_time_axis_row_layout.addStretch(1)
        right_panel_layout.addWidget(self.shared_time_axis_row)

        self.left_panel_widget.setMinimumWidth(320)
        right_panel.setMinimumWidth(480)
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(8)
        self.main_splitter.addWidget(self.left_panel_widget)
        self.main_splitter.addWidget(right_panel)
        self.main_splitter.setSizes([360, 1080])
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.handle(1).setCursor(Qt.CursorShape.SplitHCursor)
        main_layout.addWidget(self.main_splitter, 1)

        # ====== 信号连接 ======
        self.load_file_button.clicked.connect(self.load_file_dialog)
        self.plot_button.clicked.connect(self.plot_selected_signals)
        self.export_button.clicked.connect(self.export_selected_signals_to_csv)
        self.save_data_button.clicked.connect(self.save_data_cutout)
        self.select_all_button.clicked.connect(self.select_all_signals)
        self.deselect_all_button.clicked.connect(self.deselect_all_signals)
        self.search_box.textChanged.connect(self.search_signals)
        self.save_config_button.clicked.connect(self.save_signal_config)
        self.load_config_button.clicked.connect(self.load_signal_config)
        self.show_map_button.toggled.connect(self._toggle_gps_view)
        self.math_channel_button.clicked.connect(self.create_math_channel)

        self.set_buttons_enabled(False)

    def _add_ribbon_mirror(self, layout, text, source, callback):
        """Add a category-page copy of an action while sharing enabled state."""
        button = QPushButton(text)
        button.setProperty("uiRole", "secondary")
        button.clicked.connect(callback)
        insert_at = max(0, layout.count() - 1)  # Keep the trailing stretch last.
        layout.insertWidget(insert_at, button)
        self._ribbon_action_mirrors.setdefault(source, []).append(button)
        return button

    def set_current_file_status(self, path=None):
        if not path:
            self.current_file_label.setText("当前文件: 未选择")
            self.current_file_label.setToolTip("")
            left_label = getattr(self, "left_current_file_label", None)
            if left_label is not None:
                left_label.setText("当前文件: 未选择")
                left_label.setToolTip("")
            return
        path = os.fspath(path)
        display_name = f"当前文件: {os.path.basename(path)}"
        self.current_file_label.setText(display_name)
        self.current_file_label.setToolTip(path)
        left_label = getattr(self, "left_current_file_label", None)
        if left_label is not None:
            left_label.setText(display_name)
            left_label.setToolTip(path)

    def _toggle_gps_view(self, visible):
        self.show_map_button.setText("隐藏GPS轨迹" if visible else "显示GPS轨迹")
        if visible:
            if not self.show_map():
                blocker = QSignalBlocker(self.show_map_button)
                self.show_map_button.setChecked(False)
                del blocker
                self.show_map_button.setText("显示GPS轨迹")
        elif self.map_dialog is not None and self.map_dialog.isVisible():
            self.map_dialog.close()

    def _gps_dialog_finished(self, *_args):
        if self.show_map_button.isChecked():
            blocker = QSignalBlocker(self.show_map_button)
            self.show_map_button.setChecked(False)
            del blocker
        self.show_map_button.setText("显示GPS轨迹")

    def _sync_ribbon_action_mirrors(self):
        for source, mirrors in getattr(self, "_ribbon_action_mirrors", {}).items():
            for button in mirrors:
                button.setEnabled(source.isEnabled())

    def auto_parse_all_buses(self):
        if getattr(self, '_auto_parse_suppressed_for_restore', False):
            self._auto_parse_suppressed_for_restore = False
            return
        if not self.bus_protocols:
            return

        if not hasattr(self, 'auto_parse_enabled') or not self.auto_parse_enabled:
            return

        for bus_id, protocol_path in self.bus_protocols.items():
            if bus_id in self.db_per_bus and self.db_per_bus[bus_id].get('parsed', False):
                continue

            QTimer.singleShot(500 * bus_id, lambda b=bus_id: self.start_parse_with_progress(b))

    def save_config(self):
        if not self.lat_combo.currentText() or not self.lon_combo.currentText():
            return
        config = {
            'lat_signal': self.lat_combo.currentText(),
            'lon_signal': self.lon_combo.currentText()
        }
        try:
            with open('map_config.json', 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"保存配置失败: {e}")

    def load_config(self):
        if os.path.exists('map_config.json'):
            try:
                with open('map_config.json', 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    saved_lat = config.get('lat_signal', '')
                    saved_lon = config.get('lon_signal', '')

                    try:
                        self.lat_combo.currentIndexChanged.disconnect()
                        self.lon_combo.currentIndexChanged.disconnect()
                    except Exception:
                        pass

                    if saved_lat:
                        lat_idx = self.lat_combo.findText(saved_lat)
                        if lat_idx >= 0:
                            self.lat_combo.setCurrentIndex(lat_idx)
                    if saved_lon:
                        lon_idx = self.lon_combo.findText(saved_lon)
                        if lon_idx >= 0:
                            self.lon_combo.setCurrentIndex(lon_idx)
            except Exception as e:
                print(f"恢复地图配置失败: {e}")

        self.lat_combo.currentIndexChanged.connect(self.save_config)
        self.lon_combo.currentIndexChanged.connect(self.save_config)

    def refresh_signal_list_ui(self):
        """刷新信号列表UI - 统计信息置顶，信号列表紧凑"""
        # 保存当前选中状态
        checked_keys = {
            unique_key: widgets['checkbox'].isChecked()
            for unique_key, widgets in self.signal_widgets.items()
            if widgets.get('checkbox') is not None
        }

        # 清空布局
        for i in reversed(range(self.signal_layout.count())):
            item = self.signal_layout.itemAt(i)
            if item:
                widget = item.widget()
                if widget:
                    widget.setParent(None)
                    widget.deleteLater()

        self.signal_widgets.clear()

        saved_lat_text = self.lat_combo.currentText() if self.lat_combo.count() > 0 else ""
        saved_lon_text = self.lon_combo.currentText() if self.lon_combo.count() > 0 else ""

        try:
            self.lat_combo.currentIndexChanged.disconnect()
            self.lon_combo.currentIndexChanged.disconnect()
        except Exception:
            pass

        self.lat_combo.clear()
        self.lon_combo.clear()

        if not self.signals:
            empty_label = QLabel("没有可用信号，请加载数据文件")
            empty_label.setStyleSheet("color: #95a5a6; padding: 10px; font-size: 12px;")
            empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.signal_layout.addWidget(empty_label)
            self.set_buttons_enabled(False)
            return

        # ===== 统计信息 - 固定在顶部 =====
        stats_label = QLabel()
        total_signals = len(self.signals)
        can_signals = len([k for k in self.signals.keys() if k.startswith('CAN_')])
        math_signals = len([k for k in self.signals.keys() if k.startswith('MATH_')])
        mdf_signals = total_signals - can_signals - math_signals

        bus_stats = {}
        for key, info in self.signals.items():
            bus_id = info.get('bus_id')
            if bus_id is not None and key.startswith('CAN_'):
                bus_name = self.can_bus_data.get(bus_id, {}).get('name', f"Bus {bus_id}")
                bus_stats[bus_name] = bus_stats.get(bus_name, 0) + 1

        stats_parts = [f"📊 信号总数: {total_signals}"]
        if mdf_signals > 0:
            stats_parts.append(f"数据信号: {mdf_signals}")
        if can_signals > 0:
            stats_parts.append(f"CAN解析: {can_signals}")
        if math_signals > 0:
            stats_parts.append(f"计算通道: {math_signals}")

        stats_text = "  |  ".join(stats_parts)
        if bus_stats:
            bus_info = "  |  ".join([f"🚌 {name}: {count}" for name, count in bus_stats.items()])
            stats_text += f"\n{bus_info}"

        stats_label.setText(stats_text)
        stats_label.setStyleSheet("""
            color: #2c3e50; 
            font-size: 10px; 
            padding: 4px 6px; 
            background-color: #ecf0f1; 
            border-radius: 3px;
            font-weight: bold;
        """)
        stats_label.setWordWrap(True)
        stats_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.signal_layout.addWidget(stats_label)

        # ===== 信号列表容器（可滚动部分） =====
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setSpacing(0)
        scroll_layout.setContentsMargins(0, 2, 0, 2)

        signal_layout = QVBoxLayout()
        signal_layout.setSpacing(0)
        signal_layout.setContentsMargins(0, 0, 0, 0)

        def sort_key(k):
            signal_info = self.signals.get(k, {})
            if k.startswith('MATH_'):
                priority = 0
            elif k.startswith('CAN_'):
                priority = 1
            else:
                priority = 2
            display_name = signal_info.get('display_name', k)
            return (priority, display_name)

        sorted_keys = sorted(self.signals.keys(), key=sort_key)

        existing_lat_items = set()
        existing_lon_items = set()

        for unique_key in sorted_keys:
            signal_info = self.signals.get(unique_key, {})
            display_text = signal_info.get('display_name', unique_key)

            bus_id = signal_info.get('bus_id')
            if bus_id is not None and '[Bus' not in display_text:
                bus_name = self.can_bus_data.get(bus_id, {}).get('name', f"Bus {bus_id}")
                display_text = f"{display_text} [🚌 {bus_name}]"

            item_widget = QWidget()
            item_widget.setStyleSheet("""
                QWidget {
                    margin: 0px;
                    padding: 0px;
                }
            """)
            item_layout = QHBoxLayout(item_widget)
            item_layout.setContentsMargins(2, 1, 2, 1)
            item_layout.setSpacing(4)

            checkbox = QCheckBox(display_text)
            checkbox.setObjectName(unique_key)
            checkbox.setToolTip(signal_info.get('comment', ''))
            checkbox.setStyleSheet("""
                QCheckBox {
                    spacing: 3px;
                    font-size: 10px;
                    padding: 1px 0px;
                    margin: 0px;
                }
                QCheckBox::indicator {
                    width: 12px;
                    height: 12px;
                }
            """)

            if unique_key in checked_keys and checked_keys[unique_key]:
                checkbox.setChecked(True)

            comment = signal_info.get('comment', '')
            comment_label = QLabel(comment)
            comment_label.setStyleSheet("color: #7f8c8d; font-size: 8px; padding: 0px; margin: 0px;")
            comment_label.setWordWrap(False)
            comment_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            self.signal_widgets[unique_key] = {
                'checkbox': checkbox,
                'comment_label': comment_label
            }

            item_layout.addWidget(checkbox, 3)
            item_layout.addWidget(comment_label, 1)
            signal_layout.addWidget(item_widget)

            if unique_key not in existing_lat_items:
                self.lat_combo.addItem(display_text, unique_key)
                existing_lat_items.add(unique_key)
            if unique_key not in existing_lon_items:
                self.lon_combo.addItem(display_text, unique_key)
                existing_lon_items.add(unique_key)

        scroll_layout.addLayout(signal_layout)
        scroll_layout.addStretch()
        self.signal_layout.addWidget(scroll_widget)

        if saved_lat_text:
            lat_idx = self.lat_combo.findText(saved_lat_text)
            if lat_idx >= 0:
                self.lat_combo.setCurrentIndex(lat_idx)
        if saved_lon_text:
            lon_idx = self.lon_combo.findText(saved_lon_text)
            if lon_idx >= 0:
                self.lon_combo.setCurrentIndex(lon_idx)

        self.lat_combo.currentIndexChanged.connect(self.save_config)
        self.lon_combo.currentIndexChanged.connect(self.save_config)

        self.search_signals(self.search_box.text())

        self.set_buttons_enabled(True)
        self._build_search_index()

    def open_arxml_converter(self):
        """打开 ARXML 转 DBC 工具 - 作为内部对话框"""
        try:
            # 检查是否已有实例，如果没有则创建
            if not hasattr(self, '_arxml_converter') or self._arxml_converter is None:
                self._arxml_converter = ARXMLConverterDialog(self)
                self._arxml_converter.setWindowFlags(
                    Qt.WindowType.Window |
                    Qt.WindowType.WindowCloseButtonHint |
                    Qt.WindowType.WindowMinimizeButtonHint
                )

            # 显示窗口
            if self._arxml_converter.isMinimized():
                self._arxml_converter.showNormal()
            else:
                self._arxml_converter.show()
            self._arxml_converter.raise_()
            self._arxml_converter.activateWindow()

        except Exception as e:
            QMessageBox.warning(self, "启动失败", f"启动 ARXML 转换工具失败: {e}")

    def set_buttons_enabled(self, enabled):
        self.plot_button.setEnabled(enabled)
        self.export_button.setEnabled(enabled)
        self.save_data_button.setEnabled(enabled)
        self.select_all_button.setEnabled(enabled)
        self.deselect_all_button.setEnabled(enabled)
        self.save_config_button.setEnabled(enabled)
        self.load_config_button.setEnabled(enabled)
        self.show_map_button.setEnabled(enabled)
        self.math_channel_button.setEnabled(enabled)
        self.lat_combo.setEnabled(enabled)
        self.lon_combo.setEnabled(enabled)
        self._sync_ribbon_action_mirrors()

    def clear_signal_list(self):
        for i in reversed(range(self.signal_layout.count())):
            item = self.signal_layout.itemAt(i)
            if item:
                widget = item.widget()
                if widget:
                    widget.setParent(None)
                    widget.deleteLater()

        self.signal_widgets.clear()

    def clear_bus_config(self):
        while self.bus_container_layout.count():
            item = self.bus_container_layout.takeAt(0)
            if item:
                widget = item.widget()
                if widget:
                    widget.setParent(None)
                    widget.deleteLater()

        self.bus_config_widgets.clear()

    def load_file_dialog(self):
        file_name, _ = QFileDialog.getOpenFileName(
            self, "选择数据文件", "",
            "All Supported Files (*.mdf *.mf4 *.csv *.vbo *.blf *.asc *.trc);;MDF/MF4 Files (*.mdf *.mf4);;CSV Files (*.csv);;VBO Files (*.vbo);;CAN Log Files (*.blf *.asc *.trc);;All Files (*)"
        )
        if file_name:
            self._clear_cursor_positions_for_data_change()
            if self.can_parsed_data:
                self._reusable_can_selection = self._capture_can_selection()
            self._parse_generation += 1
            self._clear_current_can_results()
            # 清理绘图
            if hasattr(self, 'plot_layout') and self.plot_layout is not None:
                while self.plot_layout.count():
                    item = self.plot_layout.takeAt(0)
                    widget = item.widget()
                    if widget:
                        widget.setParent(None)
                        widget.deleteLater()
            self.plot_widgets.clear()
            self.master_viewbox = None

            if hasattr(self, 'map_dialog') and self.map_dialog:
                self.map_dialog.close()
            if self.show_map_button.isChecked():
                self.show_map_button.setChecked(False)

            self.gps_df = None
            self.custom_math_data.clear()
            self.can_parsed_data.clear()
            self.can_bus_data.clear()
            self.can_bus_signals.clear()
            self.bus_signal_map.clear()
            self.db_per_bus.clear()
            self.bus_protocols.clear()
            self.bus_config_widgets.clear()
            self.signals.clear()
            self.mdf_path = file_name
            self.set_current_file_status(file_name)

            self.clear_bus_config()

            for bus_id, worker in self.parse_workers.items():
                if worker.isRunning():
                    worker.stop()
                    worker.wait()
            self.parse_workers.clear()

            # 关闭所有进度对话框
            for bus_id, dialog in self.progress_dialogs.items():
                try:
                    dialog.close()
                    dialog.deleteLater()
                except Exception:
                    pass
            self.progress_dialogs.clear()

            file_extension = os.path.splitext(file_name)[1].lower()
            if file_extension in ('.blf', '.asc', '.trc'):
                self.load_can_bus_info(file_name)
                self.create_bus_config_ui()
            else:
                self.load_signals()

    def _capture_can_selection(self):
        selected = set(self.get_selected_signals())
        saved = {}
        if hasattr(self, '_selected_can_config'):
            self._sync_selected_can_config()
            saved.update({
                record['key']: record.get('identity')
                for record in self._selected_can_config.values()
            })
        for key in selected:
            info = self.signals.get(key, {})
            data = self.can_parsed_data.get(key, {})
            bus_id = info.get('bus_id', data.get('bus_id'))
            if bus_id is None:
                continue
            identity = info.get('signal_identity') or data.get('signal_identity')
            saved.setdefault(key, identity)
        return saved

    def _clear_current_can_results(self):
        """Invalidate file-bound CAN samples and visible curves immediately."""
        self._clear_cursor_positions_for_data_change()
        getattr(self, '_can_raw_message_cache', {}).clear()
        for bus_id, worker in tuple(getattr(self, 'parse_workers', {}).items()):
            try:
                if worker.isRunning():
                    worker.stop()
                    worker.wait()
            except RuntimeError:
                pass
        getattr(self, 'parse_workers', {}).clear()
        getattr(self, '_parsing_bus_ids', set()).clear()
        self.can_parsed_data.clear()
        self.bus_signal_map.clear()
        self.custom_math_data.clear()
        if hasattr(self, "clear_signal_curve_visibility"):
            self.clear_signal_curve_visibility()
        self.signals.clear()
        self._sig_value_cache.clear()
        self.clear_bus_config()
        for dialog in tuple(getattr(self, 'progress_dialogs', {}).values()):
            try:
                dialog.close()
                dialog.deleteLater()
            except RuntimeError:
                pass
        getattr(self, 'progress_dialogs', {}).clear()
        self._close_overlay_y_axis_managers(preserve_state=False)
        for plot in self.plot_widgets:
            self._delete_plot_widget(plot)
        self.plot_widgets.clear()
        self.master_viewbox = None
        if hasattr(self, 'plot_layout') and self.plot_layout is not None:
            self._set_plot_empty_state("新文件尚未解析，请配置 DBC 并等待解析完成")
        self.refresh_signal_list_ui()

    def load_can_bus_info(self, log_path):
        try:
            self.can_bus_data.clear()
            self.can_bus_mapping = None
            self.can_detection_snapshot = None
            detected = detect_can_bus_info(log_path)
            self.can_bus_data.update(detected)
            self.can_bus_mapping = detected.mapping
            self.can_detection_snapshot = detected.snapshot
            self.bus_loaded = True
            self._print_bus_info()

        except Exception as e:
            raise Exception(f"读取CAN总线信息失败: {e}")

    def _print_bus_info(self):
        print(f"\n{'=' * 60}")
        print(f"📊 最终总线数据:")
        for bus_id, data in self.can_bus_data.items():
            raw_ch = data.get('raw_channel', bus_id)
            is_fd = "CANFD" if data.get('has_fd', False) else "CAN2.0"
            print(
                f"  Bus {bus_id} (原始通道: {raw_ch}): {data['id_count']} 个ID, {data['msg_count']} 条报文, 类型: {is_fd}")
        print(f"{'=' * 60}\n")

    def create_bus_config_ui(self):
        self.clear_bus_config()

        if not self.can_bus_data:
            label = QLabel("未检测到CAN总线数据")
            label.setStyleSheet("color: #95a5a6; padding: 10px;")
            self.bus_container_layout.addWidget(label)
            return

        # 打印 can_bus_data 的 key 以便调试
        print(f"🔍 can_bus_data keys: {list(self.can_bus_data.keys())}")

        for bus_id in sorted(self.can_bus_data.keys()):
            config_widget = BusConfigWidget(bus_id, self)
            config_widget.bus_id_changed.connect(self.on_bus_name_changed)
            config_widget.protocol_changed.connect(self.on_bus_protocol_changed)
            config_widget.parse_requested.connect(self.parse_bus)
            config_widget.protocol_removed.connect(self.on_bus_protocol_removed)

            id_count = self.can_bus_data[bus_id].get('id_count', 0)
            config_widget.set_id_count(id_count)

            has_fd = self.can_bus_data[bus_id].get('has_fd', False)
            config_widget.set_bus_type(has_fd)

            if 'name' in self.can_bus_data[bus_id]:
                config_widget.name_edit.setText(self.can_bus_data[bus_id]['name'])

            self.bus_container_layout.addWidget(config_widget)
            self.bus_config_widgets[bus_id] = config_widget

        self.bus_container_layout.addStretch()

    def on_bus_protocol_removed(self, bus_id):
        worker = self.parse_workers.get(bus_id)
        if worker is not None:
            self.cancel_parse_bus(bus_id)

        if bus_id in self.bus_protocols:
            del self.bus_protocols[bus_id]

        if bus_id in self.db_per_bus:
            self.db_per_bus[bus_id] = {}
        if bus_id in self.can_bus_data:
            self.can_bus_data[bus_id].pop('parse_stats', None)
            self.can_bus_data[bus_id].pop('parse_error', None)
            self.can_bus_data[bus_id]['parse_state'] = 'idle'

        keys_to_remove = []
        for key, info in self.signals.items():
            if info.get('bus_id') == bus_id and key.startswith('CAN_'):
                keys_to_remove.append(key)

        for key in keys_to_remove:
            if key in self.signals:
                del self.signals[key]
            if key in self.can_parsed_data:
                del self.can_parsed_data[key]
            if key in self.bus_signal_map:
                del self.bus_signal_map[key]

        self.refresh_signal_list_ui()

        if bus_id in self.bus_config_widgets:
            self.bus_config_widgets[bus_id].set_id_count(
                self.can_bus_data.get(bus_id, {}).get('id_count', 0)
            )
            self.bus_config_widgets[bus_id].set_status('idle')

    def on_bus_name_changed(self, bus_id, new_name):
        if bus_id in self.can_bus_data:
            self.can_bus_data[bus_id]['name'] = new_name
            affected_keys = self.update_signal_names_for_bus(bus_id, new_name)
            if hasattr(self, 'update_signal_metadata_batch'):
                self.update_signal_metadata_batch(affected_keys)
            else:
                self.refresh_signal_list_ui()

    def update_signal_names_for_bus(self, bus_id, bus_name):
        affected_keys = []
        for key, info in self.signals.items():
            if info.get('bus_id') == bus_id:
                affected_keys.append(key)
                if key.startswith('CAN_'):
                    parts = key.split('_', 2)
                    if len(parts) >= 3:
                        signal_name = parts[2]
                        info['display_name'] = f"🚌 {bus_name} - {signal_name}"
                    else:
                        info['display_name'] = f"🚌 {bus_name}"
                elif key.endswith('_Time'):
                    info['display_name'] = f"⏱️ {bus_name} 时间轴"
                elif key.endswith('_MsgCount'):
                    info['display_name'] = f"📊 {bus_name} 报文计数"
        return affected_keys

    def on_bus_protocol_changed(self, bus_id, protocol_info):
        # 普通 DBC
        if bus_id in getattr(self, 'parse_workers', {}):
            self.cancel_parse_bus(bus_id)
        generations = getattr(self, '_bus_config_generations', None)
        if generations is None:
            self._bus_config_generations = generations = {}
        generations[bus_id] = generations.get(bus_id, 0) + 1
        self.bus_protocols[bus_id] = protocol_info
        if bus_id not in self.db_per_bus:
            self.db_per_bus[bus_id] = {}
        self.db_per_bus[bus_id]['arxml_sub_bus'] = ''
        self.db_per_bus[bus_id]['is_arxml'] = False
        self.db_per_bus[bus_id].pop('catalog_db', None)
        self.db_per_bus[bus_id].pop('parsed', None)
        getattr(self, '_can_raw_message_cache', {}).pop(bus_id, None)
        bus_name = self.can_bus_data.get(bus_id, {}).get('name', f"Bus {bus_id}")
        prefix = f"CAN_{bus_name}_"
        for key in tuple(self.can_parsed_data):
            if key.startswith(prefix):
                self.can_parsed_data.pop(key, None)
        for key, info in tuple(self.signals.items()):
            if info.get('bus_id') == bus_id and key.startswith(prefix):
                self.signals.pop(key, None)

        # DBC metadata is enough to populate the signal browser; BLF samples
        # are decoded only when the user asks to plot a signal.
        try:
            if cantools is None:
                raise ImportError("cantools 库未安装")
            db = cantools.database.load_file(protocol_info)
            self.db_per_bus[bus_id]['catalog_db'] = db
            self.db_per_bus[bus_id]['catalog_file_identity'] = capture_can_file_identity(protocol_info)
            bus_name = self.can_bus_data.get(bus_id, {}).get('name', f"Bus {bus_id}")
            for msg_def in db.messages:
                for sig in msg_def.signals:
                    key = f"CAN_{bus_name}_{sig.name}"
                    identity = {
                        'raw_channel': int(self.can_bus_data[bus_id].get('raw_channel', bus_id)),
                        'frame_id': int(msg_def.frame_id),
                        'is_extended': bool(getattr(msg_def, 'is_extended_frame', False)),
                        'message_name': str(msg_def.name),
                        'signal_name': str(sig.name),
                    }
                    identity_aliases = set(self._identity_alias_tokens(identity, bus_id)) if hasattr(self, '_identity_alias_tokens') else set()
                    for pending_key, pending in tuple(self.signals.items()):
                        if not pending.get('pending_selection_placeholder'):
                            continue
                        aliases = set(self._identity_alias_tokens(
                            pending.get('signal_identity'), pending.get('bus_id')
                        )) if hasattr(self, '_identity_alias_tokens') else set()
                        if identity_aliases.intersection(aliases):
                            self.signals.pop(pending_key, None)
                    info = self.signals.setdefault(key, {
                        'name': key, 'display_name': f"🚌 {bus_name} - {sig.name}",
                        'group': -88, 'channel': -88, 'bus_id': bus_id,
                        'signal_type': 'can', 'available': False,
                    })
                    prior = info.get('signal_identity')
                    candidates = list(prior) if isinstance(prior, list) else ([prior] if prior else [])
                    if identity not in candidates:
                        candidates.append(identity)
                    info.update({
                        'bus_id': bus_id, 'unit': getattr(sig, 'unit', '') or '',
                        'comment': f"{msg_def.name} (0x{msg_def.frame_id:X}) · {getattr(sig, 'unit', '') or ''}".strip(),
                        'signal_identity': candidates[0] if len(candidates) == 1 else candidates,
                    })
            self.refresh_signal_list_ui()
        except Exception as exc:
            print(f"⚠️ 无法建立 Bus {bus_id} 的 DBC 信号目录: {exc}")

        if bus_id in self.bus_config_widgets:
            self.bus_config_widgets[bus_id].set_status('idle')

    def _ensure_can_raw_message_cache(self, bus_id):
        """Return a raw-frame cache scoped to the current file/DBC/mapping."""
        snapshot = getattr(self, 'can_detection_snapshot', None)
        if snapshot is None or bus_id not in self.can_bus_data:
            return None
        protocol = self.bus_protocols.get(bus_id)
        if not protocol or not protocol.lower().endswith('.dbc'):
            return None
        entry = self.db_per_bus.setdefault(bus_id, {})
        try:
            dbc_identity = capture_can_file_identity(protocol)
        except OSError:
            # Filtered DBC objects can be supplied by the existing protocol
            # workflow even after the source path is unavailable. Parsing can
            # still use that object; only the optional raw-frame cache is off.
            return None
        db = entry.get('catalog_db')
        if db is None:
            # A filtered DBC can remain usable through the bus widget. Avoid
            # loading a separate protocol object solely for an optional cache.
            return None
        if entry.get('catalog_file_identity') != dbc_identity:
            # Reuse the established protocol-change path so catalog and cache
            # invalidation stay in one place.
            self.on_bus_protocol_changed(bus_id, protocol)
            entry = self.db_per_bus.setdefault(bus_id, {})
            db = entry.get('catalog_db')
        raw_channel = int(snapshot.mapping.bus_to_raw[bus_id])
        mapping_identity = tuple(sorted(snapshot.mapping.bus_to_raw.items()))
        context_key = (snapshot.file_identity, mapping_identity, bus_id, raw_channel, dbc_identity)
        caches = getattr(self, '_can_raw_message_cache', None)
        if caches is None:
            self._can_raw_message_cache = caches = {}
        cache = caches.get(bus_id)
        if cache is None or cache.context_key != context_key:
            frame_keys = {
                (int(message.frame_id), bool(getattr(message, 'is_extended_frame', False)))
                for message in db.messages
            }
            cache = CanRawMessageCache(snapshot.file_identity, raw_channel, frame_keys, context_key)
            caches[bus_id] = cache
        return cache

    def _raw_cache_collectors(self):
        """Group incomplete DBC raw caches by their source channel."""
        grouped = defaultdict(list)
        snapshot = getattr(self, 'can_detection_snapshot', None)
        if snapshot is None:
            return {}
        for bus_id in tuple(self.bus_protocols):
            if bus_id not in snapshot.mapping.bus_to_raw:
                continue
            cache = self._ensure_can_raw_message_cache(bus_id)
            if cache is not None and not cache.complete:
                grouped[cache.raw_channel].append(cache)
        return {channel: tuple(caches) for channel, caches in grouped.items()}

    def prepare_selected_can_signals(self, selected_keys):
        """Start only missing selected CAN samples, grouped by bus."""
        if getattr(self, '_parsing_bus_ids', None) or getattr(self, 'parse_workers', None):
            return True
        by_bus = defaultdict(set)
        for key in selected_keys:
            info = self.signals.get(key, {})
            bus_id = info.get('bus_id')
            if bus_id is None or not key.startswith('CAN_') or key in self.can_parsed_data:
                continue
            by_bus[bus_id].add(key[len(f"CAN_{self.can_bus_data.get(bus_id, {}).get('name', f'Bus {bus_id}')}_"):])
        prepared = []
        already_running = False
        for bus_id, names in by_bus.items():
            if bus_id in self._parsing_bus_ids or bus_id in self.parse_workers:
                already_running = True
                continue
            try:
                entry = self.db_per_bus.setdefault(bus_id, {})
                protocol = self.bus_protocols.get(bus_id)
                if protocol:
                    try:
                        current_dbc_identity = capture_can_file_identity(protocol)
                    except OSError as exc:
                        raise ValueError(f"无法验证 Bus {bus_id} 的 DBC 文件: {exc}") from exc
                    if (
                        entry.get('catalog_db') is None
                        or entry.get('catalog_file_identity') != current_dbc_identity
                    ):
                        self.on_bus_protocol_changed(bus_id, protocol)
                        entry = self.db_per_bus.setdefault(bus_id, {})
                db = entry.get('catalog_db')
                if db is None:
                    if not protocol:
                        continue
                    db = cantools.database.load_file(protocol)
                    entry['catalog_db'] = db
                    entry['catalog_file_identity'] = capture_can_file_identity(protocol)
                selected = {}
                for msg in db.messages:
                    matched = {sig.name for sig in msg.signals if sig.name in names}
                    if matched:
                        frame_key = (
                            int(msg.frame_id),
                            bool(getattr(msg, 'is_extended_frame', False)),
                        )
                        selected.setdefault(frame_key, set()).update(matched)
                if not selected:
                    raise ValueError(
                        f"所选信号在 Bus {bus_id} 的当前 DBC 中不存在，请重新选择信号。"
                    )
                # The filtering helper belongs to the bus configuration
                # widget; keep using that single implementation for both
                # interactive DBC filtering and demand parsing.
                filtered_db, _ = BusConfigWidget._build_filtered_database(db, selected)
                widget = self.bus_config_widgets.get(bus_id)
                if widget is not None:
                    widget._cached_filtered_db = filtered_db
                prepared.append(bus_id)
            except Exception as exc:
                self.can_bus_data.setdefault(bus_id, {})['parse_state'] = 'failed'
                self.can_bus_data[bus_id]['parse_error'] = str(exc)
                raise RuntimeError(f"无法准备 Bus {bus_id} 的选中信号解析: {exc}") from exc
        if prepared:
            self.start_parse_batch_with_progress(prepared)
            return True
        return already_running

    def _validated_can_detection_bus(self, bus_id):
        detection_worker = getattr(self, '_can_bus_detection_worker', None)
        if detection_worker is not None and detection_worker.isRunning():
            raise ValueError("CAN总线探测仍在进行中")

        snapshot = getattr(self, 'can_detection_snapshot', None)
        if not isinstance(snapshot, CanDetectionSnapshot):
            raise ValueError("缺少完整成功的CAN总线探测快照")
        if normalized_can_path(self.mdf_path) != snapshot.file_identity.path:
            raise ValueError("当前CAN日志与探测快照不是同一文件")
        try:
            current_identity = capture_can_file_identity(self.mdf_path)
        except OSError as exc:
            raise ValueError(f"无法验证当前CAN日志: {exc}") from exc
        if current_identity != snapshot.file_identity:
            raise ValueError("CAN日志在探测后发生变化")
        if bus_id not in snapshot.buses:
            raise ValueError(f"探测快照中不存在 Bus {bus_id}")

        bus_snapshot = snapshot.buses[bus_id]
        target_raw_channel = snapshot.mapping.bus_to_raw.get(bus_id)
        if (
            target_raw_channel is None
            or target_raw_channel != bus_snapshot.original_bus_id
            or bus_snapshot.bus_id != bus_id
        ):
            raise ValueError(f"Bus {bus_id} 的通道映射不一致")
        msg_count = bus_snapshot.msg_count
        if isinstance(msg_count, bool) or not isinstance(msg_count, int) or msg_count <= 0:
            raise ValueError(f"Bus {bus_id} 的msg_count无效")

        mutable_bus_data = self.can_bus_data.get(bus_id)
        if not isinstance(mutable_bus_data, dict):
            raise ValueError(f"当前总线数据中不存在 Bus {bus_id}")
        current_raw = mutable_bus_data.get('original_bus_id')
        current_count = mutable_bus_data.get('msg_count')
        if current_raw != target_raw_channel:
            raise ValueError(f"Bus {bus_id} 的当前通道与探测快照不一致")
        if (
            isinstance(current_count, bool)
            or not isinstance(current_count, int)
            or current_count <= 0
            or current_count != msg_count
        ):
            raise ValueError(f"Bus {bus_id} 的当前msg_count与探测快照不一致")
        return bus_snapshot

    def parse_bus(self, bus_id, auto_parse=False, _batch_workers=None):
        pending_restore = getattr(self, '_pending_signal_restore', None)
        if pending_restore is not None:
            pending_restore.setdefault('expected_buses', set()).add(bus_id)
        if bus_id in self._parsing_bus_ids:
            return
        self._parsing_bus_ids.add(bus_id)

        if bus_id not in self.bus_protocols:
            self._parsing_bus_ids.discard(bus_id)
            if bus_id in getattr(self, 'can_bus_data', {}):
                self.can_bus_data[bus_id]['parse_state'] = 'failed'
                self.can_bus_data[bus_id]['parse_error'] = '未配置协议文件'
            if hasattr(self, '_complete_pending_signal_restore'):
                self._complete_pending_signal_restore(bus_id, plot=False)
            if not auto_parse:
                QMessageBox.warning(self, "解析失败", f"Bus {bus_id} 未配置协议文件")
            return

        try:
            bus_snapshot = self._validated_can_detection_bus(bus_id)
        except (KeyError, TypeError, ValueError) as exc:
            self._parsing_bus_ids.discard(bus_id)
            if bus_id in getattr(self, 'can_bus_data', {}):
                self.can_bus_data[bus_id]['parse_state'] = 'failed'
                self.can_bus_data[bus_id]['parse_error'] = str(exc)
            if hasattr(self, '_complete_pending_signal_restore'):
                self._complete_pending_signal_restore(bus_id, plot=False)
            message = f"无法开始解析：{exc}。请重新探测当前CAN日志。"
            if bus_id in self.bus_config_widgets:
                self.bus_config_widgets[bus_id].set_status('error')
            if not auto_parse:
                QMessageBox.warning(self, "需要重新探测", message)
            print(f"⚠️ {message}")
            return

        if bus_id in self.parse_workers:
            self._parsing_bus_ids.discard(bus_id)
            return

        self._cancelled_parse_buses.discard(bus_id)

        if bus_id in self.bus_config_widgets:
            self.bus_config_widgets[bus_id].set_status('loading')
        if bus_id in self.can_bus_data:
            self.can_bus_data[bus_id]['parse_state'] = 'parsing'

        progress_dialog = ParseProgressDialog(bus_id, self)
        progress_dialog.cancel_requested.connect(self.cancel_parse_bus)
        progress_dialog.show()
        self.progress_dialogs[bus_id] = progress_dialog

        try:
            protocol_info = self.bus_protocols[bus_id]
            widget = self.bus_config_widgets.get(bus_id)
            db = None

            cached_filtered_db = (
                widget._cached_filtered_db
                if widget and hasattr(widget, '_cached_filtered_db')
                else None
            )

            if cached_filtered_db is not None:
                db = cached_filtered_db
                print("✅ 使用筛选后的DBC缓存")
            elif protocol_info.lower().endswith('.dbc'):
                try:
                    if cantools is None:
                        raise ImportError("cantools 库未安装")

                    db = cantools.database.load_file(protocol_info)
                    print(f"✅ DBC加载成功: {len(db.messages)} 个消息")
                except ImportError as e:
                    raise Exception(f"请安装 cantools 库: pip install cantools\n\n错误: {e}")
                except Exception as e:
                    raise Exception(f"加载DBC失败: {e}")
            else:
                raise Exception(f"不支持的协议格式: {protocol_info}")

            if db is None:
                raise Exception("协议文件加载失败")

            self.db_per_bus[bus_id] = self.db_per_bus.get(bus_id, {})
            self.db_per_bus[bus_id]['db'] = db
            self.db_per_bus[bus_id]['parsed'] = False
            self.db_per_bus[bus_id]['is_arxml'] = False

            worker = ParseWorker(
                bus_id,
                bus_snapshot.original_bus_id,
                bus_snapshot.msg_count,
                self.can_detection_snapshot.file_identity.path,
                self.can_detection_snapshot.file_identity,
                db,
                "",
            )
            worker._task_generation = getattr(self, '_can_task_generation', 0)
            worker._file_identity = self.can_detection_snapshot.file_identity
            worker._bus_config_generation = getattr(self, '_bus_config_generations', {}).get(bus_id, 0)
            worker._dbc_file_identity = self.db_per_bus[bus_id].get('catalog_file_identity')
            worker.raw_message_cache = self._ensure_can_raw_message_cache(bus_id)
            # Demand parsing is routed through the batch entry, even for one
            # bus. Legacy independent ParseWorker calls remain uncached so
            # concurrent per-bus scans cannot append the same raw frames twice.
            worker.raw_cache_collectors = (
                self._raw_cache_collectors() if _batch_workers is not None else {}
            )
            worker.raw_cache_frame_keys = worker.raw_message_cache.frame_keys if worker.raw_message_cache else frozenset()
            worker.progress_updated.connect(self.on_parse_progress)
            worker.parse_finished.connect(self.on_parse_finished)
            worker.parse_error.connect(self.on_parse_error)
            worker.parse_cancelled.connect(self.on_parse_cancelled)
            if _batch_workers is None:
                worker.finished.connect(self._on_parse_worker_thread_finished)
            self.parse_workers[bus_id] = worker
            if _batch_workers is None:
                worker.start()
                print(f"🚀 总线 {bus_id} 解析任务已启动")
            else:
                _batch_workers.append(worker)
                print(f"🚀 总线 {bus_id} 已加入批量解析任务")

        except Exception as e:
            self._parsing_bus_ids.discard(bus_id)
            if bus_id in self.bus_config_widgets:
                self.bus_config_widgets[bus_id].set_status('error')
            if bus_id in self.progress_dialogs:
                self.progress_dialogs[bus_id].set_finished(False)
                self.progress_dialogs[bus_id].set_detail(f"错误: {e}")
            if bus_id in self.can_bus_data:
                self.can_bus_data[bus_id]['parse_state'] = 'failed'
                self.can_bus_data[bus_id]['parse_error'] = str(e)
            if hasattr(self, '_complete_pending_signal_restore'):
                self._complete_pending_signal_restore(bus_id, plot=False)
            if not auto_parse:
                QMessageBox.warning(self, "解析失败", f"解析 Bus {bus_id} 时出错: {e}")
            import traceback
            traceback.print_exc()

    def start_parse_with_progress(self, bus_id):
        self.parse_bus(bus_id, auto_parse=True)

    def start_parse_batch_with_progress(self, bus_ids):
        """Prepare configured buses with existing setup, then share one reader."""
        collectors = self._raw_cache_collectors()
        prepared_workers = []
        for bus_id in sorted(set(bus_ids)):
            self.parse_bus(
                bus_id, auto_parse=True, _batch_workers=prepared_workers
            )
        if not prepared_workers:
            return None
        uncached = []
        cached = []
        for parser in prepared_workers:
            parser.raw_cache_collectors = collectors
            if parser.raw_message_cache is not None and parser.raw_message_cache.complete:
                cached.append(parser)
            else:
                uncached.append(parser)
        for parser in cached:
            parser.finished.connect(self._on_parse_worker_thread_finished)
            parser.start()
        prepared_workers = uncached
        if not prepared_workers:
            return cached[0] if len(cached) == 1 else None
        if len(prepared_workers) == 1:
            prepared_workers[0].finished.connect(
                self._on_parse_worker_thread_finished
            )
            prepared_workers[0].start()
            print("单路解析保持原 ParseWorker 路径")
            return prepared_workers[0]

        batch_worker = MultiBusParseWorker(prepared_workers)
        batch_worker._task_generation = getattr(self, '_can_task_generation', 0)
        batch_worker._file_identity = self.can_detection_snapshot.file_identity
        for parser in prepared_workers:
            self.parse_workers[parser.bus_id] = batch_worker
        batch_worker.finished.connect(self._on_multi_parse_worker_thread_finished)
        batch_worker.start()
        print(
            "🚀 批量解析任务已启动，共享单次 BLF 扫描，目标 Bus: "
            f"{sorted(parser.bus_id for parser in prepared_workers)}"
        )
        return batch_worker

    def _on_multi_parse_worker_thread_finished(self):
        worker = self.sender()
        if worker is None:
            return
        for bus_id, current in tuple(self.parse_workers.items()):
            if current is worker:
                self.parse_workers.pop(bus_id, None)
        for parser in getattr(worker, 'parsers', {}).values():
            parser.deleteLater()
        worker.deleteLater()
        self._retry_pending_can_plot()

    def cancel_parse_bus(self, bus_id):
        """Request cooperative stop and immediately reflect the stopped state."""
        self._plot_after_can_parse = False
        self._cancelled_parse_buses.add(bus_id)
        worker = self.parse_workers.get(bus_id)
        if worker is not None and worker.isRunning():
            if isinstance(worker, MultiBusParseWorker):
                worker.cancel_bus(bus_id)
            else:
                worker.stop()

        if bus_id in self.can_bus_data:
            self.can_bus_data[bus_id]['parse_state'] = 'stopped'
        if bus_id in self.bus_config_widgets and bus_id in self.bus_protocols:
            self.bus_config_widgets[bus_id].set_status('stopped')
        dialog = self.progress_dialogs.get(bus_id)
        if dialog is not None:
            dialog.status_label.setText("⏹ 正在停止解析...")
            dialog.cancel_btn.setEnabled(False)

        if worker is None or not worker.isRunning():
            self.on_parse_cancelled(bus_id)

    def on_parse_cancelled(self, bus_id):
        if not getattr(self, '_is_current_parse_callback', lambda _bus: True)(bus_id):
            return
        self._parsing_bus_ids.discard(bus_id)
        worker = self.parse_workers.get(bus_id)
        if worker is not None and not worker.isRunning():
            self.parse_workers.pop(bus_id, None)
        if bus_id in self.can_bus_data and bus_id in self.bus_protocols:
            self.can_bus_data[bus_id]['parse_state'] = 'stopped'
        if bus_id in self.bus_config_widgets:
            self.bus_config_widgets[bus_id].set_status(
                'stopped' if bus_id in self.bus_protocols else 'idle'
            )
        dialog = self.progress_dialogs.get(bus_id)
        if dialog is not None:
            dialog.set_cancelled()
        if hasattr(self, '_complete_pending_signal_restore'):
            self._complete_pending_signal_restore(bus_id, plot=False)

    def _on_parse_worker_thread_finished(self):
        worker = self.sender()
        if worker is None:
            return
        bus_id = getattr(worker, 'bus_id', None)
        if bus_id is not None and self.parse_workers.get(bus_id) is worker:
            self.parse_workers.pop(bus_id, None)
        worker.deleteLater()
        self._retry_pending_can_plot()

    def _retry_pending_can_plot(self):
        if getattr(self, '_plot_after_can_parse', False) and not self._parsing_bus_ids:
            self._skip_can_demand_once = False
            QTimer.singleShot(0, self.plot_selected_signals)

    def _is_current_parse_callback(self, bus_id):
        sender_getter = getattr(self, 'sender', None)
        sender = sender_getter() if callable(sender_getter) else None
        if sender is None:
            return True  # Supports direct slot calls in tests and legacy callers.
        current = getattr(self, 'parse_workers', {}).get(bus_id)
        owner = sender
        if current is not sender:
            parsers = getattr(current, 'parsers', {}) if current is not None else {}
            if parsers.get(bus_id) is not sender:
                return False
        task_generation = getattr(owner, '_task_generation', getattr(self, '_can_task_generation', 0))
        file_identity = getattr(owner, '_file_identity', getattr(owner, 'file_identity', None))
        config_generation = getattr(owner, '_bus_config_generation', getattr(self, '_bus_config_generations', {}).get(bus_id, 0))
        return (
            task_generation == getattr(self, '_can_task_generation', 0)
            and file_identity == getattr(getattr(self, 'can_detection_snapshot', None), 'file_identity', None)
            and config_generation == getattr(self, '_bus_config_generations', {}).get(bus_id, 0)
            and self._parse_callback_dbc_is_current(bus_id, owner)
        )

    def _parse_callback_dbc_is_current(self, bus_id, owner):
        expected = getattr(owner, '_dbc_file_identity', None)
        if expected is None:
            return True
        protocol = getattr(self, 'bus_protocols', {}).get(bus_id)
        try:
            return protocol is not None and capture_can_file_identity(protocol) == expected
        except OSError:
            return False

    def on_parse_progress(self, bus_id, progress, status_text):
        if not getattr(self, '_is_current_parse_callback', lambda _bus: True)(bus_id):
            return
        if bus_id in self.progress_dialogs:
            dialog = self.progress_dialogs[bus_id]
            if dialog and not dialog.is_cancelled():
                dialog.update_progress(progress, status_text)
                if progress < 100:
                    dialog.set_detail(f"Bus {bus_id}: 正在解析...")

    def on_parse_finished(self, bus_id, stats):
        if not getattr(self, '_is_current_parse_callback', lambda _bus: True)(bus_id):
            return
        if bus_id in getattr(self, '_cancelled_parse_buses', ()):
            self.on_parse_cancelled(bus_id)
            return
        self._parsing_bus_ids.discard(bus_id)

        for key, info in tuple(getattr(self, 'signals', {}).items()):
            if info.get('bus_id') == bus_id and info.get('pending_selection_placeholder'):
                self.signals.pop(key, None)

        pool = stats.pop('pool', {})

        if not pool:
            if stats.get('dbc_signal_count', 0) == 0:
                result_state = 'dbc_has_no_signals'
                reason = "所选 DBC 未包含可解析信号，请检查 ARXML → DBC 转换结果。"
            elif stats.get('dbc_matched_frames', 0) == 0:
                result_state = 'no_matching_frames'
                reason = "BLF 中未找到与所选 DBC 匹配的 CAN 报文，请检查 Bus/DBC 配置。"
            elif (
                stats.get('empty_decoded_frames', 0) > 0
                and stats.get('decode_failed_frames', 0) == 0
            ):
                result_state = 'all_decodes_empty'
                reason = "匹配报文解码未报错，但均未返回信号，请重新选择或生成筛选 DBC。"
            else:
                result_state = 'all_matching_frames_failed'
                reason = "找到匹配报文，但均无法解码，请检查 DBC 报文长度及 CAN FD 定义。"
            stats['result_state'] = result_state
            stats['result_message'] = reason
            print(f"⚠️ 总线 {bus_id} 未生成可用信号: {reason}")
            if bus_id in self.bus_config_widgets:
                self.bus_config_widgets[bus_id].set_status('error')
            if bus_id in self.progress_dialogs:
                self.progress_dialogs[bus_id].set_empty_result(reason)
            if bus_id in self.can_bus_data:
                self.can_bus_data[bus_id]['parse_stats'] = stats
                self.can_bus_data[bus_id]['parse_state'] = 'complete'
            if bus_id in self.db_per_bus:
                self.db_per_bus[bus_id]['parsed'] = False
            bus_name = self.can_bus_data.get(bus_id, {}).get('name', f"Bus {bus_id}")
            if hasattr(self, 'register_can_signal_catalog'):
                self.register_can_signal_catalog(bus_id, bus_name, stats)
            if hasattr(self, 'refresh_signal_list_ui'):
                self.refresh_signal_list_ui()
            if hasattr(self, '_complete_pending_signal_restore'):
                self._complete_pending_signal_restore(bus_id, plot=False)
            if getattr(self, '_plot_after_can_parse', False) and not self._parsing_bus_ids:
                self._plot_after_can_parse = False
                self._skip_can_demand_once = True
                if hasattr(self, '_set_plot_empty_state'):
                    self._set_plot_empty_state(reason)
            return

        stats['result_state'] = 'success'
        stats['result_message'] = ''
        stats.setdefault('signal_identity', stats.pop('signal_identity', {}))

        bus_name = self.can_bus_data.get(bus_id, {}).get('name', f"Bus {bus_id}")
        self.register_can_signals(pool, bus_name, bus_id)
        identities = stats.get('signal_identity', {})
        for key, info in getattr(self, 'signals', {}).items():
            if info.get('bus_id') != bus_id or not key.startswith(f'CAN_{bus_name}_'):
                continue
            name = key[len(f'CAN_{bus_name}_'):]
            candidates = identities.get(name, ())
            if candidates:
                identities_for_signal = [dict(item, bus_id=bus_id) for item in candidates]
                identity = identities_for_signal[0] if len(identities_for_signal) == 1 else identities_for_signal
                info['signal_identity'] = identity
                if key in self.can_parsed_data:
                    self.can_parsed_data[key]['signal_identity'] = identity

        if bus_id in self.can_bus_data:
            self.can_bus_data[bus_id]['parse_stats'] = stats
            self.can_bus_data[bus_id]['parse_state'] = 'complete'

        if hasattr(self, 'register_can_signal_catalog'):
            self.register_can_signal_catalog(bus_id, bus_name, stats)

        if bus_id in self.db_per_bus:
            self.db_per_bus[bus_id]['parsed'] = True

        if bus_id in self.bus_config_widgets:
            total_actual_ids = self.can_bus_data.get(bus_id, {}).get('id_count', 0)
            msg_ids_with_signals = stats.get('msg_ids_with_signals', 0)
            self.bus_config_widgets[bus_id].set_status('success')
            self.bus_config_widgets[bus_id].set_id_count(total_actual_ids, msg_ids_with_signals)

        if bus_id in self.progress_dialogs:
            self.progress_dialogs[bus_id].update_progress(100, "解析完成!")
            self.progress_dialogs[bus_id].set_finished(True, stats)
            QTimer.singleShot(3000, lambda: self.close_progress_dialog(bus_id))

        self.refresh_signal_list_ui()

        if not getattr(self, '_pending_signal_restore', None) and getattr(self, '_reusable_can_selection', None) and hasattr(self, '_apply_can_selection'):
            saved = self._reusable_can_selection
            self._apply_can_selection(
                list(saved),
                {key: {**identity, 'bus_id': self.signals.get(key, {}).get('bus_id')}
                 for key, identity in saved.items() if isinstance(identity, dict)},
            )

        if hasattr(self, '_complete_pending_signal_restore'):
            self._complete_pending_signal_restore(bus_id, plot=False)

        print(f"✅ Bus {bus_id} 解析完成: {stats}")
        if getattr(self, '_plot_after_can_parse', False) and not self._parsing_bus_ids:
            self._plot_after_can_parse = False
            self._skip_can_demand_once = True
            QTimer.singleShot(0, self.plot_selected_signals)

    def on_parse_error(self, bus_id, error_msg):
        if not getattr(self, '_is_current_parse_callback', lambda _bus: True)(bus_id):
            return
        if bus_id in getattr(self, '_cancelled_parse_buses', ()):
            self.on_parse_cancelled(bus_id)
            return
        self._parsing_bus_ids.discard(bus_id)

        if bus_id in self.bus_config_widgets:
            self.bus_config_widgets[bus_id].set_status('error')
        if bus_id in self.can_bus_data:
            self.can_bus_data[bus_id]['parse_state'] = 'failed'
            self.can_bus_data[bus_id]['parse_error'] = str(error_msg)

        if bus_id in self.progress_dialogs:
            self.progress_dialogs[bus_id].set_finished(False)
            self.progress_dialogs[bus_id].set_detail(f"错误: {error_msg}")

        if hasattr(self, '_complete_pending_signal_restore'):
            self._complete_pending_signal_restore(bus_id, plot=False)

        if getattr(self, '_plot_after_can_parse', False) and not self._parsing_bus_ids:
            self._plot_after_can_parse = False
            self._skip_can_demand_once = True
            if hasattr(self, '_set_plot_empty_state'):
                self._set_plot_empty_state(f"CAN 信号解析失败：{error_msg}")

        print(f"❌ Bus {bus_id} 解析失败: {error_msg}")

    def _complete_pending_signal_restore(self, bus_id, *, plot=True):
        pending = getattr(self, '_pending_signal_restore', None)
        if not pending:
            return
        pending.setdefault('completed_buses', set()).add(bus_id)
        expected = set(pending.get('expected_buses', ()))
        if not expected or not pending['completed_buses'] >= expected:
            return
        config = pending.get('config', {})
        if config:
            report = self._restore_calculated_channels(config)
        else:
            report = None
        for combo, field in ((self.lat_combo, 'gps_lat_key'), (self.lon_combo, 'gps_lon_key')):
            key = config.get(field)
            index = combo.findData(key) if key else -1
            if index >= 0:
                combo.setCurrentIndex(index)
        self._apply_can_selection(
            pending.get('selected', ()), pending.get('identities', {}), plot=plot
        )
        self._pending_signal_restore = None
        if report is not None and report.failures:
            QMessageBox.warning(
                self, '公式通道恢复报告', self._restore_summary(report)
            )

    def close_progress_dialog(self, bus_id):
        if bus_id in self.progress_dialogs:
            try:
                dialog = self.progress_dialogs[bus_id]
                if dialog:
                    dialog.hide()
                    dialog.close()
                    dialog.deleteLater()
            except Exception:
                pass
            del self.progress_dialogs[bus_id]

    def register_can_signals(self, pool, bus_name, bus_id):
        registered_count = 0

        for sig_name, pack in pool.items():
            if len(pack['t']) == 0:
                continue

            t_arr = np.array(pack['t'], dtype=float)
            v_arr = np.array(pack['v'], dtype=float)

            sort_idx = np.argsort(t_arr)
            t_sorted = t_arr[sort_idx]
            v_sorted = v_arr[sort_idx]

            unique_mask = np.diff(t_sorted, prepend=t_sorted[0] - 1e-9) > 1e-6
            t_unique = t_sorted[unique_mask]
            v_unique = v_sorted[unique_mask]

            if len(t_unique) == 0:
                continue

            unique_key = f"CAN_{bus_name}_{sig_name}"

            self.can_parsed_data[unique_key] = {
                'timestamps': t_unique,
                'samples': v_unique,
                'unit': pack.get('unit', ''),
                'bus_id': bus_id,
                'comment': pack.get('comment', ''),
                'file_identity': getattr(
                    getattr(self, 'can_detection_snapshot', None),
                    'file_identity', None,
                ),
                'raw_channel': getattr(self, 'can_bus_data', {}).get(bus_id, {}).get('raw_channel'),
                'protocol_path': getattr(self, 'bus_protocols', {}).get(bus_id),
                'protocol_identity': getattr(self, 'db_per_bus', {}).get(bus_id, {}).get('catalog_file_identity'),
            }

            self.signals[unique_key] = {
                'name': unique_key,
                'display_name': f"🚌 {bus_name} - {sig_name}",
                'group': -88,
                'channel': -88,
                'comment': f"{pack.get('comment', '')} ({pack.get('unit', '')})",
                'bus_id': bus_id,
                'signal_type': 'can',
                'available': True,
            }
            self.bus_signal_map[unique_key] = bus_id
            registered_count += 1

        print(f"✅ 总线 {bus_name} (ID: {bus_id}) 注册了 {registered_count} 个CAN信号")

    def register_can_signal_catalog(self, bus_id, bus_name, stats):
        """Keep every DBC signal visible while marking file-specific data availability."""
        identities = stats.get('signal_identity', {})
        matched_frames = {
            (int(frame_id), bool(is_extended))
            for frame_id, is_extended in stats.get('matched_frames', ())
        }
        for signal_name, candidates in identities.items():
            if not candidates:
                continue
            normalized = [dict(item, bus_id=bus_id) for item in candidates]
            identity = normalized[0] if len(normalized) == 1 else normalized
            key = f"CAN_{bus_name}_{signal_name}"
            available = key in self.can_parsed_data and len(self.can_parsed_data[key].get('samples', ())) > 0
            if available:
                reason = ""
            elif any((item['frame_id'], item['is_extended']) in matched_frames for item in normalized):
                if stats.get('decode_failed_frames', 0):
                    reason = "当前 BLF 有对应报文，但解码未生成此信号数据"
                else:
                    reason = "当前 BLF 有对应报文，但未生成此信号数据"
            else:
                reason = "当前 BLF 不包含此信号对应的报文"
            info = self.signals.get(key, {
                'name': key,
                'display_name': f"🚌 {bus_name} - {signal_name}",
                'group': -88,
                'channel': -88,
                'comment': '',
                'bus_id': bus_id,
                'signal_type': 'can',
            })
            info.update({
                'signal_identity': identity,
                'available': available,
                'unavailable_reason': reason,
            })
            if reason:
                info['comment'] = f"{info.get('comment', '')}；{reason}".strip('；')
            self.signals[key] = info

    def create_basic_signals_for_bus(self, bus_id):
        bus_name = self.can_bus_data.get(bus_id, {}).get('name', f"Bus {bus_id}")

        if bus_id not in self.can_bus_data:
            return

        data = self.can_bus_data[bus_id]
        timestamps = data.get('timestamps', [])
        if not timestamps:
            return

        timestamps = np.array(timestamps)
        if len(timestamps) > 0:
            t0 = timestamps[0]
            rel_timestamps = timestamps - t0

            time_key = f"CAN_{bus_name}_Time"
            self.can_parsed_data[time_key] = {
                'timestamps': rel_timestamps,
                'samples': rel_timestamps,
                'unit': 's',
                'bus_id': bus_id
            }
            self.signals[time_key] = {
                'name': time_key,
                'display_name': f"⏱️ {bus_name} 时间轴",
                'group': -88,
                'channel': -88,
                'comment': f"{bus_name} 时间轴 (采样 {len(timestamps)} 个点)",
                'bus_id': bus_id
            }
            self.bus_signal_map[time_key] = bus_id

            count_key = f"CAN_{bus_name}_MsgCount"
            msg_count = np.arange(len(timestamps))
            self.can_parsed_data[count_key] = {
                'timestamps': rel_timestamps,
                'samples': msg_count,
                'unit': '',
                'bus_id': bus_id
            }
            self.signals[count_key] = {
                'name': count_key,
                'display_name': f"📊 {bus_name} 报文计数",
                'group': -88,
                'channel': -88,
                'comment': f"{bus_name} 报文计数 ({len(timestamps)} 条报文)",
                'bus_id': bus_id
            }
            self.bus_signal_map[count_key] = bus_id

    def load_can_timestamps_only_multibus(self, log_path):
        try:
            if log_path.lower().endswith('.blf'):
                reader = can.BLFReader(log_path)
            else:
                reader = can.LogReader(log_path)

            bus_timestamps = defaultdict(list)
            t0_per_bus = {}

            for msg in reader:
                raw_channel = message_raw_channel(msg)
                mapping = getattr(self, 'can_bus_mapping', None)
                bus_id = mapping.raw_to_bus.get(raw_channel, raw_channel) if mapping else raw_channel
                if bus_id not in t0_per_bus:
                    t0_per_bus[bus_id] = msg.timestamp
                rel_t = msg.timestamp - t0_per_bus[bus_id]
                bus_timestamps[bus_id].append(rel_t)

            try:
                reader.stop()
            except Exception:
                pass

            for bus_id, timestamps in bus_timestamps.items():
                if not timestamps:
                    continue
                timestamps = np.array(timestamps)
                bus_name = self.can_bus_data.get(bus_id, {}).get('name', f"Bus {bus_id}")

                if len(timestamps) > 1000:
                    step = len(timestamps) // 1000
                    timestamps = timestamps[::step]

                time_key = f"CAN_{bus_name}_Time"
                self.can_parsed_data[time_key] = {
                    'timestamps': timestamps,
                    'samples': timestamps,
                    'unit': 's',
                    'bus_id': bus_id
                }
                self.signals[time_key] = {
                    'name': time_key,
                    'display_name': f"⏱️ {bus_name} 时间轴",
                    'group': -88,
                    'channel': -88,
                    'comment': f"{bus_name} 时间轴",
                    'bus_id': bus_id
                }
                self.bus_signal_map[time_key] = bus_id

            self.mdf_file = pd.DataFrame(index=np.array([0.0]))

        except Exception as e:
            raise Exception(f"加载CAN时间信息失败: {e}")

    def load_signals(self):
        if not self.mdf_path or not os.path.exists(self.mdf_path):
            self.clear_signal_list()
            empty_label = QLabel(f"错误: 找不到文件 {self.mdf_path}")
            empty_label.setStyleSheet("color: #e74c3c; padding: 10px;")
            self.signal_layout.addWidget(empty_label)
            self.set_buttons_enabled(False)
            return

        self.clear_signal_list()

        if hasattr(self, "clear_signal_curve_visibility"):
            self.clear_signal_curve_visibility()
        self.signals.clear()
        self.can_parsed_data.clear()
        self.bus_signal_map.clear()
        self.custom_math_data.clear()
        self.can_bus_data.clear()
        self.db_per_bus.clear()
        self.bus_protocols.clear()
        self.can_bus_signals.clear()

        self.clear_bus_config()

        if isinstance(self.mdf_file, asammdf.MDF):
            try:
                self.mdf_file.close()
            except Exception:
                pass
        self.mdf_file = None

        file_extension = os.path.splitext(self.mdf_path)[1].lower()

        try:
            self.lat_combo.currentIndexChanged.disconnect()
            self.lon_combo.currentIndexChanged.disconnect()
        except Exception:
            pass
        self.lat_combo.clear()
        self.lon_combo.clear()

        try:
            if file_extension in ('.blf', '.asc', '.trc'):
                if not self.can_bus_data:
                    self.load_can_bus_info(self.mdf_path)
                    self.create_bus_config_ui()

                if not self.bus_protocols:
                    self.load_can_timestamps_only_multibus(self.mdf_path)

                if self.can_parsed_data:
                    self.refresh_signal_list_ui()
                else:
                    self.refresh_signal_list_ui()

            elif file_extension in ('.mdf', '.mf4'):
                self.mdf_file = asammdf.MDF(self.mdf_path)
                all_channels = list(self.mdf_file.iter_channels())

                for signal in all_channels:
                    name = signal.name
                    comment = signal.comment if signal.comment else "无描述"
                    if not isinstance(comment, str):
                        try:
                            comment = comment.decode('utf-8', errors='ignore')
                        except Exception:
                            comment = str(comment)

                    unique_key = f"{name}_G{signal.group_index}_C{signal.channel_index}"
                    display_name = f"{name} (Group: {signal.group_index}, Channel: {signal.channel_index})"

                    self.signals[unique_key] = {
                        'name': name,
                        'display_name': display_name,
                        'group': signal.group_index,
                        'channel': signal.channel_index,
                        'comment': comment,
                        'bus_id': None
                    }

                self.refresh_signal_list_ui()

            elif file_extension == '.csv':
                try:
                    df = pd.read_csv(self.mdf_path, encoding='utf-8')
                except UnicodeDecodeError:
                    try:
                        df = pd.read_csv(self.mdf_path, encoding='gbk')
                    except Exception as e:
                        raise Exception(f"无法读取CSV文件，请检查编码格式。错误: {e}")

                if 'timestamp' not in df.columns:
                    first_col_name = df.columns[0]
                    if not ('signal' in first_col_name.lower() or 'value' in first_col_name.lower()):
                        df.rename(columns={first_col_name: 'timestamp'}, inplace=True)
                    else:
                        df.insert(0, 'timestamp', np.arange(len(df)))

                self.mdf_file = df.set_index('timestamp')

                for col in self.mdf_file.columns:
                    signal_name = col
                    self.signals[signal_name] = {
                        'name': signal_name,
                        'display_name': signal_name,
                        'group': -1,
                        'channel': -1,
                        'comment': "从CSV文件导入",
                        'bus_id': None
                    }

                self.refresh_signal_list_ui()

            elif file_extension == '.vbo':
                try:
                    with open(self.mdf_path, 'r', encoding='utf-8', errors='ignore') as f:
                        lines = [line.rstrip('\n') for line in f.readlines()]

                    column_names_line = None
                    data_start = None
                    for i, line in enumerate(lines):
                        stripped = line.strip().lower()
                        if stripped == '[column names]':
                            column_names_line = i + 1
                        elif stripped == '[data]':
                            data_start = i + 1

                    if column_names_line is None or data_start is None:
                        raise Exception("无法找到 [column names] 或 [data] 区。")

                    column_line = lines[column_names_line].strip()
                    column_names = column_line.split()

                    seen = {}
                    unique_columns = []
                    for col in column_names:
                        if col in seen:
                            seen[col] += 1
                            unique_columns.append(f"{col}_dup{seen[col]}")
                        else:
                            seen[col] = 0
                            unique_columns.append(col)

                    data_lines = []
                    for line in lines[data_start:]:
                        stripped = line.strip()
                        if stripped and not stripped.startswith('['):
                            data_lines.append(line)

                    if not data_lines:
                        raise Exception("数据区为空。")

                    data_str = '\n'.join(data_lines)

                    df = pd.read_csv(StringIO(data_str), sep=r'\s+', header=None, names=unique_columns,
                                     engine='python', on_bad_lines='skip')

                    if df.empty:
                        raise Exception("解析后数据为空。")

                    time_col_name = None
                    for col in df.columns:
                        if col.lower() == 'time':
                            time_col_name = col
                            break

                    if time_col_name is not None:
                        df.rename(columns={time_col_name: 'timestamp'}, inplace=True)
                    else:
                        df.rename(columns={df.columns[1]: 'timestamp'}, inplace=True)

                    df = df.dropna(how='all', axis=1)
                    df['timestamp'] = pd.to_numeric(df['timestamp'], errors='coerce')
                    df = df.dropna(subset=['timestamp']).copy()

                    raw_timestamps = df['timestamp'].values
                    if len(raw_timestamps) > 0 and raw_timestamps.max() >= 100:
                        int_part = np.floor(raw_timestamps).astype(int)
                        frac_part = raw_timestamps - int_part

                        hours = int_part // 10000
                        minutes = (int_part % 10000) // 100
                        seconds_int = int_part % 100
                        absolute_seconds = (hours * 3600) + (minutes * 60) + seconds_int + frac_part
                    else:
                        absolute_seconds = raw_timestamps

                    if len(absolute_seconds) > 0:
                        t0 = absolute_seconds[0]
                        df['timestamp'] = absolute_seconds - t0

                    df = df.sort_values('timestamp')

                    is_duplicate = df['timestamp'].duplicated()
                    if is_duplicate.any():
                        cum_dup = df.groupby('timestamp').cumcount()
                        df['timestamp'] = df['timestamp'] + cum_dup * 1e-6

                    self.mdf_file = df.set_index('timestamp')

                    for col in self.mdf_file.columns:
                        display_name = col
                        comment = "VBO导入"
                        if '_dup' in col:
                            comment += " (重复列已重命名)"

                        self.signals[col] = {
                            'name': col,
                            'display_name': display_name,
                            'group': -1,
                            'channel': -1,
                            'comment': comment,
                            'bus_id': None
                        }

                    self.refresh_signal_list_ui()

                except Exception as e:
                    raise Exception(f"无法解析VBO文件: {e}")

            else:
                error_label = QLabel(f"不支持的文件类型: {file_extension}")
                error_label.setStyleSheet("color: #e74c3c; padding: 10px;")
                self.signal_layout.addWidget(error_label)
                self.set_buttons_enabled(False)
                return

            if not self.signals:
                empty_label = QLabel("文件中没有找到有效信号。")
                empty_label.setStyleSheet("color: #95a5a6; padding: 10px;")
                self.signal_layout.addWidget(empty_label)
                self.set_buttons_enabled(False)
                return

            if file_extension in ('.blf', '.asc', '.trc') and not self.can_parsed_data:
                hint_label = QLabel("💡 提示: 在下方 CAN 总线配置中加载 DBC，选择信号后点击“绘图”")
                hint_label.setStyleSheet(
                    "color: #2980b9; padding: 5px; font-size: 11px; background-color: #d6eaf8; border-radius: 3px;")
                self.signal_layout.insertWidget(0, hint_label)

            self.refresh_signal_list_ui()

        except Exception as e:
            self.clear_signal_list()
            error_label = QLabel(f"加载文件时出错: {str(e)}")
            error_label.setStyleSheet("color: #e74c3c; padding: 10px;")
            self.signal_layout.addWidget(error_label)
            self.set_buttons_enabled(False)
            import traceback
            traceback.print_exc()

    def search_signals(self, text):
        text = text.strip().lower()

        if hasattr(self, 'search_timer') and self.search_timer is not None:
            self.search_timer.stop()

        self._search_pending_text = text
        self.search_timer = QTimer()
        self.search_timer.setSingleShot(True)
        self.search_timer.timeout.connect(self._do_search)
        self.search_timer.start(100)

    def _do_search(self):
        text = self._search_pending_text
        if not text:
            for unique_key, widgets in self.signal_widgets.items():
                checkbox = widgets['checkbox']
                comment_label = widgets['comment_label']
                if checkbox:
                    checkbox.show()
                if comment_label:
                    comment_label.show()
            return

        self._build_search_index()

        matched_keys = set()
        for key, info in self.search_index.items():
            if text in info['search_text']:
                matched_keys.add(key)

        for unique_key, widgets in self.signal_widgets.items():
            checkbox = widgets['checkbox']
            comment_label = widgets['comment_label']
            if unique_key in matched_keys:
                if checkbox:
                    checkbox.show()
                if comment_label:
                    comment_label.show()
            else:
                if checkbox:
                    checkbox.hide()
                if comment_label:
                    comment_label.hide()

    def _build_search_index(self):
        self.search_index.clear()
        for unique_key, info in self.signals.items():
            search_text = f"{info.get('name', '')} {info.get('display_name', '')} {info.get('comment', '')}".lower()
            self.search_index[unique_key] = {
                'search_text': search_text
            }

    def get_selected_signals(self):
        selected_signals = []
        for unique_key, widgets in self.signal_widgets.items():
            if widgets['checkbox'].isChecked():
                selected_signals.append(unique_key)
        return selected_signals

    def select_all_signals(self):
        for unique_key, widgets in self.signal_widgets.items():
            if widgets['checkbox'].isVisible():
                widgets['checkbox'].setChecked(True)

    def deselect_all_signals(self):
        for unique_key, widgets in self.signal_widgets.items():
            if widgets['checkbox'].isVisible():
                widgets['checkbox'].setChecked(False)

    def _get_signal(self, signal_info):
        """获取信号数据，支持文字类型信号"""
        resolver = SignalResolver(
            self.signals,
            data_source=self.mdf_file,
            data_path=self.mdf_path,
            can_data=self.can_parsed_data,
            legacy_math_data=self.custom_math_data,
        )
        try:
            return resolver.resolve_info(signal_info).signal
        except SignalResolutionError as exc:
            if os.path.splitext(self.mdf_path)[1].lower() in ('.mdf', '.mf4'):
                print(f"MDF/MF4 获取信号失败: {exc}")
            return None

    @staticmethod
    def _configure_plot_y_axis(axis, numeric_extent=None):
        """Let Y tick labels determine axis width, reducing font for large values."""
        axis.setStyle(autoExpandTextSpace=True, autoReduceTextSpace=True)
        if numeric_extent is not None and numeric_extent >= 1000:
            tick_font = pg.QtGui.QFont()
            tick_font.setPointSizeF(8.0)
            axis.setStyle(tickFont=tick_font)
        axis.setWidth(None)

    def _close_overlay_y_axis_managers(self, *, preserve_state=True):
        """Close managers before their PlotWidgets/scenes are destroyed."""
        found_manager = False
        for plot_widget in tuple(getattr(self, "plot_widgets", ())):
            manager = getattr(plot_widget, "multi_y_axis_manager", None)
            if manager is None:
                continue
            found_manager = True
            if preserve_state:
                self._overlay_axis_scroll_position = manager.scroll_position
                self._overlay_y_axis_state = manager.close(preserve_state=True)
                if manager.current_signal_key is not None:
                    self._overlay_current_signal_key = manager.current_signal_key
            else:
                manager.close(preserve_state=False)
            plot_widget.multi_y_axis_manager = None
        if not preserve_state:
            self._overlay_y_axis_state = {}
            self._overlay_current_signal_key = None
            self._overlay_axis_scroll_position = 0
        return found_manager

    @staticmethod
    def _delete_plot_widget(plot_widget):
        """Delete the plot wrapper too when overlay axes use a scrollbar."""
        container = getattr(plot_widget, "axis_scroll_container", None)
        target = container or plot_widget
        target.setParent(None)
        target.deleteLater()

    def plot_selected_signals(self):
        self._close_overlay_y_axis_managers(preserve_state=True)
        for widget in self.plot_widgets:
            self._delete_plot_widget(widget)
        self.plot_widgets.clear()
        self.master_viewbox = None
        for index in reversed(range(self.plot_layout.count())):
            item = self.plot_layout.itemAt(index)
            if item is not None and item.spacerItem() is not None:
                self.plot_layout.takeAt(index)

        if not hasattr(self, 'rescale_timer'):
            self.rescale_timer = QTimer()
            self.rescale_timer.setSingleShot(True)
            self.rescale_timer.timeout.connect(self.execute_auto_scale)

        selected_signals = self.get_selected_signals()
        if not selected_signals:
            if hasattr(self, '_set_plot_empty_state'):
                self._set_plot_empty_state("请选择至少一个信号进行绘制")
            else:
                self.plot_layout.addWidget(QLabel("请选择至少一个信号进行绘制"))
            return

        all_data_list = []
        all_timestamps = []
        for unique_key in selected_signals:
            sig_info = self.signals.get(unique_key)
            if not sig_info:
                continue

            signal = self._get_signal(sig_info)
            if signal is not None and len(signal.timestamps) > 0:
                all_data_list.append((unique_key, sig_info, signal))
                all_timestamps.extend([signal.timestamps[0], signal.timestamps[-1]])

        if not all_data_list:
            if hasattr(self, '_set_plot_empty_state'):
                self._set_plot_empty_state("无法获取任何信号数据。")
            else:
                self.plot_layout.addWidget(QLabel("无法获取任何信号数据。"))
            return

        t_min = np.min(all_timestamps)
        t_max = np.max(all_timestamps)

        display_mode = getattr(self, "curve_display_mode", "independent")
        display_groups = (
            [(item,) for item in all_data_list]
            if display_mode == "independent"
            else [tuple(all_data_list)]
        )

        for i, signal_group in enumerate(display_groups):
            signal_key, signal_info, signal = signal_group[0]
            try:
                plot_widget = pg.PlotWidget(viewBox=LimitedViewBox())
                self.plot_widgets.append(plot_widget)
                use_axis_scrollbar = (
                    display_mode == "overlay" and len(signal_group) > 1
                )
                axis_scroll_bar = None
                if use_axis_scrollbar:
                    container = QWidget(self.plot_container)
                    container_layout = QVBoxLayout(container)
                    container_layout.setContentsMargins(0, 0, 0, 0)
                    container_layout.setSpacing(0)
                    container_layout.addWidget(plot_widget, 1)
                    axis_scroll_bar = QScrollBar(Qt.Orientation.Horizontal, container)
                    axis_scroll_bar.setObjectName("multiYAxisScrollBar")
                    axis_scroll_bar.setFixedHeight(14)
                    axis_scroll_bar.setVisible(False)
                    container_layout.addWidget(
                        axis_scroll_bar, 0, Qt.AlignmentFlag.AlignLeft
                    )
                    plot_widget.axis_scroll_container = container
                    plot_widget.y_axis_scroll_bar = axis_scroll_bar
                    self.plot_layout.addWidget(container, 1)
                else:
                    self.plot_layout.addWidget(plot_widget, 1)

                vb = plot_widget.getPlotItem().getViewBox()
                plot_widget.getPlotItem().enableAutoRange(x=False)
                vb.setMouseEnabled(x=True, y=False)
                vb.setLimits(xMin=t_min, xMax=t_max)

                left_axis = plot_widget.getPlotItem().getAxis('left')
                self._configure_plot_y_axis(left_axis)
                overlay_manager = None
                if display_mode == "overlay" and len(signal_group) > 1:
                    overlay_manager = MultiYAxisManager(
                        vb,
                        plot_widget.scene(),
                        initial_state=getattr(self, "_overlay_y_axis_state", {}),
                        plot_item=plot_widget.getPlotItem(),
                        plot_widget=plot_widget,
                        scroll_bar=axis_scroll_bar,
                        initial_scroll_position=getattr(
                            self, "_overlay_axis_scroll_position", 0
                        ),
                    )
                    plot_widget.multi_y_axis_manager = overlay_manager
                    # Until the scrollable multi-axis strip is implemented, the
                    # reference ViewBox's single Y axis would show a false scale.
                    plot_widget.getPlotItem().hideAxis("left")

                # Each display group owns one plot; overlay groups add one curve
                # per selected signal to that shared plot and its ViewBox.
                records = []
                legend = (
                    plot_widget.addLegend(offset=(8, 8))
                    if len(signal_group) > 1 else None
                )
                if legend is not None:
                    legend.setColumnCount(max(1, min(
                        len(signal_group), max(1, plot_widget.width() // 180)
                    )))
                y_min_data = float("inf")
                y_max_data = float("-inf")
                initial_signal_y_ranges = {}
                for curve_index, (item_key, item_info, item_signal) in enumerate(signal_group):
                    is_text_signal = bool(
                        getattr(item_signal, "is_text_signal", False)
                    )
                    if (
                        len(signal_group) == 1
                        and is_text_signal
                        and getattr(item_signal, "text_mapping", None) is not None
                    ):
                        axis = plot_widget.getPlotItem().getAxis("left")
                        axis.setTicks([[
                            (j, label)
                            for j, label in enumerate(item_signal.text_mapping)
                        ]])

                    samples = np.asarray(item_signal.samples)
                    valid_mask = ~np.isnan(samples)
                    if valid_mask.any():
                        signal_y_min = float(np.min(samples[valid_mask]))
                        signal_y_max = float(np.max(samples[valid_mask]))
                    else:
                        signal_y_min, signal_y_max = 0.0, 1.0
                    if valid_mask.any():
                        y_min_data = min(y_min_data, signal_y_min)
                        y_max_data = max(y_max_data, signal_y_max)

                    signal_y_range = signal_y_max - signal_y_min
                    signal_padding = (
                        1.0 if signal_y_min == 0 else abs(signal_y_min) * 0.1
                    ) if signal_y_range == 0 else signal_y_range * 0.4
                    if signal_padding == 0:
                        signal_padding = 1.0
                    if len(signal_group) == 1 and is_text_signal:
                        mapping = getattr(item_signal, "text_mapping", None) or ()
                        if mapping:
                            signal_y_min, signal_y_max = -0.5, len(mapping) - 0.5
                            signal_padding = 0.1
                    initial_signal_y_range = (
                        signal_y_min - signal_padding,
                        signal_y_max + signal_padding,
                    )
                    initial_signal_y_ranges[item_key] = initial_signal_y_range

                    color = (
                        self._signal_color(item_key)
                        if hasattr(self, "_signal_color")
                        else pg.intColor(curve_index, hues=max(1, len(signal_group)))
                    )
                    full_name = item_info.get(
                        "display_name", item_info.get("name", "信号")
                    )
                    signal_axis_item = None
                    if overlay_manager is not None:
                        signal_axis_item = pg.AxisItem(orientation="left")
                        self._configure_plot_y_axis(signal_axis_item)
                    signal_view_box = (
                        overlay_manager.register_signal(
                            item_key, initial_signal_y_range,
                            axis_item=signal_axis_item, axis_color=color,
                        )
                        if overlay_manager is not None
                        else vb
                    )
                    if overlay_manager is None:
                        curve = plot_widget.plot(
                            item_signal.timestamps,
                            item_signal.samples,
                            pen=pg.mkPen(color, width=1.4),
                            name=full_name,
                        )
                    else:
                        curve = pg.PlotDataItem(
                            item_signal.timestamps,
                            item_signal.samples,
                            pen=pg.mkPen(color, width=1.4),
                            name=full_name,
                        )
                        signal_view_box.addItem(curve)
                    curve_visible = (
                        self.is_signal_curve_visible(item_key)
                        if hasattr(self, "is_signal_curve_visible") else True
                    )
                    curve.setVisible(curve_visible)
                    if legend is not None and curve_visible:
                        legend.addItem(curve, full_name)
                    if legend is not None and legend.items and curve_visible:
                        legend_label = legend.items[-1][1]
                        font_metrics = QFontMetrics(legend_label.item.font())
                        legend_label.setText(font_metrics.elidedText(
                            full_name, Qt.TextElideMode.ElideRight, 140
                        ))
                        legend_label.setToolTip(full_name)
                    if not is_text_signal:
                        curve.setDownsampling(auto=True, method="peak")
                        curve.setClipToView(True)
                        plot_widget.sigXRangeChanged.connect(self.on_x_range_changed)

                    spot = pg.ScatterPlotItem(
                        size=8, pen=pg.mkPen(None), brush=pg.mkBrush(color)
                    )
                    signal_view_box.addItem(spot)
                    spot.setVisible(False)
                    records.append({
                        "signal_key": item_key,
                        "signal_info": item_info,
                        "signal_data": item_signal,
                        "data_curve": curve,
                        "curve_visible": curve_visible,
                        "view_box": signal_view_box,
                        "spot_item": spot,
                        "is_text_signal": is_text_signal,
                    })

                if not np.isfinite(y_min_data) or not np.isfinite(y_max_data):
                    y_min_data, y_max_data = 0.0, 1.0
                self._configure_plot_y_axis(
                    left_axis, max(abs(y_min_data), abs(y_max_data))
                )
                y_range = y_max_data - y_min_data
                y_padding = (
                    1.0 if y_min_data == 0 else abs(y_min_data) * 0.1
                ) if y_range == 0 else y_range * 0.4
                if y_padding == 0:
                    y_padding = 1.0
                if len(signal_group) == 1 and records[0]["is_text_signal"]:
                    mapping = getattr(signal, "text_mapping", None) or ()
                    if mapping:
                        y_min_data, y_max_data = -0.5, len(mapping) - 0.5
                        y_padding = 0.1
                plot_widget.setYRange(
                    y_min_data - y_padding, y_max_data + y_padding, padding=0
                )
                if overlay_manager is not None:
                    current_signal_key = getattr(
                        self, "_overlay_current_signal_key", None
                    )
                    if current_signal_key not in overlay_manager.signal_keys:
                        current_signal_key = records[0]["signal_key"]
                    overlay_manager.set_current_signal(current_signal_key)
                    overlay_manager.install_axis_layout()
                    self._overlay_current_signal_key = current_signal_key
                    plot_widget.initial_signal_y_ranges = {
                        record["signal_key"]: overlay_manager.y_range(
                            record["signal_key"]
                        )
                        for record in records
                    }
                else:
                    plot_widget.initial_signal_y_ranges = initial_signal_y_ranges

                title = f"信号: {signal_info.get('display_name', signal_info.get('name', ''))}"
                if len(signal_group) > 1:
                    visible_count = sum(
                        1 for record in records if record.get("curve_visible", True)
                    )
                    title = f"叠加显示 · {visible_count} 个信号"
                    hidden_count = len(records) - visible_count
                    if hidden_count:
                        title += f"（已隐藏 {hidden_count}）"
                bus_id = signal_info.get("bus_id")
                if bus_id is not None and len(signal_group) == 1:
                    title += f" [Bus {bus_id}]"
                if display_mode == "independent":
                    plot_widget.setTitle(title, size="10pt")
                else:
                    plot_widget.setTitle(title)

                cursor_green = DEFAULT_THEME.colors.success
                cursor_blue = DEFAULT_THEME.colors.primary
                v_line = pg.InfiniteLine(
                    angle=90, movable=False,
                    pen=pg.mkPen(cursor_green, width=1.5),
                )
                v_line.setVisible(False)
                plot_widget.addItem(v_line, ignoreBounds=True)
                measurement_lines = {}
                for cursor_name, cursor_color in (("A", cursor_green), ("B", cursor_blue)):
                    measurement_line = pg.InfiniteLine(
                        angle=90, movable=False,
                        pen=pg.mkPen(cursor_color, width=1.5),
                    )
                    measurement_line.setVisible(False)
                    plot_widget.addItem(measurement_line, ignoreBounds=True)
                    measurement_lines[cursor_name] = measurement_line

                cursor_labels = {"single": [], "A": [], "B": []}
                for record in records:
                    for cursor_name, cursor_color in (
                        ("single", cursor_green), ("A", cursor_green), ("B", cursor_blue)
                    ):
                        label = pg.TextItem(
                            text="", color=cursor_color, anchor=(0, 0.5),
                            fill=pg.mkColor(255, 255, 255, 220),
                            border=pg.mkColor(cursor_color),
                        )
                        label.setFont(pg.QtGui.QFont("Arial", 8))
                        label.setVisible(False)
                        record["view_box"].addItem(label, ignoreBounds=True)
                        cursor_labels[cursor_name].append(label)
                text_item = cursor_labels["single"][0]

                plot_widget.vLine = v_line
                plot_widget.measurement_lines = measurement_lines
                plot_widget.text_item = text_item
                plot_widget.cursor_labels = cursor_labels
                plot_widget.signal_records = records
                plot_widget.cursor_spots = [record["spot_item"] for record in records]
                plot_widget.spot_item = records[0]["spot_item"]
                plot_widget.signal_data = signal
                plot_widget.signal_info = signal_info
                plot_widget.is_text_signal = records[0]["is_text_signal"]
                plot_widget.data_curve = records[0]["data_curve"]
                plot_widget.legend_item = legend

                # ===== 右上角统计信息 =====
                stats_text_item = pg.TextItem(
                    text="",
                    color=(200, 200, 200),
                    anchor=(1, 0)  # 右上角对齐
                )
                # 设置字体
                font = pg.QtGui.QFont("Arial", 9)
                stats_text_item.setFont(font)
                # 添加到视图，ignoreBounds=True 使其不受视图边界裁剪
                plot_widget.addItem(stats_text_item, ignoreBounds=True)
                plot_widget.stats_text_item = stats_text_item

                # 保存对 plot_widget 的引用，用于回调
                plot_widget._stats_update_needed = True

                # 初始更新统计信息（延迟一点确保视图已渲染）
                QTimer.singleShot(50, lambda: self._update_stats(plot_widget))

                # 连接视图变化信号，使用 weak 引用避免内存泄漏
                def make_stats_callback(widget):
                    def callback():
                        if widget and widget.stats_text_item is not None:
                            self._update_stats(widget)

                    return callback

                vb.sigRangeChanged.connect(make_stats_callback(plot_widget))

                plot_widget.setMinimumHeight(150)
                if (
                    display_mode == "independent"
                    and hasattr(self, "is_signal_curve_visible")
                    and not self.is_signal_curve_visible(signal_key)
                ):
                    # A hidden signal is still selected, but must not produce an
                    # empty plot row when the user redraws the selection.
                    plot_widget.setVisible(False)

                plot_widget.setLabel('bottom', '时间', units='s')
                plot_widget.showGrid(x=True, y=overlay_manager is None)
                plot_widget.getPlotItem().layout.setContentsMargins(2, 2, 2, 2)

                if i == 0:
                    self.master_viewbox = vb
                    self.master_viewbox.setXRange(t_min, t_max)
                else:
                    vb.setXLink(self.master_viewbox)
                plot_widget.initial_view_range = tuple(
                    tuple(axis_range) for axis_range in vb.viewRange()
                )

            except Exception as e:
                print(f"警告: 无法绘制信号 '{signal_info.get('display_name')}'。错误: {e}")

        if self.map_dialog and self.map_dialog.isVisible():
            QTimer.singleShot(300, self.update_map_marker)

    def _update_stats(self, plot_widget):
        """更新曲线图右上角的统计信息"""
        try:
            # 检查 widget 是否有效
            if plot_widget is None:
                return

            signal_records = getattr(plot_widget, "signal_records", ())
            if len(signal_records) > 1:
                view_box = plot_widget.getViewBox()
                if view_box is None:
                    return
                x_min, x_max = view_box.viewRange()[0]
                stats_lines = []
                for record in signal_records:
                    signal = record["signal_data"]
                    start = int(np.searchsorted(signal.timestamps, x_min))
                    stop = int(np.searchsorted(signal.timestamps, x_max))
                    values = np.asarray(signal.samples[start:stop])
                    if not len(values):
                        continue
                    valid = values[~np.isnan(values)]
                    if not len(valid):
                        summary = "无有效数据"
                    else:
                        summary = (
                            f"最大 {np.max(valid):.3g} · 最小 {np.min(valid):.3g}"
                            f" · 平均 {np.mean(valid):.3g} · 标准差 {np.std(valid):.3g}"
                        )
                    name = record["signal_info"].get(
                        "display_name", record["signal_info"].get("name", "信号")
                    )
                    stats_lines.append(f"{name}: {summary}")
                item = plot_widget.stats_text_item
                if item is not None:
                    current = view_box.viewRange()
                    y_min, y_max = current[1]
                    item.setPos(current[0][1], y_max - (y_max - y_min) * 0.02)
                    item.setText("\n".join(stats_lines))
                return

            signal = plot_widget.signal_data
            if signal is None or len(signal.timestamps) == 0:
                return

            # 获取当前视图范围
            view_box = plot_widget.getViewBox()
            if view_box is None:
                return

            view_range = view_box.viewRange()
            if view_range is None or len(view_range) < 2:
                return

            x_min, x_max = view_range[0]
            y_min, y_max = view_range[1]

            # 获取当前视图内的数据
            idx_start = np.searchsorted(signal.timestamps, x_min)
            idx_end = np.searchsorted(signal.timestamps, x_max)

            if idx_start >= idx_end:
                stats_text = "无数据"
                stats_item = plot_widget.stats_text_item
                if stats_item:
                    stats_item.setPos(x_max, y_max)
                    stats_item.setText(stats_text)
                return

            visible_samples = signal.samples[idx_start:idx_end]

            if len(visible_samples) == 0:
                return

            # 判断是否为文字信号
            is_text_signal = hasattr(plot_widget, 'is_text_signal') and plot_widget.is_text_signal

            if is_text_signal and hasattr(signal, 'text_mapping') and signal.text_mapping:
                # ===== 文字信号：统计各文字占比 =====
                valid_mask = ~np.isnan(visible_samples)
                if not valid_mask.any():
                    stats_text = "无有效数据"
                else:
                    valid_values = visible_samples[valid_mask]
                    from collections import Counter
                    value_counts = Counter(valid_values.astype(int))

                    total_count = sum(value_counts.values())
                    stats_lines = []
                    for val, count in value_counts.items():
                        if 0 <= val < len(signal.text_mapping):
                            label = signal.text_mapping[val]
                            percentage = count / total_count * 100
                            stats_lines.append(f"{label}: {percentage:.1f}%")

                    if len(stats_lines) > 8:
                        stats_lines = stats_lines[:8]
                        stats_lines.append("...")

                    stats_text = "\n".join(stats_lines)
            else:
                # ===== 数值信号：统计最大、最小、平均值、标准差 =====
                valid_mask = ~np.isnan(visible_samples)
                if not valid_mask.any():
                    stats_text = "无有效数据"
                else:
                    valid_values = visible_samples[valid_mask]
                    min_val = np.min(valid_values)
                    max_val = np.max(valid_values)
                    mean_val = np.mean(valid_values)
                    std_val = np.std(valid_values)

                    # 根据数值范围决定显示精度
                    if np.issubdtype(valid_values.dtype, np.integer):
                        stats_text = f"最大: {max_val:.0f}\n最小: {min_val:.0f}\n平均: {mean_val:.1f}\n标准差: {std_val:.1f}"
                    elif np.max(np.abs(valid_values)) < 0.01:
                        stats_text = f"最大: {max_val:.6f}\n最小: {min_val:.6f}\n平均: {mean_val:.6f}\n标准差: {std_val:.6f}"
                    else:
                        stats_text = f"最大: {max_val:.3f}\n最小: {min_val:.3f}\n平均: {mean_val:.3f}\n标准差: {std_val:.3f}"

            # ===== 更新统计信息位置（始终在右上角） =====
            stats_item = plot_widget.stats_text_item
            if stats_item is None:
                return

            # 重新获取当前视图范围（确保是最新的）
            current_view = view_box.viewRange()
            x_max_current = current_view[0][1]
            y_max_current = current_view[1][1]
            y_min_current = current_view[1][0]

            # 计算Y轴位置（在顶部，留一点边距）
            y_range = y_max_current - y_min_current
            y_pos = y_max_current - y_range * 0.02

            # 设置文本内容和位置
            stats_item.setPos(x_max_current, y_pos)
            stats_item.setText(stats_text)

        except Exception as e:
            print(f"更新统计信息失败: {e}")

    def on_x_range_changed(self):
        if self.rescale_timer is None:
            self.rescale_timer = QTimer()
            self.rescale_timer.setSingleShot(True)
            self.rescale_timer.timeout.connect(self.execute_auto_scale)
        self.rescale_timer.start(150)

    def execute_auto_scale(self):
        for pw in self.plot_widgets:
            if not hasattr(pw, 'signal_data'):
                continue
            view_range = pw.getViewBox().viewRange()[0]
            y_axis_manager = getattr(pw, "multi_y_axis_manager", None)
            if y_axis_manager is not None:
                for record in getattr(pw, "signal_records", ()):
                    if not record.get("curve_visible", True):
                        continue
                    signal = record["signal_data"]
                    if record["is_text_signal"] and len(pw.signal_records) == 1:
                        continue
                    start = int(np.searchsorted(signal.timestamps, view_range[0]))
                    stop = int(np.searchsorted(signal.timestamps, view_range[1]))
                    values = np.asarray(signal.samples[start:stop])
                    if not len(values):
                        continue
                    valid = values[~np.isnan(values)]
                    if not len(valid):
                        continue
                    ymin, ymax = np.min(valid), np.max(valid)
                    yrange = ymax - ymin
                    padding = yrange * 0.1 if yrange > 0 else 1.0
                    y_axis_manager.set_y_range(
                        record["signal_key"], (ymin - padding, ymax + padding)
                    )
                continue
            visible_values = []
            for record in getattr(pw, "signal_records", ()):
                if not record.get("curve_visible", True):
                    continue
                signal = record["signal_data"]
                if record["is_text_signal"] and len(getattr(pw, "signal_records", ())) == 1:
                    continue
                start = int(np.searchsorted(signal.timestamps, view_range[0]))
                stop = int(np.searchsorted(signal.timestamps, view_range[1]))
                values = np.asarray(signal.samples[start:stop])
                if len(values):
                    valid = values[~np.isnan(values)]
                    if len(valid):
                        visible_values.append(valid)
            if visible_values:
                values = np.concatenate(visible_values)
                ymin, ymax = np.min(values), np.max(values)
                yrange = ymax - ymin
                padding = yrange * 0.1 if yrange > 0 else 1.0
                pw.setYRange(ymin - padding, ymax + padding, padding=0)

    def update_cursor_positions(self, evt, source_plot=None):
        if (
            self.cursor_mode != "single"
            or self._dragging_cursor is None
            or self._dragging_cursor.get("name") != "single"
            or not self.plot_widgets
            or self.master_viewbox is None
        ):
            return
        source_plot = source_plot or self._dragging_cursor.get("plot")
        if source_plot is None:
            return
        try:
            x_pos = float(source_plot.getViewBox().mapSceneToView(evt).x())
            x_min, x_max = self.master_viewbox.viewRange()[0]
            x_pos = min(max(x_pos, x_min), x_max)
        except (RuntimeError, TypeError, ValueError):
            return
        self._single_cursor_time = x_pos
        self._render_single_cursor_at(x_pos)
        master_y = sum(self.master_viewbox.viewRange()[1]) * 0.5
        self.update_map_marker(
            self.master_viewbox.mapViewToScene(QPointF(x_pos, master_y))
        )

    def _render_single_cursor_at(self, x_pos):
        if not self.plot_widgets:
            return

        for plot_widget in self.plot_widgets:
            records = getattr(plot_widget, "signal_records", ())
            if not records:
                continue
            cursor_x = x_pos
            single_labels = getattr(plot_widget, "cursor_labels", {}).get("single", ())
            for record_index, record in enumerate(records):
                signal = record["signal_data"]
                if signal is None or len(signal.timestamps) == 0:
                    record["spot_item"].setData([], [])
                    if record_index < len(single_labels):
                        single_labels[record_index].setVisible(False)
                    continue

                idx = int(np.clip(np.searchsorted(signal.timestamps, x_pos), 0, len(signal.timestamps) - 1))
                x_val = float(signal.timestamps[idx])
                y_val = signal.samples[idx]
                cursor_x = x_pos if len(records) > 1 else x_val

                if np.isnan(y_val):
                    record["spot_item"].setData([], [])
                    shown = "NaN (无效数据)"
                else:
                    if record["is_text_signal"]:
                        raw_text = getattr(signal, "raw_text_values", None)
                        mapping = getattr(signal, "text_mapping", None)
                        if raw_text is not None and len(raw_text) > idx:
                            shown = str(raw_text[idx])
                        elif mapping:
                            label_index = int(round(y_val))
                            shown = mapping[label_index] if 0 <= label_index < len(mapping) else str(y_val)
                        else:
                            shown = str(y_val)
                    else:
                        shown = f"{float(y_val):.6f}"
                    record["spot_item"].setData([x_val], [y_val])
                if record_index < len(single_labels):
                    label = single_labels[record_index]
                    label.setText(f"{cursor_x:.6f}, {shown}")
                    curve_visible = record.get("curve_visible", True)
                    record["spot_item"].setVisible(
                        self.cursor_mode == "single" and curve_visible
                    )
                    label.setVisible(
                        self.cursor_mode == "single" and curve_visible
                    )
                    self._position_cursor_label(
                        plot_widget, label, cursor_x,
                        float(y_val) if not np.isnan(y_val) else None,
                        view_box=record.get("view_box"),
                    )

            plot_widget.vLine.setPos(cursor_x)
            plot_widget.text_item = single_labels[0] if single_labels else plot_widget.text_item
            self._separate_cursor_labels(plot_widget, "single")

    def convert_to_decimal(self, series, is_lat=True):
        samples = pd.to_numeric(series, errors='coerce').astype(float)
        abs_val = samples.abs()

        if abs_val.max() > 1e6:
            decimal = samples / 1e6
        elif abs_val.max() > 180:
            total_minutes_decimal = abs_val / 60.0
            if total_minutes_decimal.max() <= 190:
                unsigned_decimal = total_minutes_decimal
            else:
                deg = (abs_val // 100).astype(int)
                minutes = abs_val % 100
                unsigned_decimal = deg + minutes / 60.0

            if is_lat:
                decimal = np.sign(samples) * unsigned_decimal
            else:
                decimal = -np.sign(samples) * unsigned_decimal
        else:
            decimal = samples

        if is_lat:
            decimal = np.clip(decimal, -90, 90)
        else:
            decimal = np.clip(decimal, -180, 180)

        return decimal

    def show_map(self):
        lat_key = self.lat_combo.currentData()
        lon_key = self.lon_combo.currentData()

        if not lat_key or not lon_key:
            QMessageBox.warning(self, "显示轨迹", "请选择纬度和经度信号。")
            return False

        lat_info = self.signals.get(lat_key)
        lon_info = self.signals.get(lon_key)
        if not lat_info or not lon_info:
            QMessageBox.warning(self, "显示轨迹", "无法获取选定的信号信息。")
            return False

        lat_signal = self._get_signal(lat_info)
        lon_signal = self._get_signal(lon_info)
        if lat_signal is None or lon_signal is None or len(lat_signal) < 2:
            QMessageBox.warning(self, "显示轨迹", "GPS信号为空或数据不足。")
            return False

        df_lat = pd.DataFrame({'t': lat_signal.timestamps, 'lat_raw': lat_signal.samples})
        df_lon = pd.DataFrame({'t': lon_signal.timestamps, 'lon_raw': lon_signal.samples})
        df = pd.merge(df_lat, df_lon, on='t', how='outer').sort_values('t')

        df['lat'] = self.convert_to_decimal(df['lat_raw'], is_lat=True)
        df['lon'] = self.convert_to_decimal(df['lon_raw'], is_lat=False)

        df['valid'] = (df['lat'].abs() > 1e-5) & (df['lon'].abs() > 1e-5) & df['lat'].notna() & df['lon'].notna()

        df['lat'] = df['lat'].interpolate(method='linear', limit=500, limit_direction='both')
        df['lon'] = df['lon'].interpolate(method='linear', limit=500, limit_direction='both')
        df['valid'] = (df['lat'].abs() > 1e-5) & (df['lon'].abs() > 1e-5) & df['lat'].notna() & df['lon'].notna()

        df['group'] = df['valid'].ne(df['valid'].shift()).cumsum()

        segments = []
        for _, group in df.groupby('group'):
            if group['valid'].any():
                sub_seg = group.dropna(subset=['lat', 'lon'])
                if len(sub_seg) >= 2:
                    segments.append(sub_seg)

        if not segments:
            QMessageBox.warning(self, "显示轨迹", "未检测到任何有效GPS点（全为0,0、无效格式或断续太严重）。")
            return False

        df_clean = pd.concat(segments, ignore_index=True)

        max_points = 5000
        if len(df_clean) > max_points:
            step = len(df_clean) // max_points
            df_clean = df_clean.iloc[::step].reset_index(drop=True)

        self.gps_df = df_clean[['t', 'lat', 'lon']].reset_index(drop=True)

        center_lat = df_clean['lat'].mean()
        center_lon = df_clean['lon'].mean()

        js_segments = []
        for seg in segments:
            if len(seg) > 2000:
                seg = seg.iloc[::(len(seg) // 2000)].reset_index(drop=True)
            seg_js = "[" + ",\n        ".join([f"[{row['lat']}, {row['lon']}]" for _, row in seg.iterrows()]) + "]"
            js_segments.append(seg_js)

        all_segments_js = ",\n        ".join(js_segments)

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>GPS实时轨迹</title>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
            <style> body {{ margin:0; padding:0; }} #map {{ position:absolute; top:0; bottom:0; right:0; left:0; }} </style>
        </head>
        <body>
        <div id="map"></div>
        <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
        <script>
            var map = L.map('map', {{ maxZoom: 22 }}).setView([{center_lat}, {center_lon}], 16);
            var googleHybrid = L.tileLayer('https://mt1.google.com/vt/lyrs=s&x={{x}}&y={{y}}&z={{z}}', {{ maxZoom: 22, maxNativeZoom: 20, attribution: 'Google' }}).addTo(map);
            var googleSatellite = L.tileLayer('https://mt1.google.com/vt/lyrs=s&x={{x}}&y={{y}}&z={{z}}', {{ maxZoom: 22, maxNativeZoom: 20, attribution: 'Google' }});
            var osm = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{ maxZoom: 19, attribution: '&copy; OpenStreetMap' }});

            var baseMaps = {{ "卫星混合图": googleHybrid, "纯卫星图": googleSatellite, "街道图": osm }};
            L.control.layers(baseMaps).addTo(map);

            var pathSegments = [ {all_segments_js} ];
            var bounds = L.latLngBounds();

            pathSegments.forEach(function(coords) {{
                if (coords.length >= 2) {{
                    var polyline = L.polyline(coords, {{color: '#00FF00', weight: 3, opacity: 0.9}}).addTo(map);
                    bounds.extend(polyline.getBounds());
                }}
            }});

            if (pathSegments.length > 0) {{
                var firstSeg = pathSegments[0];
                var lastSeg = pathSegments[pathSegments.length - 1];
                L.circleMarker(firstSeg[0], {{radius: 6, color: 'green', fillColor: 'lime', fillOpacity: 1}}).addTo(map).bindPopup('起点');
                L.circleMarker(lastSeg[lastSeg.length - 1], {{radius: 6, color: 'red', fillColor: 'crimson', fillOpacity: 1}}).addTo(map).bindPopup('终点');
            }}

            if (bounds.isValid()) {{ map.fitBounds(bounds.pad(0.15)); }}

            var currentMarker = null;
            function updateBlueDot(lat, lon) {{
                if (currentMarker) {{ currentMarker.setLatLng([lat, lon]); }}
                else {{
                    currentMarker = L.circleMarker([lat, lon], {{ radius: 9, weight: 2, color: 'white', fillColor: '#3388ff', fillOpacity: 1 }}).addTo(map);
                }}
                map.panTo([lat, lon], {{animate: true, duration: 0.2}});
            }}
        </script>
        </body>
        </html>
        """

        if self.map_dialog is None:
            self.map_dialog = QDialog(self)
            self.map_dialog.setWindowTitle("GPS轨迹图")
            self.map_dialog.resize(900, 650)
            self.map_dialog.finished.connect(self._gps_dialog_finished)

            layout = QVBoxLayout(self.map_dialog)
            self.web_view = QWebEngineView()
            self.web_view.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
            self.web_view.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
            layout.addWidget(self.web_view)
            self.web_view.loadFinished.connect(self._gps_map_loaded)

        self.web_view.setHtml(html_content)
        self.map_dialog.show()
        return True

    def _gps_map_loaded(self, ok):
        if ok and self.map_dialog is not None and self.map_dialog.isVisible():
            QTimer.singleShot(500, self.update_map_marker)

    def update_map_marker(self, evt=None):
        if self.gps_df is None or not hasattr(self, 'web_view') or not self.web_view:
            return
        if not self.map_dialog or not self.map_dialog.isVisible():
            return
        if self.master_viewbox is None:
            return

        if evt is not None:
            mouse_point = self.master_viewbox.mapSceneToView(evt)
            current_t = mouse_point.x()
        else:
            view_range = self.master_viewbox.viewRange()[0]
            current_t = (view_range[0] + view_range[1]) / 2

        gps_t_values = self.gps_df['t'].values
        if len(gps_t_values) == 0:
            return

        t_relative = current_t - self.plot_widgets[0].signal_data.timestamps[0] if self.plot_widgets else current_t
        gps_start_t = gps_t_values[0]
        target_t = gps_start_t + t_relative

        idx = np.searchsorted(gps_t_values, current_t)
        if idx >= len(gps_t_values) or (idx > 0 and abs(gps_t_values[idx - 1] - current_t) > 10):
            idx = np.searchsorted(gps_t_values, target_t)

        idx = np.clip(idx, 0, len(gps_t_values) - 1)

        lat = float(self.gps_df.iloc[idx]['lat'])
        lon = float(self.gps_df.iloc[idx]['lon'])

        js_code = f"if(typeof updateBlueDot === 'function') {{ updateBlueDot({lat}, {lon}); }}"
        self.web_view.page().runJavaScript(js_code)

    def save_data_cutout(self):
        if getattr(self, '_cutout_worker', None) is not None and self._cutout_worker.isRunning():
            QMessageBox.information(self, "数据截取", "已有截取任务正在运行，请等待完成或取消。")
            return
        if self.master_viewbox is None:
            QMessageBox.warning(self, "数据截取", "请先加载文件并绘制信号。")
            return

        try:
            view_range = self.master_viewbox.viewRange()[0]
            window = CutoutWindow(view_range[0], view_range[1])
        except Exception:
            QMessageBox.warning(self, "数据截取", "无法获取当前视图的有效时间范围。")
            return

        try:
            kind = source_kind(self.mdf_path)
        except Exception as exc:
            QMessageBox.warning(self, "数据截取", str(exc))
            return

        context = self._build_cutout_context(kind, window)
        settings = getattr(self, "_gui_settings", None)
        dialog = CutoutDialog(
            context,
            initial_dir=os.path.dirname(self.mdf_path) or None,
            remembered_mode=load_last_mode(settings, kind.value),
            settings=settings,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        option = dialog.selected_option()
        if option is None:
            return

        try:
            job = self._build_cutout_job(context, option, dialog.output_path())
        except Exception as exc:
            QMessageBox.critical(self, "数据截取失败", str(exc))
            return

        worker = CutoutWorker(job, self)
        progress_dialog = CutoutProgressDialog(self)
        worker.progress.connect(progress_dialog.set_progress)
        worker.completed.connect(self._finish_cutout)
        worker.failed.connect(self._fail_cutout)
        worker.cancelled.connect(self._cancel_cutout)
        worker.finished.connect(lambda: self._cleanup_cutout(worker, progress_dialog))
        progress_dialog.cancel_requested.connect(self._request_cutout_cancel)
        self._cutout_worker = worker
        self._cutout_progress = progress_dialog
        self._cutout_option = option
        worker.start()
        progress_dialog.show()

    def _finish_cutout(self, result):
        if self._cutout_progress is not None:
            self._cutout_progress.close()
        if not getattr(self, '_close_after_cutout', False):
            self._show_cutout_result(result, getattr(self, '_cutout_option', None))

    def _fail_cutout(self, message):
        if self._cutout_progress is not None:
            self._cutout_progress.close()
        if not getattr(self, '_close_after_cutout', False):
            QMessageBox.critical(self, "数据截取失败", message)

    def _cancel_cutout(self):
        if self._cutout_progress is not None:
            self._cutout_progress.close()

    def _request_cutout_cancel(self):
        worker = getattr(self, '_cutout_worker', None)
        if worker is not None:
            worker.cancel()

    def _cleanup_cutout(self, worker, dialog):
        if worker is not getattr(self, '_cutout_worker', None):
            dialog.close()
            dialog.deleteLater()
            worker.deleteLater()
            return
        self._cutout_worker = None
        self._cutout_progress = None
        self._cutout_option = None
        dialog.close()
        dialog.deleteLater()
        worker.deleteLater()
        if getattr(self, '_close_after_cutout', False):
            self._close_after_cutout = False
            self.close()

    # ------------------------------------------------------------- 数据截取内部
    @staticmethod
    def _valid_signal_arrays(data):
        if data is None:
            return None
        timestamps = data.get('timestamps')
        samples = data.get('samples')
        if timestamps is None or samples is None:
            return None
        if len(timestamps) == 0 or len(timestamps) != len(samples):
            return None
        return data

    def _cutout_parsed_signal(self, key):
        return self._valid_signal_arrays(self.can_parsed_data.get(key))

    def _cutout_math_signal(self, key):
        return self._valid_signal_arrays(self.custom_math_data.get(key))

    @staticmethod
    def _is_mdf_file_signal(info):
        group = info.get('group')
        channel = info.get('channel')
        return (
            isinstance(group, int) and not isinstance(group, bool) and group >= 0
            and isinstance(channel, int) and not isinstance(channel, bool) and channel >= 0
        )

    def _cutout_csv_columns(self):
        frame = self.mdf_file
        if isinstance(frame, pd.DataFrame):
            return {str(column) for column in frame.columns}
        return set()

    def _cutout_selected_csv_available(self, selected):
        columns = self._cutout_csv_columns()
        return sum(
            1 for key in selected
            if key in columns or self._cutout_math_signal(key) is not None
        )

    def _build_cutout_context(self, kind, window):
        selected = list(self.get_selected_signals())
        if kind in (SourceKind.BLF, SourceKind.CAN_LOG):
            parsed = [
                key for key in self.can_parsed_data
                if self._cutout_parsed_signal(key) is not None
            ]
            available = sum(
                1 for key in selected if self._cutout_parsed_signal(key) is not None
            )
            return CutoutContext(
                self.mdf_path, kind, window, len(selected), available, 0, len(parsed)
            )
        if kind is SourceKind.MDF:
            file_keys = [
                key for key, info in self.signals.items()
                if self._is_mdf_file_signal(info)
            ]
            available = sum(
                1 for key in selected
                if self._is_mdf_file_signal(self.signals.get(key, {}))
            )
            return CutoutContext(
                self.mdf_path, kind, window, len(selected), available, len(file_keys), 0
            )
        return CutoutContext(
            self.mdf_path, kind, window, len(selected),
            self._cutout_selected_csv_available(selected), len(self._cutout_csv_columns()), 0,
        )

    def _cutout_mdf_selection(self):
        channels, skipped = [], []
        for key in self.get_selected_signals():
            info = self.signals.get(key, {})
            name = info.get('display_name', key)
            if self._is_mdf_file_signal(info):
                channels.append((str(info.get('name', key)), int(info['group']), int(info['channel'])))
            elif key in self.custom_math_data:
                skipped.append(f"{name}（公式计算通道，不写入 MDF）")
            else:
                skipped.append(f"{name}（没有可用的数据）")
        if not channels:
            raise ValueError("已选信号不属于该文件，没有可导出的通道。")
        return tuple(channels), tuple(skipped)

    def _cutout_mdf_series(self, scope):
        if scope is OutputScope.ALL:
            candidates = list(self.can_parsed_data)
        else:
            candidates = list(self.get_selected_signals())
        series, skipped = [], []
        for key in candidates:
            info = self.signals.get(key, {})
            data = self._cutout_parsed_signal(key)
            if data is None:
                if scope is OutputScope.SELECTED:
                    name = info.get('display_name', key)
                    if key in self.custom_math_data:
                        skipped.append(f"{name}（公式计算通道，不写入 MDF）")
                    else:
                        skipped.append(f"{name}（没有可用的解析数据）")
                continue
            series.append(SignalSeries(
                name=clean_signal_name(info.get('display_name', key), info.get('name', key)),
                unit=str(data.get('unit') or ''),
                comment=str(info.get('comment') or data.get('comment') or ''),
                timestamps=data['timestamps'],
                samples=data['samples'],
            ))
        if not series:
            raise ValueError("没有可写入 MDF 的解析信号数据。")
        return tuple(series), tuple(skipped)

    def _cutout_csv_selection(self):
        columns, calculated, skipped = [], [], []
        frame_columns = self._cutout_csv_columns()
        for key in self.get_selected_signals():
            info = self.signals.get(key, {})
            if key in frame_columns:
                columns.append(key)
                continue
            data = self._cutout_math_signal(key)
            if data is None:
                skipped.append(f"{info.get('display_name', key)}（没有可用数据）")
                continue
            calculated.append(SignalSeries(
                name=str(info.get('display_name', key)),
                unit=str(data.get('unit') or ''),
                comment=str(info.get('comment') or ''),
                timestamps=data['timestamps'],
                samples=data['samples'],
            ))
        if not columns and not calculated:
            raise ValueError("已选信号没有可导出的数据。")
        return tuple(columns), tuple(calculated), tuple(skipped)

    def _cutout_total_messages(self):
        total = 0
        for data in getattr(self, 'can_bus_data', {}).values():
            try:
                total += int(data.get('msg_count') or 0)
            except (AttributeError, TypeError, ValueError):
                continue
        return total

    def _build_cutout_job(self, context, option, output_path):
        job_kwargs = dict(
            source_path=self.mdf_path,
            kind=context.kind,
            window=context.window,
            output_path=str(output_path),
            format=option.format,
            scope=option.scope,
        )
        notes = ()
        if option.format is OutputFormat.BLF:
            job_kwargs['mapping'] = getattr(self, 'can_bus_mapping', None)
            job_kwargs['total_messages'] = self._cutout_total_messages()
        elif option.format is OutputFormat.MDF and context.kind is SourceKind.MDF:
            channels = None
            if option.scope is OutputScope.SELECTED:
                channels, notes = self._cutout_mdf_selection()
            job_kwargs['channels'] = channels
        elif option.format is OutputFormat.MDF:
            series, notes = self._cutout_mdf_series(option.scope)
            job_kwargs['series'] = series
        else:
            frame = self.mdf_file
            if not isinstance(frame, pd.DataFrame):
                raise ValueError("内部错误：当前文件对象不是表格数据。")
            if option.scope is OutputScope.ALL:
                columns, calculated = None, ()
            else:
                columns, calculated, notes = self._cutout_csv_selection()
            job_kwargs.update(frame=frame, columns=columns, calculated=calculated)
        job_kwargs['notes'] = tuple(notes)
        return CutoutJob(**job_kwargs)

    def _execute_cutout(self, context, option, output_path):
        """Synchronous path kept for tests and for callers without a GUI loop."""
        job = self._build_cutout_job(context, option, output_path)
        return execute_cutout_job(job)

    def _show_cutout_result(self, result, option):
        lines = [f"输出文件：{result.output_path}"]
        if result.message_count:
            lines.append(
                f"内容：{len(result.bus_counts)} 个总线，{result.message_count:,} 条原始报文"
            )
            if 1 < len(result.bus_counts) <= 8:
                detail = "，".join(
                    f"Bus {bus_id}: {count:,} 条" for bus_id, count in result.bus_counts
                )
                lines.append(f"　　　　{detail}")
        elif option.format is OutputFormat.MDF:
            detail = f"内容：{result.channel_count:,} 个信号通道"
            if result.sample_count:
                detail += f"，{result.sample_count:,} 个采样点"
            lines.append(detail)
        else:
            lines.append(f"内容：{result.row_count:,} 行 × {result.channel_count} 列")
        lines.append(f"耗时：{result.elapsed:.2f} s")
        if result.skipped:
            lines.append("")
            lines.append(f"注意：{len(result.skipped)} 个信号未写入：")
            lines.extend(f"· {item}" for item in result.skipped[:6])
            if len(result.skipped) > 6:
                lines.append("· …")
        QMessageBox.information(self, "数据截取完成", "\n".join(lines))

    def cut_blf_by_time(self, input_path, output_path, start_time, stop_time, bus_filter=None):
        """Write one relative-time window of a BLF into a new BLF.

        The window is measured from the first message of the file.  Parsing and
        plotting use that same file time base for every bus, so the cut must use
        a single global t0 as well; using a per-channel t0 would put every other
        bus at relative time 0 and change which messages end up in the output.
        """
        from .data_cutout import CutoutWindow, cut_blf_raw

        try:
            window = CutoutWindow(start_time, stop_time)
            result = cut_blf_raw(
                input_path,
                output_path,
                window,
                mapping=getattr(self, 'can_bus_mapping', None),
                bus_filter=bus_filter,
                reader_factory=can.BLFReader,
                writer_factory=can.BLFWriter,
            )
        except Exception as exc:
            print(f"BLF截取失败: {exc}")
            raise Exception(f"BLF截取失败: {exc}")

        print(f"BLF截取完成: 共截取 {result.message_count} 条报文")
        for bus_id, count in result.bus_counts:
            print(f"  总线 {bus_id}: {count} 条")
        return True

    def export_selected_signals_to_csv(self):
        selected_signals = self.get_selected_signals()
        if not selected_signals:
            QMessageBox.warning(self, "导出CSV", "请选择至少一个信号进行导出。")
            return

        file_name, _ = QFileDialog.getSaveFileName(self, "导出CSV", "selected_signals.csv", "CSV Files (*.csv)")
        if not file_name:
            return

        try:
            all_timestamps = []
            for unique_key in selected_signals:
                signal_info = self.signals.get(unique_key)
                if not signal_info:
                    continue

                signal = self._get_signal(signal_info)
                if signal is not None and len(signal.timestamps) > 0:
                    all_timestamps.extend(signal.timestamps)

            if not all_timestamps:
                QMessageBox.warning(self, "导出CSV", "无法获取信号数据。")
                return

            t_start = np.min(all_timestamps)
            t_end = np.max(all_timestamps)

            target_frequency, ok = QInputDialog.getDouble(
                self, '重采样频率', '请输入目标重采样频率 (Hz):', value=10.0, min=0.1, max=10000.0, decimals=2
            )
            if not ok or target_frequency <= 0:
                return

            raster = np.arange(t_start, t_end + 1 / target_frequency, 1 / target_frequency)
            data = {}
            for unique_key in selected_signals:
                signal_info = self.signals.get(unique_key)
                if not signal_info:
                    continue

                try:
                    signal = self._get_signal(signal_info)
                    if hasattr(signal, 'resample'):
                        resampled_signal = signal.resample(raster)
                        data[signal_info['display_name']] = resampled_signal.samples
                    else:
                        df_signal = pd.DataFrame({'timestamp': signal.timestamps, 'samples': signal.samples}).set_index(
                            'timestamp')
                        df_raster = pd.DataFrame({'timestamp': raster}).set_index('timestamp')
                        df_aligned = pd.merge_asof(df_raster, df_signal, on='timestamp', direction='nearest')
                        data[signal_info['display_name']] = df_aligned['samples'].values
                except Exception as e:
                    print(f"导出信号 '{signal_info['display_name']}' 出错: {e}")

            if not data:
                return

            df_resampled = pd.DataFrame(data, index=raster)
            df_resampled.index.name = 'timestamp'
            df_resampled.to_csv(file_name, encoding='utf-8')
            QMessageBox.information(self, "导出CSV成功", f"成功导出信号到 {file_name}")
        except Exception as e:
            QMessageBox.critical(self, "导出CSV失败", f"导出CSV时出错: {e}")

    def create_math_channel(self):
        if not self.signals:
            QMessageBox.warning(self, "计算通道", "当前未加载任何有效数据文件。")
            return

        dialog = MathChannelDialog(self.signals, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            config = dialog.get_config()

            key_a = config["key_a"]
            key_b = config["key_b"]
            op = config["op"]
            policy = config["policy"]
            new_name = config["new_name"]

            if not new_name:
                QMessageBox.warning(self, "错误", "新信号通道的名称不能为空。")
                return

            unique_math_key = f"MATH_{new_name}"
            if unique_math_key in self.signals:
                QMessageBox.warning(self, "错误", f"信号名称 '{new_name}' 已存在，请换一个名称。")
                return

            sig_a = self._get_signal(self.signals[key_a])
            sig_b = self._get_signal(self.signals[key_b])

            if sig_a is None or sig_b is None:
                QMessageBox.critical(self, "错误", "无法获取参与计算的信号实体数据。")
                return

            t_base = sig_a.timestamps
            samples_a = sig_a.samples
            samples_b_aligned = np.interp(t_base, sig_b.timestamps, sig_b.samples)

            try:
                if op == '+':
                    result_samples = samples_a + samples_b_aligned
                elif op == '-':
                    result_samples = samples_a - samples_b_aligned
                elif op == '*':
                    result_samples = samples_a * samples_b_aligned
                elif op == '/':
                    is_zero_mask = np.abs(samples_b_aligned) < 1e-7
                    with np.errstate(divide='ignore', invalid='ignore'):
                        if policy == 'nan':
                            result_samples = np.where(is_zero_mask, np.nan, samples_a / samples_b_aligned)
                        else:
                            result_samples = np.where(is_zero_mask, 0.0, samples_a / samples_b_aligned)
                else:
                    raise Exception("未知的数学运算符")

                self.custom_math_data[unique_math_key] = {
                    'timestamps': t_base,
                    'samples': result_samples
                }

                self.signals[unique_math_key] = {
                    'name': unique_math_key,
                    'display_name': f"🧮 {new_name}",
                    'group': -99,
                    'channel': -99,
                    'comment': f"计算: [{self.signals[key_a]['name']}] {op} [{self.signals[key_b]['name']}]",
                    'math_definition': {
                        'key_a': key_a,
                        'key_b': key_b,
                        'op': op,
                        'policy': policy,
                        'display_name': new_name
                    }
                }

                self.refresh_signal_list_ui()

                if unique_math_key in self.signal_widgets:
                    self.signal_widgets[unique_math_key]['checkbox'].setChecked(True)

                QMessageBox.information(self, "计算通道", f"数学计算通道 '{new_name}' 创建成功！")

            except Exception as ex:
                QMessageBox.critical(self, "计算失败", f"数学通道运算出错: {ex}")

    def save_signal_config(self):
        selected_keys = self.get_selected_signals()
        if not selected_keys:
            QMessageBox.warning(self, "保存", "未选择任何信号。")
            return

        file_path, _ = QFileDialog.getSaveFileName(self, "保存配置", "", "JSON Files (*.json)")
        if not file_path:
            return

        math_configs = {}
        for key, info in self.signals.items():
            if key.startswith("MATH_") and 'math_definition' in info:
                if info['math_definition']:
                    math_configs[key] = info['math_definition']

        bus_config = {}
        for bus_id, data in self.can_bus_data.items():
            bus_config[str(bus_id)] = {
                'name': data.get('name', f"Bus {bus_id}"),
                'protocol': self.bus_protocols.get(bus_id, ''),
            }

        config_data = {
            "selected_signals": selected_keys,
            "gps_lat_key": self.lat_combo.currentData(),
            "gps_lon_key": self.lon_combo.currentData(),
            "math_channels": math_configs,
            "bus_config": bus_config
        }

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=4)
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))

    def load_signal_config(self):
        file_name, _ = QFileDialog.getOpenFileName(self, "加载信号配置", "", "JSON Files (*.json)")
        if not file_name:
            return

        try:
            with open(file_name, 'r', encoding='utf-8') as f:
                config_data = json.load(f)

            if not self.signals:
                QMessageBox.warning(self, "错误", "请先加载数据文件，再加载配置。")
                return

            bus_config = config_data.get("bus_config", {})
            for bus_id_str, config in bus_config.items():
                bus_id = int(bus_id_str)
                if bus_id in self.can_bus_data:
                    self.can_bus_data[bus_id]['name'] = config.get('name', f"Bus {bus_id}")
                    protocol = config.get('protocol', '')
                    if protocol:
                        self.bus_protocols[bus_id] = protocol
                        if bus_id not in self.db_per_bus:
                            self.db_per_bus[bus_id] = {}

                        if bus_id in self.bus_config_widgets:
                            self.bus_config_widgets[bus_id].name_edit.setText(self.can_bus_data[bus_id]['name'])
                            if protocol:
                                self.bus_config_widgets[bus_id].set_protocol_display(
                                    os.path.basename(protocol)
                                )
                                self.bus_config_widgets[bus_id].protocol_path = protocol

            math_defs = config_data.get("math_channels", {})
            for key, defs in math_defs.items():
                if key not in self.signals:
                    self.rebuild_math_channel(key, defs)

            self.refresh_signal_list_ui()

            selected_keys = set(config_data.get("selected_signals", []))
            for unique_key, widgets in self.signal_widgets.items():
                if unique_key in selected_keys:
                    widgets['checkbox'].setChecked(True)

            lat_key = config_data.get("gps_lat_key")
            lon_key = config_data.get("gps_lon_key")
            if lat_key:
                idx = self.lat_combo.findData(lat_key)
                if idx >= 0:
                    self.lat_combo.setCurrentIndex(idx)
            if lon_key:
                idx = self.lon_combo.findData(lon_key)
                if idx >= 0:
                    self.lon_combo.setCurrentIndex(idx)

            self.plot_selected_signals()

        except Exception as e:
            QMessageBox.critical(self, "加载失败", f"配置文件解析出错: {e}")

    def rebuild_math_channel(self, unique_key, defs):
        try:
            key_a = defs.get('key_a')
            key_b = defs.get('key_b')
            op = defs.get('op')
            policy = defs.get('policy', 'zero')

            if key_a not in self.signals or key_b not in self.signals:
                return

            sig_a = self._get_signal(self.signals[key_a])
            sig_b = self._get_signal(self.signals[key_b])

            if sig_a is None or sig_b is None:
                return

            samples_b_aligned = np.interp(sig_a.timestamps, sig_b.timestamps, sig_b.samples)

            if op == '+':
                result_samples = sig_a.samples + samples_b_aligned
            elif op == '-':
                result_samples = sig_a.samples - samples_b_aligned
            elif op == '*':
                result_samples = sig_a.samples * samples_b_aligned
            elif op == '/':
                is_zero_mask = np.abs(samples_b_aligned) < 1e-7
                with np.errstate(divide='ignore', invalid='ignore'):
                    if policy == 'nan':
                        result_samples = np.where(is_zero_mask, np.nan, sig_a.samples / samples_b_aligned)
                    else:
                        result_samples = np.where(is_zero_mask, 0.0, sig_a.samples / samples_b_aligned)
            else:
                return

            self.custom_math_data[unique_key] = {
                'timestamps': sig_a.timestamps,
                'samples': result_samples
            }

            display_name = defs.get('display_name', unique_key.replace("MATH_", ""))

            self.signals[unique_key] = {
                'name': unique_key,
                'display_name': f"🧮 {display_name}",
                'group': -99,
                'channel': -99,
                'comment': f"计算通道: {defs.get('key_a')} {defs.get('op')} {defs.get('key_b')}",
                'math_definition': defs
            }

        except Exception as e:
            print(f"计算通道 {unique_key} 重建失败: {e}")

    def closeEvent(self, event):
        """关闭窗口时清理所有资源"""
        print("🔄 开始清理资源...")

        # 停止所有解析线程
        for bus_id, worker in self.parse_workers.items():
            if worker.isRunning():
                print(f"  ⏹ 停止解析线程 Bus {bus_id}...")
                worker.stop()
                worker.wait(3000)
                if worker.isRunning():
                    print(f"  ⚠️ 线程 Bus {bus_id} 未能正常停止，强制终止")
                    worker.terminate()
            worker.deleteLater()
        self.parse_workers.clear()

        # 关闭所有进度对话框
        for bus_id, dialog in self.progress_dialogs.items():
            try:
                dialog.close()
                dialog.deleteLater()
            except Exception:
                pass
        self.progress_dialogs.clear()

        # 关闭 MDF 文件
        if hasattr(self, 'mdf_file') and self.mdf_file is not None:
            if isinstance(self.mdf_file, asammdf.MDF):
                try:
                    self.mdf_file.close()
                    print("  📁 MDF文件已关闭")
                except Exception:
                    pass
            self.mdf_file = None

        # 清理绘图资源
        if hasattr(self, 'plot_widgets'):
            self._close_overlay_y_axis_managers(preserve_state=False)
            for widget in self.plot_widgets:
                try:
                    self._delete_plot_widget(widget)
                except Exception:
                    pass
            self.plot_widgets.clear()

        if hasattr(self, 'plot_layout'):
            while self.plot_layout.count():
                item = self.plot_layout.takeAt(0)
                if item:
                    widget = item.widget()
                    if widget:
                        widget.setParent(None)
                        widget.deleteLater()

        # 清理信号列表
        if hasattr(self, 'signal_widgets'):
            for key, widgets in self.signal_widgets.items():
                try:
                    if widgets.get('checkbox'):
                        widgets['checkbox'].setParent(None)
                        widgets['checkbox'].deleteLater()
                    if widgets.get('comment_label'):
                        widgets['comment_label'].setParent(None)
                        widgets['comment_label'].deleteLater()
                except Exception:
                    pass
            self.signal_widgets.clear()

        # 清理信号数据
        if hasattr(self, "clear_signal_curve_visibility"):
            self.clear_signal_curve_visibility()
        self.signals.clear()
        self.can_parsed_data.clear()
        self.bus_signal_map.clear()
        self.custom_math_data.clear()

        # 清除总线配置
        if hasattr(self, 'bus_config_widgets'):
            for bus_id, widget in self.bus_config_widgets.items():
                try:
                    widget.setParent(None)
                    widget.deleteLater()
                except Exception:
                    pass
            self.bus_config_widgets.clear()

        # 清理地图资源
        if hasattr(self, 'map_dialog') and self.map_dialog:
            try:
                self.map_dialog.close()
                self.map_dialog.deleteLater()
            except Exception:
                pass
            self.map_dialog = None
            self.web_view = None

        # 清理定时器
        if hasattr(self, 'rescale_timer') and self.rescale_timer:
            self.rescale_timer.stop()
            self.rescale_timer.deleteLater()
            self.rescale_timer = None

        print("✅ 资源清理完成")
        event.accept()

    @staticmethod
    def _get_filtered_dbc_path(original_path):
        """获取筛选后的DBC文件路径"""
        base_name = os.path.splitext(os.path.basename(original_path))[0]
        save_dir = os.path.join(os.path.expanduser("~"), ".vet_data", "filtered_dbc")
        save_name = f"{base_name}_filtered.dbc"
        save_path = os.path.join(save_dir, save_name)
        if os.path.exists(save_path):
            return save_path
        return None


if __name__ == '__main__':
    multiprocessing.freeze_support()
    pg.setConfigOptions(antialias=True)
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--enable-local-file-accesses --disable-web-security"

    app = QApplication(sys.argv)
    window = MDFPlotter()
    window.show()
    sys.exit(app.exec())
