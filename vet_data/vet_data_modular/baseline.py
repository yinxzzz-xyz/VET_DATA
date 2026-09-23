#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
VET_DATA_v26.08.25_CAN_MultiBus_UI - 支持容器帧解析
完整移植 main_py.py 的容器帧解析逻辑
"""
import subprocess
import sys
import os
import threading

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
                             QTextEdit)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread
import numpy as np
from PyQt6.QtWebEngineCore import QWebEngineSettings
from collections import defaultdict
import warnings
import logging
from io import StringIO
import re
from PyQt6.QtGui import QColor
import time
import multiprocessing

from .signal_resolver import SignalResolutionError, SignalResolver

# 抑制警告
warnings.filterwarnings("ignore")
logging.getLogger("cantools").setLevel(logging.ERROR)

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

        layout = QVBoxLayout(self)
        layout.setSpacing(3)

        # 统计信息 - 显示存在的ID数量
        total_msgs = len(self.db.messages)
        total_sigs = self._count_total_signals()
        existing_count = len(self.existing_ids)

        info_text = f"数据库包含 {total_msgs} 个消息, 共 {total_sigs} 个信号"
        if existing_count > 0:
            info_text += f" | 🟢 日志中存在 {existing_count} 个消息ID"
        info_label = QLabel(info_text)
        info_label.setStyleSheet("color: #2c3e50; font-weight: bold; padding: 3px; font-size: 11px;")
        layout.addWidget(info_label)

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
        self.select_all_btn.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold; padding: 4px;")
        self.deselect_all_btn.setText("❌ 全部取消")
        self.deselect_all_btn.setStyleSheet("background-color: #e74c3c; color: white; font-weight: bold; padding: 4px;")
        self.select_all_btn.clicked.connect(self.select_all)
        self.deselect_all_btn.clicked.connect(self.deselect_all)

        self.selection_count_label.setText("已选: 0 / 0")
        self.selection_count_label.setStyleSheet("color: #27ae60; font-weight: bold; font-size: 11px;")

        btn_layout.addWidget(self.select_all_btn)
        btn_layout.addWidget(self.deselect_all_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self.selection_count_label)
        layout.addLayout(btn_layout)

        # 信号列表 - 使用QTreeWidget
        self.tree.setHeaderLabels(["信号名", "ID", "帧名", "端序", "位数", "注释"])
        self.tree.setIndentation(15)
        self.tree.setAlternatingRowColors(True)
        self.tree.setColumnWidth(0, 200)
        self.tree.setColumnWidth(1, 80)
        self.tree.setColumnWidth(2, 180)
        self.tree.setColumnWidth(3, 70)
        self.tree.setColumnWidth(4, 50)
        self.tree.setColumnWidth(5, 150)

        # 修改样式表：移除所有 color 设置，让 setForeground 生效
        self.tree.setStyleSheet("""
            QTreeWidget {
                border: 1px solid #ddd;
                font-size: 11px;
                alternate-background-color: #f8f9fa;
                show-decoration-selected: 0;
            }
            QTreeWidget::item {
                padding: 1px 2px;
                height: 18px;
                border: none;
                background-color: transparent;
            }
            QTreeWidget::item:selected {
                background-color: rgba(52, 152, 219, 0.15);
            }
            QTreeWidget::item:selected:active {
                background-color: rgba(52, 152, 219, 0.15);
            }
            QTreeWidget::item:hover {
                background-color: transparent;
            }
            QTreeWidget::item:selected:hover {
                background-color: rgba(52, 152, 219, 0.15);
            }
            QTreeWidget::item:!selected:hover {
                background-color: transparent;
            }
            QTreeWidget::item:selected:!active {
                background-color: rgba(52, 152, 219, 0.10);
            }
            QTreeWidget::indicator {
                width: 14px;
                height: 14px;
                margin-right: 4px;
            }
            QTreeWidget::indicator:checked {
                background-color: #4CAF50;
                border: 1px solid #388E3C;
                border-radius: 2px;
            }
            QTreeWidget::indicator:unchecked {
                background-color: white;
                border: 1px solid #bbb;
                border-radius: 2px;
            }
            QTreeWidget::indicator:indeterminate {
                background-color: #FFD700;
                border: 1px solid #DAA520;
                border-radius: 2px;
            }
            QTreeWidget::indicator:hover {
                border-color: #4CAF50;
            }
            QHeaderView::section {
                background-color: #e8ecf1;
                padding: 2px 5px;
                font-weight: bold;
                font-size: 10px;
                border: none;
                border-right: 1px solid #ddd;
            }
            QHeaderView::section:last {
                border-right: none;
            }
        """)

        self.tree.itemClicked.connect(self.on_item_clicked)
        self.tree.itemChanged.connect(self.on_item_changed)
        layout.addWidget(self.tree)

        # 底部按钮
        btn_layout2 = QHBoxLayout()
        self.select_btn.setText("✅ 使用选中的信号")
        self.select_btn.setStyleSheet(
            "background-color: #27ae60; color: white; font-weight: bold; font-size: 12px; padding: 8px;")
        self.cancel_btn.setText("取消")
        self.cancel_btn.setStyleSheet("font-size: 12px; padding: 8px;")
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
        """填充树形列表"""
        self.tree.clear()
        self.signal_items = []
        self.item_keys = []
        self.frame_signal_count = {}
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

            has_matching = False
            matching_signals = []

            for sig in msg.signals:
                sig_name = sig.name
                sig_comment = getattr(sig, 'comment', '') or ''
                search_text = f"{sig_name} {frame_id_hex} {frame_name} {sig_comment}".lower()

                should_show = True
                if filter_text:
                    should_show = filter_text in search_text

                if should_show:
                    has_matching = True
                    matching_signals.append((sig, should_show))
                else:
                    matching_signals.append((sig, False))

            if not has_matching and filter_text:
                continue

            # 记录该帧的信号总数
            self.frame_signal_count[frame_id] = len(msg.signals)

            frame_item = QTreeWidgetItem(self.tree)
            frame_item.setFlags(frame_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)

            # 设置帧名（第0列）
            frame_item.setText(0, f"📦 {frame_name}")

            # 如果该ID在数据中存在，帧名用绿色 - 使用 setForeground 确保生效
            if frame_exists:
                frame_item.setForeground(0, QColor(0, 128, 0))  # darkGreen
                frame_item.setToolTip(0, f"该帧ID (0x{frame_id:X}) 在日志中存在 ✓")
            else:
                frame_item.setForeground(0, QColor(0, 0, 139))  # darkBlue
                frame_item.setToolTip(0, f"该帧ID (0x{frame_id:X}) 在日志中不存在")

            frame_item.setText(1, frame_id_hex)

            # 检查该帧下所有信号的选中状态
            frame_checked_count = 0
            for sig, _ in matching_signals:
                key = f"{frame_id}_{sig.name}"
                if self._selected_state.get(key, False):
                    frame_checked_count += 1

            total_sigs = len(msg.signals)
            # "已选 X 个" 用绿色显示
            if frame_checked_count > 0:
                frame_item.setText(2, f"({total_sigs} 个信号，已选 {frame_checked_count} 个)")
                frame_item.setForeground(2, QColor(0, 128, 0))  # darkGreen
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

            for sig, show in matching_signals:
                if not show and filter_text:
                    continue

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

                self.signal_items.append(sig_item)
                self.item_keys.append(key)

                if self._selected_state.get(key, False):
                    sig_item.setCheckState(0, Qt.CheckState.Checked)
                else:
                    sig_item.setCheckState(0, Qt.CheckState.Unchecked)

        self.update_selection_count()

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
                idx = self.signal_items.index(item)
                key = self.item_keys[idx]
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
                frame_id = None
                for fid, fitem in self.frame_items.items():
                    if fitem is item:
                        frame_id = fid
                        break

                if frame_id is not None:
                    # 更新该帧下所有信号的选中状态
                    for sig_item, key in zip(self.signal_items, self.item_keys):
                        if key.startswith(f"{frame_id}_"):
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
                idx = self.signal_items.index(item)
                key = self.item_keys[idx]
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

        frame_id = None
        for fid, fitem in self.frame_items.items():
            if fitem is frame_item:
                frame_id = fid
                break

        if frame_id is None:
            return

        total_count = self.frame_signal_count.get(frame_id, 0)

        checked_count = 0
        for key, is_selected in self._selected_state.items():
            if key.startswith(f"{frame_id}_") and is_selected:
                checked_count += 1

        # 更新显示文本，"已选 X 个"用绿色
        if checked_count > 0:
            frame_item.setText(2, f"({total_count} 个信号，已选 {checked_count} 个)")
            frame_item.setForeground(2, Qt.GlobalColor.darkGreen)
        else:
            frame_item.setText(2, f"({total_count} 个信号)")
            # 恢复默认颜色
            frame_item.setForeground(2, Qt.GlobalColor.black)

        # 更新勾选状态（如果需要）
        if update_checkbox:
            if checked_count == total_count and total_count > 0:
                frame_item.setCheckState(0, Qt.CheckState.Checked)
            elif checked_count > 0:
                frame_item.setCheckState(0, Qt.CheckState.PartiallyChecked)
            else:
                frame_item.setCheckState(0, Qt.CheckState.Unchecked)

    def filter_signals(self, text):
        self._populate_tree(text)

    def select_all(self):
        """全选当前筛选显示的所有信号"""
        for key in self._selected_state:
            self._selected_state[key] = True
        self._populate_tree(self.search_box.text())

    def deselect_all(self):
        """取消全选所有信号"""
        for key in self._selected_state:
            self._selected_state[key] = False
        self._populate_tree(self.search_box.text())

    def update_selection_count(self):
        total = len(self._selected_state)
        selected = sum(1 for v in self._selected_state.values() if v)
        self.selection_count_label.setText(f"已选: {selected} / {total}")
        if selected == 0:
            self.selection_count_label.setStyleSheet("color: #e74c3c; font-weight: bold;")
        else:
            self.selection_count_label.setStyleSheet("color: #27ae60; font-weight: bold;")

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
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # 标题
        title_label = QLabel("🔧 ARXML 转 DBC 工具")
        title_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #2c3e50; padding: 5px;")
        layout.addWidget(title_label)

        # 说明
        desc_label = QLabel("DBC 文件将自动保存到 ARXML 文件所在目录的 dbc_output 文件夹中")
        desc_label.setStyleSheet("color: #7f8c8d; font-size: 11px; padding: 0 5px 10px 5px;")
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)

        # ===== ARXML 文件选择 =====
        arxml_group = QGroupBox("选择 ARXML 文件")
        arxml_group.setStyleSheet("""
            QGroupBox { font-weight: bold; font-size: 12px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px 0 5px; }
        """)
        arxml_layout = QVBoxLayout()

        arxml_file_layout = QHBoxLayout()
        self.arxml_path_edit.setPlaceholderText("请选择 ARXML 文件...")
        self.arxml_path_edit.setReadOnly(True)
        self.arxml_select_btn.setText("浏览")
        self.arxml_select_btn.setStyleSheet("background-color: #3498db; color: white; padding: 5px 15px;")
        self.arxml_select_btn.clicked.connect(self.select_arxml)

        arxml_file_layout.addWidget(self.arxml_path_edit, 3)
        arxml_file_layout.addWidget(self.arxml_select_btn, 1)
        arxml_layout.addLayout(arxml_file_layout)

        arxml_group.setLayout(arxml_layout)
        layout.addWidget(arxml_group)

        # ===== DBC 输出信息（只读显示） =====
        dbc_group = QGroupBox("DBC 输出路径（自动生成）")
        dbc_group.setStyleSheet("""
            QGroupBox { font-weight: bold; font-size: 12px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px 0 5px; }
        """)
        dbc_layout = QVBoxLayout()

        self.dbc_path_edit.setPlaceholderText("选择 ARXML 文件后自动生成...")
        self.dbc_path_edit.setReadOnly(True)
        self.dbc_path_edit.setStyleSheet("background-color: #f0f0f0;")
        dbc_layout.addWidget(self.dbc_path_edit)

        dbc_group.setLayout(dbc_layout)
        layout.addWidget(dbc_group)

        # 转换按钮
        self.convert_btn.setText("开始转换")
        self.convert_btn.setStyleSheet("""
            QPushButton {
                background-color: #27ae60;
                color: white;
                font-weight: bold;
                font-size: 13px;
                padding: 8px 25px;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #2ecc71; }
            QPushButton:disabled { background-color: #95a5a6; }
        """)
        self.convert_btn.clicked.connect(self.start_conversion)
        self.convert_btn.setEnabled(False)
        layout.addWidget(self.convert_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        # 状态栏
        status_layout = QHBoxLayout()
        status_layout.addWidget(QLabel("状态:"))
        self.status_label.setText("就绪")
        self.status_label.setStyleSheet("font-weight: bold;")
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        layout.addLayout(status_layout)

        # 进度条 - 无限模式（与附件一致）
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        # 日志
        log_group = QGroupBox("转换日志")
        log_group.setStyleSheet("""
            QGroupBox { font-weight: bold; font-size: 12px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px 0 5px; }
        """)
        log_layout = QVBoxLayout()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("""
            QTextEdit {
                font-family: Consolas, monospace;
                font-size: 10px;
                background-color: #2c3e50;
                color: #ecf0f1;
                border: 1px solid #34495e;
                border-radius: 3px;
            }
        """)
        self.log_text.setMinimumHeight(120)
        log_layout.addWidget(self.log_text)
        log_group.setLayout(log_layout)
        layout.addWidget(log_group)

        # 底部按钮
        btn_layout = QHBoxLayout()
        self.clear_log_btn.setText("清空日志")
        self.clear_log_btn.clicked.connect(self.clear_log)
        btn_layout.addWidget(self.clear_log_btn)
        btn_layout.addStretch()
        self.open_folder_btn.setText("📁 打开输出文件夹")
        self.open_folder_btn.setEnabled(False)
        self.open_folder_btn.clicked.connect(self.open_output_folder)
        btn_layout.addWidget(self.open_folder_btn)
        self.close_btn.setText("关闭")
        self.close_btn.clicked.connect(self.close)
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)

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
        QApplication.processEvents()

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
        self.status_label.setText("转换中...")
        self.status_label.setStyleSheet("color: #f39c12; font-weight: bold;")
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

        # 在新线程中执行转换（与附件完全一致）
        threading.Thread(target=self._run_conversion, daemon=True).start()

    def _run_conversion(self):
        """
        后台执行 ARXML -> DBC
        使用 canmatrix Python API 直接转换，避免子进程问题
        """
        try:
            arxml_file = os.path.abspath(self.arxml_path)
            dbc_file = os.path.abspath(self.dbc_path)

            self.log("=" * 50)
            self.log("🔧 ARXML → DBC 转换开始")
            self.log(f"输入文件: {arxml_file}")
            self.log(f"输出文件: {dbc_file}")

            # 确保输出目录存在
            output_dir = os.path.dirname(dbc_file)
            if not os.path.exists(output_dir):
                os.makedirs(output_dir, exist_ok=True)

            # 删除可能存在的旧文件
            if os.path.exists(dbc_file):
                try:
                    os.remove(dbc_file)
                    self.log("🗑️ 已删除旧的 DBC 文件")
                except Exception as e:
                    self.log(f"⚠️ 删除旧文件失败: {e}")

            # 导入 canmatrix（显式导入所有需要的子模块）
            try:
                import canmatrix
                import canmatrix.convert
                import canmatrix.formats
                import canmatrix.formats.arxml  # 关键：确保 ARXML 格式被加载
                self.log(f"✅ canmatrix 版本: {getattr(canmatrix, '__version__', 'unknown')}")
            except ImportError as e:
                self.log(f"❌ 导入 canmatrix 失败: {e}")
                raise RuntimeError(f"请确保已安装 canmatrix: pip install canmatrix")

            # 检查 ARXML 文件是否存在且可读
            if not os.path.exists(arxml_file):
                raise FileNotFoundError(f"ARXML 文件不存在: {arxml_file}")

            if os.path.getsize(arxml_file) == 0:
                raise ValueError(f"ARXML 文件为空: {arxml_file}")

            # 转换进度日志
            self.log("🔄 正在转换 (使用 canmatrix.convert.convert)...")
            QApplication.processEvents()

            # ===== 执行转换 =====
            # 参数与 canmatrix-convert 命令行保持一致：
            #   canmatrix-convert input.arxml output.dbc
            #   --dbcExportEncoding ascii
            #   --ignoreEncodingErrors
            try:
                canmatrix.convert.convert(
                    arxml_file,
                    dbc_file,
                    dbcExportEncoding="ascii",
                    ignoreEncodingErrors="ignore"
                )
                self.log("✅ canmatrix.convert.convert() 调用完成")
            except Exception as e:
                self.log(f"⚠️ 转换调用失败: {e}")
                # 尝试更基础的调用方式
                try:
                    # 加载 ARXML
                    self.log("🔄 尝试分步加载...")
                    db = canmatrix.formats.loadp(arxml_file)
                    if db:
                        self.log(f"✅ 加载成功: {len(db)} 个 CAN 总线")
                        # 保存为 DBC
                        canmatrix.formats.dump(dbc_file, db, dbcExportEncoding="ascii")
                        self.log("✅ 分步保存成功")
                    else:
                        raise RuntimeError("加载 ARXML 返回空数据库")
                except Exception as e2:
                    self.log(f"❌ 分步加载也失败: {e2}")
                    raise RuntimeError(f"转换失败: {e2}")

            # ===== 检查输出 =====
            def check_output(filepath):
                """检查 DBC 文件是否有效"""
                if not os.path.exists(filepath):
                    return False, "文件不存在"
                if os.path.getsize(filepath) == 0:
                    return False, "文件为空"
                # 检查 DBC 文件基本格式
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if 'BO_' not in content:
                            return False, "文件中没有找到 BO_ (消息定义)"
                    return True, ""
                except Exception as e:
                    return False, f"读取失败: {e}"

            success, msg = check_output(dbc_file)
            if success:
                file_size = os.path.getsize(dbc_file)
                self.log(f"✅ 转换成功！文件大小: {file_size} 字节")
                # 统计消息数量
                try:
                    with open(dbc_file, 'r', encoding='utf-8') as f:
                        msg_count = f.read().count('BO_')
                    self.log(f"📊 包含 {msg_count} 个消息定义")
                except:
                    pass
                self.conversion_success.emit()
            else:
                # 检查是否在输出目录中生成了其他 DBC 文件
                output_dir = os.path.dirname(dbc_file)
                candidates = []
                try:
                    for fname in os.listdir(output_dir):
                        if fname.lower().endswith('.dbc'):
                            full_path = os.path.join(output_dir, fname)
                            if os.path.getsize(full_path) > 0:
                                candidates.append(fname)
                except Exception:
                    pass

                if candidates:
                    self.log(f"✅ 在输出目录找到 DBC 文件: {', '.join(candidates)}")
                    self.conversion_success.emit()
                else:
                    raise RuntimeError(f"转换完成但未生成有效的 DBC 文件: {msg}")

        except Exception as e:
            import traceback
            error_msg = traceback.format_exc()
            self.log("❌ ARXML → DBC 转换异常:")
            self.log(error_msg)
            self.conversion_error.emit(str(e))

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
        self.progress_bar.setVisible(False)
        self.convert_btn.setEnabled(True)
        self.open_folder_btn.setEnabled(True)
        self.update_convert_button()
        self.status_label.setText("转换成功！")
        self.status_label.setStyleSheet("color: #27ae60; font-weight: bold;")

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
        self.progress_bar.setVisible(False)
        self.convert_btn.setEnabled(True)
        self.update_convert_button()
        self.status_label.setText("转换失败")
        self.status_label.setStyleSheet("color: #e74c3c; font-weight: bold;")
        self.log(f"❌ 转换失败: {error_msg}")
        self.log("=" * 50)
        QMessageBox.critical(self, "错误", f"转换失败：\n{error_msg}")

    def closeEvent(self, event):
        """关闭窗口时清理资源"""
        print("🔄 关闭 ARXML 转换工具...")

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
        self.protocol_label = QLabel()
        self.select_protocol_btn = QPushButton()
        self.protocol_clear_btn = QPushButton()
        self.parse_btn = QPushButton()
        self.status_indicator = QLabel()
        self.init_ui()

    def init_ui(self):
        layout = QHBoxLayout()
        layout.setContentsMargins(5, 4, 5, 4)
        layout.setSpacing(5)

        self.id_label.setText(f"Bus {self.bus_id}")
        self.id_label.setMinimumWidth(50)
        self.id_label.setStyleSheet("font-weight: bold; color: #2c3e50; font-size: 11px;")
        layout.addWidget(self.id_label)

        self.name_edit.setPlaceholderText(f"Bus {self.bus_id}")
        self.name_edit.setMinimumWidth(80)
        self.name_edit.textChanged.connect(self.on_name_changed)
        layout.addWidget(self.name_edit)

        self.type_label.setText("CAN2.0")
        self.type_label.setMinimumWidth(55)
        self.type_label.setStyleSheet("color: #2980b9; font-size: 10px; font-weight: bold;")
        layout.addWidget(self.type_label)

        self.id_count_label.setText("0 IDs")
        self.id_count_label.setMinimumWidth(50)
        self.id_count_label.setStyleSheet("color: #7f8c8d; font-size: 10px;")
        layout.addWidget(self.id_count_label)

        self.protocol_label.setText("未配置")
        self.protocol_label.setMinimumWidth(120)
        self.protocol_label.setStyleSheet("color: #e74c3c; font-size: 9px;")
        self.protocol_label.setWordWrap(True)
        layout.addWidget(self.protocol_label)

        self.select_protocol_btn.setText("选择协议")
        self.select_protocol_btn.setFixedWidth(65)
        self.select_protocol_btn.setStyleSheet(
            "QPushButton { background-color: #3498db; color: white; font-size: 9px; }")
        self.select_protocol_btn.clicked.connect(self.select_protocol)
        layout.addWidget(self.select_protocol_btn)

        self.protocol_clear_btn.setText("✕")
        self.protocol_clear_btn.setFixedWidth(25)
        self.protocol_clear_btn.setToolTip("清除协议")
        self.protocol_clear_btn.setStyleSheet("""
                    QPushButton { 
                        background-color: #e74c3c; 
                        color: white; 
                        font-size: 10px; 
                        font-weight: bold;
                        border-radius: 3px;
                    }
                    QPushButton:hover { background-color: #c0392b; }
                """)
        self.protocol_clear_btn.clicked.connect(self.clear_protocol)
        self.protocol_clear_btn.setVisible(False)
        layout.addWidget(self.protocol_clear_btn)

        self.parse_btn.setText("解析")
        self.parse_btn.setFixedWidth(45)
        self.parse_btn.setStyleSheet("QPushButton { background-color: #27ae60; color: white; font-size: 9px; }")
        self.parse_btn.clicked.connect(lambda: self.parse_requested.emit(self.bus_id))
        layout.addWidget(self.parse_btn)

        self.status_indicator.setText("⚪")
        self.status_indicator.setFixedWidth(18)
        layout.addWidget(self.status_indicator)

        self.setLayout(layout)

    def on_name_changed(self, text):
        self.bus_id_changed.emit(self.bus_id, text.strip() if text.strip() else f"Bus {self.bus_id}")

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
                self.protocol_label.setStyleSheet("color: #27ae60; font-size: 9px;")
                if self.protocol_clear_btn:
                    self.protocol_clear_btn.setVisible(True)
                self.protocol_changed.emit(self.bus_id, self.protocol_path)
                self._cached_filtered_db = filtered_db
            else:
                self.protocol_path = file_path
                self.protocol_label.setText(os.path.basename(file_path))
                self.protocol_label.setStyleSheet("color: #27ae60; font-size: 9px;")
                if self.protocol_clear_btn:
                    self.protocol_clear_btn.setVisible(True)
                self.protocol_changed.emit(self.bus_id, file_path)
                self._cached_filtered_db = None

        except ImportError as e:
            QMessageBox.warning(self, "依赖缺失", f"请安装 cantools 库: pip install cantools\n\n错误: {e}")
        except Exception as e:
            QMessageBox.warning(self, "DBC加载失败", f"加载DBC时出错: {e}")
            import traceback
            traceback.print_exc()

    def clear_protocol(self):
        self.protocol_path = ""
        self.arxml_sub_bus = ""
        self.protocol_label.setText("未配置")
        self.protocol_label.setStyleSheet("color: #e74c3c; font-size: 9px;")
        if self.protocol_clear_btn:
            self.protocol_clear_btn.setVisible(False)
        self.protocol_removed.emit(self.bus_id)
        self.set_status('idle')

    def set_id_count(self, count, parsed_count=None):
        if parsed_count is not None and parsed_count > 0:
            self.id_count_label.setText(f"{parsed_count}/{count} IDs")
            self.id_count_label.setStyleSheet("color: #27ae60; font-size: 10px; font-weight: bold;")
        elif count > 0:
            self.id_count_label.setText(f"{count} IDs")
            self.id_count_label.setStyleSheet("color: #2c3e50; font-size: 10px; font-weight: bold;")
        else:
            self.id_count_label.setText("0 IDs")
            self.id_count_label.setStyleSheet("color: #95a5a6; font-size: 10px;")

    def set_bus_type(self, is_fd):
        if is_fd:
            self.type_label.setText("CANFD")
            self.type_label.setStyleSheet("color: #e67e22; font-size: 10px; font-weight: bold;")
        else:
            self.type_label.setText("CAN2.0")
            self.type_label.setStyleSheet("color: #2980b9; font-size: 10px; font-weight: bold;")

    def set_status(self, status, extra_info=""):
        if status == 'idle':
            self.status_indicator.setText("⚪")
            self.status_indicator.setStyleSheet("color: #95a5a6;")
            self.status_indicator.setToolTip("就绪")
        elif status == 'loading':
            self.status_indicator.setText("🔄")
            self.status_indicator.setStyleSheet("color: #f39c12;")
            self.status_indicator.setToolTip("解析中...")
        elif status == 'success':
            self.status_indicator.setText("🟢")
            self.status_indicator.setStyleSheet("color: #27ae60;")
            self.status_indicator.setToolTip(f"解析完成{extra_info}")
        elif status == 'error':
            self.status_indicator.setText("🔴")
            self.status_indicator.setStyleSheet("color: #e74c3c;")
            self.status_indicator.setToolTip("解析失败")

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

            # ===== 自动检测并保留 Header 信号 =====
            header_signals_by_frame = {}
            for msg_def in db.messages:
                frame_id = msg_def.frame_id
                for sig in msg_def.signals:
                    name_lower = sig.name.lower()
                    if ('header_id' in name_lower or 'pdu_id' in name_lower or
                            'header_dlc' in name_lower or 'pdu_len' in name_lower):
                        if frame_id not in header_signals_by_frame:
                            header_signals_by_frame[frame_id] = []
                        header_signals_by_frame[frame_id].append(sig.name)

            from cantools.database.can import Database, Message

            new_db = Database()
            total_original = 0
            total_filtered = 0
            auto_added = 0

            for msg_def in db.messages:
                frame_id = msg_def.frame_id
                total_original += len(msg_def.signals)

                header_names = header_signals_by_frame.get(frame_id, [])

                selected_names = set()
                if frame_id in selected_signals:
                    selected_names = set(selected_signals[frame_id])

                if not selected_names:
                    continue

                for hdr_name in header_names:
                    if hdr_name not in selected_names:
                        selected_names.add(hdr_name)
                        auto_added += 1
                        print(f"  🔄 自动保留Header信号: {hdr_name} (帧 0x{frame_id:X})")

                new_msg = Message(
                    frame_id=msg_def.frame_id,
                    name=msg_def.name,
                    length=msg_def.length,
                    senders=msg_def.senders if hasattr(msg_def, 'senders') else [],
                    signals=[],
                    comment=msg_def.comment if hasattr(msg_def, 'comment') else None
                )

                for sig in msg_def.signals:
                    if sig.name in selected_names:
                        new_msg.signals.append(sig)
                        total_filtered += 1

                if new_msg.signals:
                    new_db.messages.append(new_msg)

            print(f"✅ 信号筛选完成:")
            print(f"   原始: {len(db.messages)} 个消息, {total_original} 个信号")
            print(f"   保留: {len(new_db.messages)} 个消息, {total_filtered} 个信号")
            print(f"   自动添加Header信号: {auto_added} 个")

            self._save_filtered_dbc_with_custom_name(new_db, self.protocol_path)

            return new_db
        return db

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
                self.protocol_path = original_path + " [已筛选-内存]"
                self.protocol_label.setText(os.path.basename(original_path) + " [已筛选-内存]")
                self.protocol_label.setStyleSheet("color: #27ae60; font-size: 9px;")
                self._filtered_dbc_path = None
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
            self.protocol_label.setText(os.path.basename(save_path) + " [已筛选]")
            self.protocol_label.setStyleSheet("color: #27ae60; font-size: 9px;")
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
            self.protocol_path = original_path + " [已筛选-内存]"
            self.protocol_label.setText(os.path.basename(original_path) + " [已筛选-内存]")
            self.protocol_label.setStyleSheet("color: #27ae60; font-size: 9px;")
            self._filtered_dbc_path = None

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

    def __init__(self, bus_id, parent=None):
        super().__init__(parent)
        self.bus_id = bus_id
        self._instance_id = id(self)
        self._cancelled = False
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
        self.cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #e74c3c;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 6px 12px;
            }
            QPushButton:hover {
                background-color: #c0392b;
            }
            QPushButton:disabled {
                background-color: #95a5a6;
            }
        """)
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

    def on_cancel(self):
        self._cancelled = True
        self.status_label.setText("⏹ 正在取消...")
        self.cancel_btn.setEnabled(False)

    def is_cancelled(self):
        return self._cancelled

    def showEvent(self, event):
        super().showEvent(event)

    def hideEvent(self, event):
        super().hideEvent(event)

    def closeEvent(self, event):
        self._cancelled = True
        event.accept()


class ParseWorker(QThread):
    """后台解析线程 - 从 main_py.py 完整移植容器帧解析逻辑"""
    progress_updated = pyqtSignal(int, int, str)
    parse_finished = pyqtSignal(int, dict)
    parse_error = pyqtSignal(int, str)

    def __init__(self, bus_id, log_path, db, arxml_sub_bus="", channel_offset=0, parent=None):
        super().__init__(parent)
        self.bus_id = bus_id
        self.log_path = log_path
        self.db = db
        self.arxml_sub_bus = arxml_sub_bus
        self.channel_offset = channel_offset
        self._is_running = True

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
                header_total_bits = header_id_sig.length + header_dlc_sig.length

                self.container_frames_info[frame_id] = {
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
        for frame_id, frame_info in self.container_frames_info.items():
            multiplexer_signals = {}

            for msg_def in db.messages:
                if msg_def.frame_id != frame_id:
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

    def run(self):
        try:
            print(f"\n{'=' * 70}")
            print(f"🚀 [Bus {self.bus_id}] 开始解析")
            print(f"   日志文件: {self.log_path}")
            print(f"   数据库消息数: {len(self.db.messages)}")
            print(f"   是否多路复用: {self.is_multiplexer}")
            if self.is_multiplexer:
                print(f"   检测到 {len(self.container_frames_info)} 个容器帧:")
                for frame_id, info in self.container_frames_info.items():
                    print(
                        f"     0x{frame_id:X} ({info['frame_name']}) - Header总位数: {info['header_total_bits']} bits")
                    print(f"     分组数: {len(info['multiplexer_signals'])}")
            print(f"{'=' * 70}")

            # 打开日志文件
            if self.log_path.lower().endswith('.blf'):
                reader = can.BLFReader(self.log_path)
            else:
                reader = can.LogReader(self.log_path)

            # ===== 构建消息映射 =====
            msg_shard = {}
            msg_signal_map = {}

            for msg_def in self.db.messages:
                frame_id = msg_def.frame_id
                msg_shard[frame_id] = msg_def
                print(f"  📨 注册消息: ID=0x{frame_id:X} ({frame_id}), 信号数={len(msg_def.signals)}")

                sig_map = {}
                for sig in msg_def.signals:
                    sig_map[sig.name] = sig
                msg_signal_map[frame_id] = sig_map

            print(f"\n总线 {self.bus_id}: 数据库中有 {len(msg_shard)} 个消息定义")
            print(f"  消息ID列表: {[hex(i) for i in sorted(msg_shard.keys())]}")

            # ===== 统计总消息数 =====
            total_msgs = 0
            try:
                temp_reader = can.BLFReader(self.log_path) if self.log_path.lower().endswith('.blf') else can.LogReader(
                    self.log_path)
                for msg in temp_reader:
                    raw_channel = getattr(msg, 'channel', 0)
                    if raw_channel == 0:
                        bus_id = 1
                    else:
                        bus_id = raw_channel + 1
                    if bus_id == self.bus_id:
                        total_msgs += 1
                temp_reader.stop()
                print(f"总线 {self.bus_id}: 预估总消息数: {total_msgs}")
            except Exception as e:
                print(f"统计消息总数失败: {e}")
                total_msgs = 0

            t0 = None
            pool = defaultdict(lambda: {'t': [], 'v': [], 'unit': '', 'comment': ''})

            total_messages_processed = 0
            total_signals_decoded = 0
            msg_ids_with_signals = set()
            matched_msg_ids = set()
            unmatched_msg_ids = set()
            container_frame_counts = defaultdict(int)  # 每个容器帧的计数

            if total_msgs > 0:
                progress_interval = max(1, total_msgs // 200)
            else:
                progress_interval = 1000

            last_progress_update = 0
            last_progress_percent = 0

            # ===== 遍历所有消息 =====
            print(f"\n📖 开始遍历消息...")

            for msg in reader:
                if not self._is_running:
                    print("⏹ 解析被用户停止")
                    break

                if t0 is None:
                    t0 = msg.timestamp

                raw_channel = getattr(msg, 'channel', 0)
                if raw_channel == 0:
                    bus_id = 1
                else:
                    bus_id = raw_channel + 1

                if bus_id != self.bus_id:
                    continue

                rel_t = msg.timestamp - t0
                msg_id = msg.arbitration_id

                # ===== 专门打印 0x13E 帧 =====
                if msg_id == 0x13E:
                    print(f"\n{'=' * 60}")
                    print(f"🔍 [0x13E 帧] 时间: {rel_t:.6f}s, 数据长度: {len(msg.data)}, 数据: {msg.data.hex()}")
                    print(f"   raw_channel: {raw_channel}, bus_id: {bus_id}")

                msg_def = msg_shard.get(msg_id)
                if msg_def is None:
                    unmatched_msg_ids.add(msg_id)
                    continue

                matched_msg_ids.add(msg_id)
                total_messages_processed += 1

                # ===== 检测是否为容器帧 =====
                if self.is_multiplexer and msg_id in self.container_frames_info:
                    container_frame_counts[msg_id] += 1
                    frame_info = self.container_frames_info[msg_id]
                    header_total_bits = frame_info['header_total_bits']
                    multiplexer_signals = frame_info['multiplexer_signals']
                    header_bytes = (header_total_bits + 7) // 8
                    frame_name = frame_info['frame_name']

                    data = msg.data
                    i = 0
                    data_len = len(data)
                    current_count = container_frame_counts[msg_id]

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
                                            pack['t'].append(rel_t)
                                            pack['v'].append(physical_value)
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
                                            pack['t'].append(rel_t)
                                            pack['v'].append(physical_value)
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

                                total_signals_decoded += len(signals)
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
                        decoded = msg_def.decode(msg.data, decode_choices=False)
                        for sig_name, sig_val in decoded.items():
                            if not isinstance(sig_val, (int, float)):
                                continue
                            pack = pool[sig_name]
                            pack['t'].append(rel_t)
                            pack['v'].append(float(sig_val))
                            if not pack['unit']:
                                sig_obj = msg_def.get_signal_by_name(sig_name)
                                if sig_obj:
                                    pack['unit'] = getattr(sig_obj, 'unit', '')
                                    pack['comment'] = f"ID: 0x{msg_id:X}"
                        total_signals_decoded += len(decoded)
                        msg_ids_with_signals.add(msg_id)
                    except Exception:
                        sig_map = msg_signal_map.get(msg_id, {})
                        for sig_name, sig_obj in sig_map.items():
                            try:
                                sig_val = sig_obj.decode(msg.data)
                                if isinstance(sig_val, (int, float)):
                                    pack = pool[sig_name]
                                    pack['t'].append(rel_t)
                                    pack['v'].append(float(sig_val))
                                    if not pack['unit']:
                                        pack['unit'] = getattr(sig_obj, 'unit', '')
                                        pack['comment'] = f"ID: 0x{msg_id:X}"
                                    total_signals_decoded += 1
                                    msg_ids_with_signals.add(msg_id)
                            except Exception:
                                pass

                # ===== 进度更新 =====
                if total_msgs > 0:
                    progress = int(total_messages_processed / total_msgs * 100)
                    if progress > last_progress_percent:
                        progress_to_show = min(99, progress)
                        if progress_to_show > last_progress_percent:
                            self.progress_updated.emit(
                                self.bus_id,
                                progress_to_show,
                                f"处理中... {total_messages_processed:,} / {total_msgs:,} 条消息"
                            )
                            last_progress_percent = progress_to_show
                    elif total_messages_processed - last_progress_update >= progress_interval:
                        self.progress_updated.emit(
                            self.bus_id,
                            min(99, last_progress_percent),
                            f"处理中... {total_messages_processed:,} / {total_msgs:,} 条消息"
                        )
                        last_progress_update = total_messages_processed

            try:
                reader.stop()
            except Exception:
                pass

            # ===== 打印统计 =====
            print(f"\n{'=' * 70}")
            print(f"📊 总线 {self.bus_id} 解析统计:")
            print(f"  - 匹配到的消息ID数: {len(matched_msg_ids)}")
            print(f"  - 未匹配的消息ID数: {len(unmatched_msg_ids)}")
            if unmatched_msg_ids:
                print(f"  - 未匹配ID示例: {[hex(i) for i in list(unmatched_msg_ids)[:20]]}")
            print(f"  - 处理的报文总数: {total_messages_processed}")
            print(f"  - 容器帧数量: {sum(container_frame_counts.values())}")
            if container_frame_counts:
                print(f"  - 各容器帧计数:")
                for frame_id, count in container_frame_counts.items():
                    frame_name = self.container_frames_info[frame_id]['frame_name']
                    print(f"     0x{frame_id:X} ({frame_name}): {count} 帧")
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
                'signals_decoded': total_signals_decoded,
                'msg_ids_with_signals': len(msg_ids_with_signals),
                'matched_msg_ids': len(matched_msg_ids),
                'unmatched_msg_ids': len(unmatched_msg_ids),
                'container_frame_counts': dict(container_frame_counts),
                'pool': pool
            }

            self.progress_updated.emit(self.bus_id, 100, "解析完成!")
            self.parse_finished.emit(self.bus_id, stats)

        except Exception as e:
            print(f"❌ [Bus {self.bus_id}] 解析异常: {e}")
            import traceback
            traceback.print_exc()
            self.parse_error.emit(self.bus_id, str(e))


class MDFPlotter(QWidget):
    def __init__(self):
        super().__init__()
        self.mdf_path = ""
        self.signals = {}
        self.signal_widgets = {}
        self.mdf_file = None
        self.plot_widgets = []
        self.master_viewbox = None
        self.map_dialog = None
        self.web_view = None
        self.gps_df = None
        self._temp_map_dir = None
        self.cursor2_pos = None
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
        self.auto_parse_enabled = False

        self.search_index = {}
        self.search_timer = None
        self._search_pending_text = ""
        self._parsing_bus_ids = set()

        self.progress_dialogs = {}

    def init_ui(self):
        self.setWindowTitle('VET_DATA_v26.08.28')
        self.setGeometry(100, 100, 1450, 950)

        main_layout = QHBoxLayout(self)

        # ====== 左侧面板 ======
        left_panel = QVBoxLayout()
        left_panel.setContentsMargins(5, 5, 5, 5)

        file_select_layout = QHBoxLayout()
        self.current_file_label = QLabel("未选择数据文件")
        self.current_file_label.setWordWrap(True)
        self.load_file_button = QPushButton("选择数据文件")
        file_select_layout.addWidget(self.current_file_label)
        file_select_layout.addWidget(self.load_file_button)
        file_select_layout.setStretch(0, 1)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("搜索信号...")
        self.search_box.setStyleSheet("""
            QLineEdit {
                padding: 3px 6px;
                font-size: 11px;
                border: 1px solid #ccc;
                border-radius: 3px;
            }
        """)

        select_buttons_layout = QHBoxLayout()
        self.select_all_button = QPushButton("全选")
        self.select_all_button.setStyleSheet("padding: 2px 10px;")
        self.deselect_all_button = QPushButton("全部取消")
        self.deselect_all_button.setStyleSheet("padding: 2px 10px;")
        select_buttons_layout.addWidget(self.select_all_button)
        select_buttons_layout.addWidget(self.deselect_all_button)

        config_buttons_layout = QHBoxLayout()
        self.save_config_button = QPushButton("保存配置")
        self.load_config_button = QPushButton("加载配置")
        config_buttons_layout.addWidget(self.save_config_button)
        config_buttons_layout.addWidget(self.load_config_button)

        self.math_channel_button = QPushButton("➕ 创建计算通道 (加减乘除)")
        self.math_channel_button.setStyleSheet(
            "QPushButton { background-color: #2b8a3e; color: white; font-weight: bold; }"
        )

        # ====== 可用信号区域 ======
        signal_groupbox = QGroupBox("可用信号")
        signal_groupbox.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        signal_layout_outer = QVBoxLayout()
        signal_groupbox.setLayout(signal_layout_outer)

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
        bus_groupbox.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        bus_layout = QVBoxLayout()
        bus_layout.setSpacing(2)

        bus_scroll = QScrollArea()
        bus_scroll.setWidgetResizable(True)
        bus_scroll.setMinimumHeight(150)
        bus_scroll.setFrameShape(QFrame.Shape.NoFrame)
        bus_scroll.setStyleSheet("""
            QScrollArea {
                border: none;
                background-color: transparent;
            }
        """)

        self.bus_container = QWidget()
        self.bus_container_layout = QVBoxLayout()
        self.bus_container_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.bus_container_layout.setSpacing(2)
        self.bus_container.setLayout(self.bus_container_layout)
        bus_scroll.setWidget(self.bus_container)
        bus_layout.addWidget(bus_scroll)

        # ARXML2DBC 按钮
        bus_btn_layout = QHBoxLayout()
        self.arxml2dbc_btn = QPushButton("🔧 ARXML2DBC")
        self.arxml2dbc_btn.setStyleSheet(
            "QPushButton { background-color: #9b59b6; color: white; font-weight: bold; padding: 4px; }"
        )
        self.arxml2dbc_btn.setToolTip("启动 ARXML 转 DBC 工具")
        self.arxml2dbc_btn.clicked.connect(self.open_arxml_converter)
        bus_btn_layout.addWidget(self.arxml2dbc_btn)
        bus_btn_layout.addStretch()
        bus_layout.addLayout(bus_btn_layout)

        bus_groupbox.setLayout(bus_layout)

        # ====== GPS地图面板 ======
        map_groupbox = QGroupBox("GPS轨迹图")
        map_groupbox.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        map_layout = QVBoxLayout()
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
        self.show_map_button.setStyleSheet(
            "QPushButton { background-color: #0066cc; color: white; font-weight: bold; }"
        )
        map_layout.addLayout(lat_layout)
        map_layout.addLayout(lon_layout)
        map_layout.addWidget(self.show_map_button)
        map_groupbox.setLayout(map_layout)

        # ====== 底部按钮 ======
        button_layout = QHBoxLayout()
        self.plot_button = QPushButton("绘制所选信号")
        self.export_button = QPushButton("导出为CSV")
        self.save_data_button = QPushButton("数据截取")
        button_layout.addWidget(self.plot_button)
        button_layout.addWidget(self.export_button)
        button_layout.addWidget(self.save_data_button)

        # ====== 使用 QSplitter ======
        left_splitter = QSplitter(Qt.Orientation.Vertical)
        left_splitter.addWidget(signal_groupbox)
        left_splitter.addWidget(bus_groupbox)
        left_splitter.setSizes([400, 250])
        left_splitter.setChildrenCollapsible(False)

        left_panel.addLayout(file_select_layout)
        left_panel.addWidget(self.search_box)
        left_panel.addLayout(select_buttons_layout)
        left_panel.addLayout(config_buttons_layout)
        left_panel.addWidget(self.math_channel_button)
        left_panel.addWidget(left_splitter)
        left_panel.addWidget(map_groupbox)
        left_panel.addLayout(button_layout)
        left_panel.setStretch(5, 1)

        # ====== 右侧面板 ======
        right_panel = QGroupBox("信号曲线")
        right_panel.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        right_panel_layout = QVBoxLayout(right_panel)
        self.plot_container = QWidget()
        self.plot_layout = QVBoxLayout(self.plot_container)
        self.plot_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll_area_right = QScrollArea()
        scroll_area_right.setWidgetResizable(True)
        scroll_area_right.setWidget(self.plot_container)
        right_panel_layout.addWidget(scroll_area_right)

        main_layout.addLayout(left_panel, 1)
        main_layout.addWidget(right_panel, 3)

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
        self.show_map_button.clicked.connect(self.show_map)
        self.math_channel_button.clicked.connect(self.create_math_channel)

        self.set_buttons_enabled(False)

    def auto_parse_all_buses(self):
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
                self.map_dialog = None
                self.web_view = None

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
            self.current_file_label.setText(f"当前文件: {os.path.basename(file_name)}")

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

    def load_can_bus_info(self, log_path):
        try:
            self.can_bus_data.clear()

            if log_path.lower().endswith('.blf'):
                reader = can.BLFReader(log_path)
            else:
                reader = can.LogReader(log_path)

            bus_stats = {}
            total_count = 0

            sample_interval = 100
            bus_samples = defaultdict(lambda: {'timestamps': [], 'ids': set(), 'data': [], 'is_fd': [], 'dlc': []})

            t0 = None
            bus_id_sets = defaultdict(set)

            raw_channels = set()
            has_channel_zero = False

            for msg in reader:
                if t0 is None:
                    t0 = msg.timestamp

                total_count += 1
                raw_channel = getattr(msg, 'channel', 1)
                raw_channels.add(raw_channel)
                if raw_channel == 0:
                    has_channel_zero = True

                bus_id = raw_channel
                if bus_id == 0:
                    bus_id = 0

                rel_t = msg.timestamp - t0 if t0 else 0

                is_fd = getattr(msg, 'is_fd', False) or getattr(msg, 'is_can_fd', False)

                if bus_id not in bus_stats:
                    bus_stats[bus_id] = {'count': 0, 'first_timestamp': msg.timestamp, 'is_fd': is_fd,
                                         'raw_channel': raw_channel}
                bus_stats[bus_id]['count'] += 1
                if is_fd:
                    bus_stats[bus_id]['is_fd'] = True

                # 关键：收集每个总线的仲裁ID
                bus_id_sets[bus_id].add(msg.arbitration_id)

                bus_samples[bus_id]['ids'].add(msg.arbitration_id)

                if bus_stats[bus_id]['count'] % sample_interval == 1 or bus_stats[bus_id]['count'] <= 10:
                    bus_samples[bus_id]['timestamps'].append(rel_t)
                    if hasattr(msg, 'data'):
                        bus_samples[bus_id]['data'].append(msg.data)
                    bus_samples[bus_id]['is_fd'].append(is_fd)
                    bus_samples[bus_id]['dlc'].append(len(msg.data) if hasattr(msg, 'data') else 0)

            try:
                reader.stop()
            except Exception:
                pass

            if total_count == 0:
                raise Exception("未读取到任何CAN报文")

            channel_offset = 1 if has_channel_zero else 0

            print(f"\n{'=' * 60}")
            print(f"📊 通道检测结果:")
            print(f"  原始通道号: {sorted(raw_channels)}")
            print(f"  是否包含通道0: {has_channel_zero}")
            print(f"  通道偏移量: {channel_offset}")
            if has_channel_zero:
                print(f"  ⚠️ 检测到通道0，所有通道将+1映射")
            print(f"{'=' * 60}\n")

            for raw_bus_id, samples in bus_samples.items():
                bus_id = raw_bus_id + channel_offset

                if bus_id == 0:
                    bus_id = 1

                actual_id_count = len(bus_id_sets.get(raw_bus_id, set()))
                stats = bus_stats.get(raw_bus_id, {})
                is_fd = stats.get('is_fd', False)

                # 打印每个总线的ID信息
                actual_ids_list = list(bus_id_sets.get(raw_bus_id, set()))
                print(f"📊 总线 {bus_id} (原始通道: {raw_bus_id})")
                print(f"   - 实际ID数量: {len(actual_ids_list)}")
                print(f"   - 前10个ID: {actual_ids_list[:10]}")

                self.can_bus_data[bus_id] = {
                    'timestamps': samples['timestamps'],
                    'ids': list(samples['ids']),
                    'actual_ids': actual_ids_list,  # 关键：存储实际的CAN ID列表
                    'data': samples['data'],
                    'is_fd': samples['is_fd'],
                    'dlc': samples['dlc'],
                    'msg_count': stats.get('count', 0),
                    'id_count': actual_id_count,
                    'name': f"Bus {bus_id}",
                    'has_fd': is_fd,
                    'raw_channel': stats.get('raw_channel', raw_bus_id),
                    'original_bus_id': raw_bus_id
                }

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

        QTimer.singleShot(1000, self.auto_parse_all_buses)

    def on_bus_protocol_removed(self, bus_id):
        if bus_id in self.bus_protocols:
            del self.bus_protocols[bus_id]

        if bus_id in self.db_per_bus:
            self.db_per_bus[bus_id] = {}

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
            self.update_signal_names_for_bus(bus_id, new_name)
            self.refresh_signal_list_ui()

    def update_signal_names_for_bus(self, bus_id, bus_name):
        for key, info in self.signals.items():
            if info.get('bus_id') == bus_id:
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

    def on_bus_protocol_changed(self, bus_id, protocol_info):
        # 普通 DBC
        self.bus_protocols[bus_id] = protocol_info
        if bus_id not in self.db_per_bus:
            self.db_per_bus[bus_id] = {}
        self.db_per_bus[bus_id]['arxml_sub_bus'] = ''
        self.db_per_bus[bus_id]['is_arxml'] = False

        if bus_id in self.bus_config_widgets:
            self.bus_config_widgets[bus_id].set_status('idle')

    def parse_bus(self, bus_id, auto_parse=False):
        if bus_id in self._parsing_bus_ids:
            return
        self._parsing_bus_ids.add(bus_id)

        if bus_id not in self.bus_protocols:
            self._parsing_bus_ids.discard(bus_id)
            if not auto_parse:
                QMessageBox.warning(self, "解析失败", f"Bus {bus_id} 未配置协议文件")
            return

        if bus_id in self.parse_workers and self.parse_workers[bus_id].isRunning():
            self._parsing_bus_ids.discard(bus_id)
            return

        if bus_id in self.bus_config_widgets:
            self.bus_config_widgets[bus_id].set_status('loading')

        progress_dialog = ParseProgressDialog(bus_id, self)
        progress_dialog.show()
        self.progress_dialogs[bus_id] = progress_dialog

        try:
            protocol_info = self.bus_protocols[bus_id]
            widget = self.bus_config_widgets.get(bus_id)
            db = None

            if protocol_info.lower().endswith('.dbc'):
                try:
                    if cantools is None:
                        raise ImportError("cantools 库未安装")

                    if widget and hasattr(widget, '_cached_filtered_db') and widget._cached_filtered_db is not None:
                        db = widget._cached_filtered_db
                        print("✅ 使用筛选后的DBC缓存")
                    else:
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

            worker = ParseWorker(bus_id, self.mdf_path, db, "", channel_offset=1)
            worker.progress_updated.connect(self.on_parse_progress)
            worker.parse_finished.connect(self.on_parse_finished)
            worker.parse_error.connect(self.on_parse_error)
            self.parse_workers[bus_id] = worker
            worker.start()
            print(f"🚀 总线 {bus_id} 解析任务已启动")

        except Exception as e:
            self._parsing_bus_ids.discard(bus_id)
            if bus_id in self.bus_config_widgets:
                self.bus_config_widgets[bus_id].set_status('error')
            if bus_id in self.progress_dialogs:
                self.progress_dialogs[bus_id].set_finished(False)
                self.progress_dialogs[bus_id].set_detail(f"错误: {e}")
            if not auto_parse:
                QMessageBox.warning(self, "解析失败", f"解析 Bus {bus_id} 时出错: {e}")
            import traceback
            traceback.print_exc()

    def start_parse_with_progress(self, bus_id):
        self.parse_bus(bus_id, auto_parse=True)

    def on_parse_progress(self, bus_id, progress, status_text):
        if bus_id in self.progress_dialogs:
            dialog = self.progress_dialogs[bus_id]
            if dialog and not dialog.is_cancelled():
                dialog.update_progress(progress, status_text)
                if progress < 100:
                    dialog.set_detail(f"Bus {bus_id}: 正在解析...")

    def on_parse_finished(self, bus_id, stats):
        self._parsing_bus_ids.discard(bus_id)

        pool = stats.pop('pool', {})

        if not pool:
            print(f"⚠️ 总线 {bus_id} 解析完成，但没有提取到任何信号数据")
            if bus_id in self.bus_config_widgets:
                self.bus_config_widgets[bus_id].set_status('error')
            if bus_id in self.progress_dialogs:
                self.progress_dialogs[bus_id].set_finished(False)
            return

        bus_name = self.can_bus_data.get(bus_id, {}).get('name', f"Bus {bus_id}")
        self.register_can_signals(pool, bus_name, bus_id)

        if bus_id in self.can_bus_data:
            self.can_bus_data[bus_id]['parse_stats'] = stats

        if bus_id in self.db_per_bus:
            self.db_per_bus[bus_id]['parsed'] = True

        if bus_id in self.bus_config_widgets:
            total_actual_ids = self.can_bus_data.get(bus_id, {}).get('id_count', 0)
            msg_ids_with_signals = stats.get('msg_ids_with_signals', 0)
            self.bus_config_widgets[bus_id].set_status('success')
            self.bus_config_widgets[bus_id].set_id_count(total_actual_ids, msg_ids_with_signals)

        if bus_id in self.progress_dialogs:
            self.progress_dialogs[bus_id].set_finished(True, stats)
            QTimer.singleShot(3000, lambda: self.close_progress_dialog(bus_id))

        self.refresh_signal_list_ui()

        if bus_id in self.parse_workers:
            del self.parse_workers[bus_id]

        print(f"✅ Bus {bus_id} 解析完成: {stats}")

    def on_parse_error(self, bus_id, error_msg):
        self._parsing_bus_ids.discard(bus_id)

        if bus_id in self.bus_config_widgets:
            self.bus_config_widgets[bus_id].set_status('error')

        if bus_id in self.progress_dialogs:
            self.progress_dialogs[bus_id].set_finished(False)
            self.progress_dialogs[bus_id].set_detail(f"错误: {error_msg}")

        if bus_id in self.parse_workers:
            del self.parse_workers[bus_id]

        print(f"❌ Bus {bus_id} 解析失败: {error_msg}")

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
                'comment': pack.get('comment', '')
            }

            self.signals[unique_key] = {
                'name': unique_key,
                'display_name': f"🚌 {bus_name} - {sig_name}",
                'group': -88,
                'channel': -88,
                'comment': f"{pack.get('comment', '')} ({pack.get('unit', '')})",
                'bus_id': bus_id,
                'signal_type': 'can'
            }
            self.bus_signal_map[unique_key] = bus_id
            registered_count += 1

        print(f"✅ 总线 {bus_name} (ID: {bus_id}) 注册了 {registered_count} 个CAN信号")

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
                bus_id = getattr(msg, 'channel', 0)
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
                hint_label = QLabel("💡 提示: 请在下方CAN总线配置中加载协议文件并点击\"解析\"按钮")
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

    def plot_selected_signals(self):
        for widget in self.plot_widgets:
            widget.setParent(None)
            widget.deleteLater()
        self.plot_widgets.clear()
        self.master_viewbox = None

        if not hasattr(self, 'rescale_timer'):
            self.rescale_timer = QTimer()
            self.rescale_timer.setSingleShot(True)
            self.rescale_timer.timeout.connect(self.execute_auto_scale)

        selected_signals = self.get_selected_signals()
        if not selected_signals:
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
                all_data_list.append((sig_info, signal))
                all_timestamps.extend([signal.timestamps[0], signal.timestamps[-1]])

        if not all_data_list:
            self.plot_layout.addWidget(QLabel("无法获取任何信号数据。"))
            return

        t_min = np.min(all_timestamps)
        t_max = np.max(all_timestamps)

        for i, (signal_info, signal) in enumerate(all_data_list):
            try:
                plot_widget = pg.PlotWidget(viewBox=LimitedViewBox())
                self.plot_widgets.append(plot_widget)
                self.plot_layout.addWidget(plot_widget)

                vb = plot_widget.getPlotItem().getViewBox()
                plot_widget.getPlotItem().enableAutoRange(x=False)
                vb.setMouseEnabled(x=True, y=False)
                vb.setLimits(xMin=t_min, xMax=t_max)

                left_axis = plot_widget.getPlotItem().getAxis('left')
                left_axis.setWidth(30)

                # ===== 判断是否为文字信号 =====
                is_text_signal = hasattr(signal, 'is_text_signal') and signal.is_text_signal
                has_text_mapping = hasattr(signal, 'text_mapping') and signal.text_mapping is not None

                if is_text_signal and has_text_mapping:
                    plot_widget.plot(signal.timestamps, signal.samples, pen='y')

                    ay = plot_widget.getPlotItem().getAxis('left')
                    ticks = [(j, label) for j, label in enumerate(signal.text_mapping)]
                    ay.setTicks([ticks])

                    if len(signal.text_mapping) > 0:
                        plot_widget.setYRange(-0.5, len(signal.text_mapping) - 0.5, padding=0.1)

                    title = f"信号: {signal_info['display_name']}"
                else:
                    samples = signal.samples
                    valid_mask = ~np.isnan(samples)
                    if valid_mask.any():
                        y_min_data = np.min(samples[valid_mask])
                        y_max_data = np.max(samples[valid_mask])
                    else:
                        y_min_data, y_max_data = 0.0, 1.0

                    y_range = y_max_data - y_min_data
                    if y_range == 0:
                        y_padding = 1.0 if y_min_data == 0 else np.abs(y_min_data) * 0.1
                        if y_padding == 0:
                            y_padding = 1.0
                    else:
                        y_padding = y_range * 0.4

                    plot_widget.setYRange(y_min_data - y_padding, y_max_data + y_padding, padding=0)
                    plot_widget.plot(signal.timestamps, signal.samples, pen='y')
                    plot_widget.sigXRangeChanged.connect(self.on_x_range_changed)

                    title = f"信号: {signal_info['display_name']}"

                bus_id = signal_info.get('bus_id')
                if bus_id is not None:
                    title += f" [Bus {bus_id}]"
                plot_widget.setTitle(title)

                # ===== 光标和标注 =====
                spot_item = pg.ScatterPlotItem(size=8, pen=pg.mkPen(None), brush=pg.mkBrush('r'))
                plot_widget.addItem(spot_item)

                v_line = pg.InfiniteLine(angle=90, movable=False, pen='g')
                plot_widget.addItem(v_line, ignoreBounds=True)

                text_item = pg.TextItem(text="", color=(255, 255, 0), anchor=(0, 1))
                plot_widget.addItem(text_item, ignoreBounds=True)

                plot_widget.vLine = v_line
                plot_widget.text_item = text_item
                plot_widget.spot_item = spot_item
                plot_widget.signal_data = signal
                plot_widget.signal_info = signal_info
                plot_widget.is_text_signal = is_text_signal

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

                plot_widget.setLabel('bottom', '时间', units='s')
                plot_widget.showGrid(x=True, y=True)

                if i == 0:
                    self.master_viewbox = vb
                    self.master_viewbox.setXRange(t_min, t_max)
                else:
                    vb.setXLink(self.master_viewbox)

                plot_widget.scene().sigMouseMoved.connect(self.update_cursor_positions)

            except Exception as e:
                print(f"警告: 无法绘制信号 '{signal_info.get('display_name')}'。错误: {e}")

        self.plot_layout.addStretch(1)

        if self.map_dialog and self.map_dialog.isVisible():
            QTimer.singleShot(300, self.update_map_marker)

    def _update_stats(self, plot_widget):
        """更新曲线图右上角的统计信息"""
        try:
            # 检查 widget 是否有效
            if plot_widget is None:
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

            signal = pw.signal_data

            # 文字信号不需要自动缩放Y轴
            if hasattr(signal, 'is_text_signal') and signal.is_text_signal:
                continue

            # 数值信号的自动缩放
            view_range = pw.getViewBox().viewRange()[0]
            idx_start = np.searchsorted(signal.timestamps, view_range[0])
            idx_end = np.searchsorted(signal.timestamps, view_range[1])
            visible_data = signal.samples[idx_start:idx_end]

            v_mask = ~np.isnan(visible_data)
            if len(visible_data) > 0 and v_mask.any():
                ymin, ymax = np.min(visible_data[v_mask]), np.max(visible_data[v_mask])
                yrange = ymax - ymin
                padding = yrange * 0.1 if yrange > 0 else 1.0
                pw.setYRange(ymin - padding, ymax + padding, padding=0)

    def update_cursor_positions(self, evt):
        if not self.plot_widgets or self.master_viewbox is None:
            return

        mouse_point_master = self.master_viewbox.mapSceneToView(evt)
        x_pos = mouse_point_master.x()

        for plot_widget in self.plot_widgets:
            signal = plot_widget.signal_data
            if signal is None or len(signal.timestamps) == 0:
                continue

            idx = np.searchsorted(signal.timestamps, x_pos)
            idx = np.clip(idx, 0, len(signal.timestamps) - 1)

            x_val = signal.timestamps[idx]
            y_val = signal.samples[idx]

            plot_widget.vLine.setPos(x_val)

            if np.isnan(y_val):
                plot_widget.spot_item.setData([], [])
                display_y_val = "NaN (无效数据)"
            else:
                # ===== 处理文字信号显示 =====
                if hasattr(signal, 'is_text_signal') and signal.is_text_signal:
                    # 对于文字信号，显示对应的文字
                    if hasattr(signal, 'text_mapping') and signal.text_mapping:
                        # 找到最近的文字值
                        if hasattr(signal, 'raw_text_values') and len(signal.raw_text_values) > idx:
                            display_y_val = signal.raw_text_values[idx]
                        else:
                            # 通过映射查找
                            label_idx = int(round(y_val))
                            if 0 <= label_idx < len(signal.text_mapping):
                                display_y_val = signal.text_mapping[label_idx]
                            else:
                                display_y_val = f"{y_val:.0f}"
                    else:
                        display_y_val = f"{y_val:.3f}"
                else:
                    display_y_val = f"{y_val:.3f}"

                plot_widget.spot_item.setData([x_val], [y_val])

            view_range = plot_widget.getPlotItem().getViewBox().viewRange()
            y_min, y_max = view_range[1]
            text_y_pos = y_max - (y_max - y_min) * 0.2

            plot_widget.text_item.setPos(x_val, text_y_pos)
            plot_widget.text_item.setText(f"t={x_val:.3f}s, val={display_y_val}")

        self.update_map_marker(evt)

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
            return

        lat_info = self.signals.get(lat_key)
        lon_info = self.signals.get(lon_key)
        if not lat_info or not lon_info:
            QMessageBox.warning(self, "显示轨迹", "无法获取选定的信号信息。")
            return

        lat_signal = self._get_signal(lat_info)
        lon_signal = self._get_signal(lon_info)
        if lat_signal is None or lon_signal is None or len(lat_signal) < 2:
            QMessageBox.warning(self, "显示轨迹", "GPS信号为空或数据不足。")
            return

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
            return

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

        if self.map_dialog:
            self.map_dialog.close()

        self.map_dialog = QDialog(self)
        self.map_dialog.setWindowTitle("GPS轨迹图")
        self.map_dialog.resize(900, 650)

        layout = QVBoxLayout(self.map_dialog)
        self.web_view = QWebEngineView()
        self.web_view.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        self.web_view.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)

        layout.addWidget(self.web_view)
        self.web_view.setHtml(html_content)

        def on_load(ok):
            if ok:
                QTimer.singleShot(500, self.update_map_marker)

        self.web_view.loadFinished.connect(on_load)
        self.map_dialog.show()

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
        if self.master_viewbox is None:
            QMessageBox.warning(self, "数据截取", "请先加载文件并绘制信号。")
            return

        file_extension = os.path.splitext(self.mdf_path)[1].lower()

        try:
            current_x_range = self.master_viewbox.viewRange()[0]
            start_time = current_x_range[0]
            stop_time = current_x_range[1]
        except Exception:
            QMessageBox.warning(self, "数据截取", "无法获取有效的时间范围。")
            return

        if stop_time <= start_time:
            QMessageBox.warning(self, "数据截取", "时间范围无效。")
            return

        base_name = os.path.splitext(os.path.basename(self.mdf_path))[0]

        if file_extension in ('.blf', '.asc', '.trc'):
            bus_filter = None
            if self.can_bus_data:
                bus_list = sorted(self.can_bus_data.keys())
                if len(bus_list) > 1:
                    items = ["所有总线"] + [f"Bus {b}" for b in bus_list]
                    item, ok = QInputDialog.getItem(
                        self, "选择总线", "选择要截取的总线:", items, 0, False
                    )
                    if ok and item != "所有总线":
                        bus_filter = [int(item.split()[1])]

            default_ext = '.blf' if file_extension == '.blf' else '.csv'
            filter_str = "BLF Files (*.blf);;All Files (*)" if file_extension == '.blf' else "CSV Files (*.csv)"
            default_file_name = f"{base_name}_cut_{start_time:.2f}s-{stop_time:.2f}s{default_ext}"

            file_name, _ = QFileDialog.getSaveFileName(self, "保存截取的数据", default_file_name, filter_str)
            if not file_name:
                return

            try:
                if file_name.endswith('.blf'):
                    self.cut_blf_by_time(self.mdf_path, file_name, start_time, stop_time, bus_filter)
                    QMessageBox.information(self, "截取成功", f"BLF已截取保存到:\n{file_name}")
                else:
                    self._export_can_to_csv(file_name, start_time, stop_time, bus_filter)
                    QMessageBox.information(self, "导出成功", f"数据已导出到:\n{file_name}")
            except Exception as e:
                QMessageBox.critical(self, "截取失败", str(e))
            return

        elif file_extension in ('.mdf', '.mf4'):
            default_ext = '.mf4'
            filter_str = "MDF/MF4 Files (*.mdf *.mf4)"
            default_file_name = f"{base_name}_cut_{start_time:.2f}s-{stop_time:.2f}s{default_ext}"

            file_name, _ = QFileDialog.getSaveFileName(self, "保存截取的数据 (MDF/MF4)", default_file_name, filter_str)
            if not file_name:
                return

            try:
                if not isinstance(self.mdf_file, asammdf.MDF):
                    raise Exception("内部错误：文件对象不是MDF/MF4格式。")

                mdf_cut = self.mdf_file.cut(start=start_time, stop=stop_time, time_from_zero=False)
                mdf_cut.save(file_name, overwrite=True)
                mdf_cut.close()

                QMessageBox.information(self, "数据截取成功", f"MDF/MF4 数据已截取保存到:\n{file_name}")
            except Exception as e:
                QMessageBox.critical(self, "数据截取失败", f"另存为MDF/MF4时出错: {e}")

        elif file_extension in ('.csv', '.vbo'):
            default_ext = '.csv'
            filter_str = "CSV Files (*.csv)"
            default_file_name = f"{base_name}_cut_{start_time:.2f}s-{stop_time:.2f}s{default_ext}"

            file_name, _ = QFileDialog.getSaveFileName(self, "保存截取的数据 (CSV)", default_file_name, filter_str)
            if not file_name:
                return

            try:
                selected_keys = self.get_selected_signals()
                if not selected_keys:
                    QMessageBox.warning(self, "数据截取",
                                        "由于当前数据格式非原生MDF，请先在左侧至少勾选一个信号用于截取导出。")
                    return

                data_dict = {}
                global_t = None
                for key in selected_keys:
                    sig = self._get_signal(self.signals[key])
                    if sig is not None:
                        mask = (sig.timestamps >= start_time) & (sig.timestamps <= stop_time)
                        if global_t is None:
                            global_t = sig.timestamps[mask]
                        data_dict[self.signals[key]['display_name']] = np.interp(global_t, sig.timestamps, sig.samples)

                if global_t is None or len(global_t) == 0:
                    QMessageBox.warning(self, "数据截取", "选定时间段内无可切片的数据。")
                    return

                df_cut = pd.DataFrame(data_dict, index=global_t)
                df_cut.index.name = 'timestamp'
                df_cut.to_csv(file_name, encoding='utf-8')
                QMessageBox.information(self, "数据截取成功", f"数据已成功截取并重采样保存到:\n{file_name}")
            except Exception as e:
                QMessageBox.critical(self, "数据截取失败", f"另存为CSV时出错: {e}")

    def cut_blf_by_time(self, input_path, output_path, start_time, stop_time, bus_filter=None):
        try:
            t0_per_bus = {}
            with can.BLFReader(input_path) as reader:
                for msg in reader:
                    bus_id = getattr(msg, 'channel', 0)
                    if bus_id not in t0_per_bus:
                        t0_per_bus[bus_id] = msg.timestamp
                    if len(t0_per_bus) > 0:
                        break

            if not t0_per_bus:
                raise Exception("无法读取BLF文件起始时间")

            count = 0
            total_count = 0
            bus_stats = defaultdict(int)

            try:
                from can import BLFWriter
                with BLFWriter(output_path) as writer:
                    with can.BLFReader(input_path) as reader:
                        for msg in reader:
                            total_count += 1
                            bus_id = getattr(msg, 'channel', 0)
                            t0 = t0_per_bus.get(bus_id, t0_per_bus.get(0, msg.timestamp))
                            rel_t = msg.timestamp - t0

                            if bus_filter is not None and bus_id not in bus_filter:
                                continue

                            if start_time <= rel_t <= stop_time:
                                if hasattr(writer, 'write'):
                                    writer.write(msg)
                                elif hasattr(writer, 'on_message_received'):
                                    writer.on_message_received(msg)
                                else:
                                    writer.write(msg)
                                count += 1
                                bus_stats[bus_id] += 1

                        if count == 0:
                            raise Exception(f"指定时间范围内没有找到任何报文 (总报文数: {total_count})")

                        print(f"BLF截取完成: 共截取 {count} 条报文")
                        for bus_id, cnt in bus_stats.items():
                            print(f"  总线 {bus_id}: {cnt} 条")
                return True

            except Exception as e:
                print(f"BLF截取失败: {e}")
                raise

        except Exception as e:
            raise Exception(f"BLF截取失败: {e}")

    def _export_can_to_csv(self, output_path, start_time, stop_time, bus_filter=None):
        try:
            if not self.can_parsed_data:
                raise Exception("没有可用的CAN解析数据，请先配置并解析总线")

            data_dict = {'timestamp': []}
            for key, data in self.can_parsed_data.items():
                bus_id = data.get('bus_id')
                if bus_filter is not None and bus_id not in bus_filter:
                    continue

                mask = (data['timestamps'] >= start_time) & (data['timestamps'] <= stop_time)
                timestamps = data['timestamps'][mask]
                samples = data['samples'][mask]

                if len(timestamps) == 0:
                    continue

                if len(data_dict['timestamp']) == 0:
                    data_dict['timestamp'] = timestamps

                signal_name = self.signals.get(key, {}).get('display_name', key)
                data_dict[signal_name] = np.interp(data_dict['timestamp'], timestamps, samples)

            df = pd.DataFrame(data_dict)
            df.to_csv(output_path, index=False, encoding='utf-8')
            print(f"CSV导出完成: {len(data_dict['timestamp'])} 行数据")

        except Exception as e:
            raise Exception(f"导出CSV失败: {e}")

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
                                self.bus_config_widgets[bus_id].protocol_label.setText(os.path.basename(protocol))
                                self.bus_config_widgets[bus_id].protocol_label.setStyleSheet(
                                    "color: #27ae60; font-size: 9px;")
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
            for widget in self.plot_widgets:
                try:
                    widget.setParent(None)
                    widget.deleteLater()
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
