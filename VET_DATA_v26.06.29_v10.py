import sys
import os
import threading

import asammdf
import pyqtgraph as pg
import pandas as pd
import json
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QScrollArea, QGroupBox,
    QLabel, QFileDialog, QGridLayout, QLineEdit, QDialog,
    QInputDialog, QMessageBox, QComboBox,
    QFrame, QListWidget, QListWidgetItem, QProgressBar,
    QSpinBox,QSizePolicy,QStyledItemDelegate
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread
import numpy as np
from asammdf.blocks.utils import MdfException
from asammdf import Signal
from PyQt6.QtWebEngineCore import QWebEngineSettings
from collections import defaultdict
import warnings
import logging
import re
from io import StringIO

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

class SignalItemDelegate(QStyledItemDelegate):
    """自定义委托：信号列表项左对齐名称，右对齐数值，名称过长省略"""
    def paint(self, painter, option, index):
        original_name = index.data(Qt.ItemDataRole.UserRole + 2)
        value_str = index.data(Qt.ItemDataRole.UserRole + 3)

        if original_name is None:
            super().paint(painter, option, index)
            return

        super().paint(painter, option, index)

        painter.save()
        painter.setPen(option.palette.text().color())

        rect = option.rect
        left_margin = 28
        right_margin = 8

        font_metrics = painter.fontMetrics()
        value_width = 0
        if value_str is not None and value_str != "":
            value_width = font_metrics.horizontalAdvance(value_str) + 4

        available_width = rect.width() - left_margin - right_margin
        name_max_width = available_width - value_width
        if name_max_width < 10:
            name_max_width = 10

        elided_name = font_metrics.elidedText(original_name, Qt.TextElideMode.ElideRight, name_max_width)

        name_rect = rect.adjusted(left_margin, 0, -right_margin - value_width, 0)
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided_name)

        if value_str is not None and value_str != "":
            value_rect = rect.adjusted(rect.width() - right_margin - value_width, 0, -right_margin, 0)
            painter.drawText(value_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, value_str)

        painter.restore()

from PyQt6.QtWidgets import QCompleter
from PyQt6.QtCore import QStringListModel, Qt

class MathChannelDialog(QDialog):
    def __init__(self, available_signals, parent=None):
        super().__init__(parent)
        self.available_signals = available_signals
        self.display_to_key = {}
        for key, info in available_signals.items():
            self.display_to_key[info['display_name']] = key
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("创建自定义计算通道")
        self.resize(450, 250)
        layout = QVBoxLayout(self)
        grid = QGridLayout()

        grid.addWidget(QLabel("信号 A (被除数/被减数):"), 0, 0)
        self.combo_a = QComboBox()
        self.combo_a.setEditable(True)
        self.combo_a.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo_a.setMaxVisibleItems(20)
        grid.addWidget(self.combo_a, 0, 1)

        grid.addWidget(QLabel("运算符:"), 1, 0)
        self.combo_op = QComboBox()
        self.combo_op.addItems(["+ (加)", "- (减)", "* (乘)", "/ (除)"])
        grid.addWidget(self.combo_op, 1, 1)

        grid.addWidget(QLabel("信号 B (除数/减数):"), 2, 0)
        self.combo_b = QComboBox()
        self.combo_b.setEditable(True)
        self.combo_b.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo_b.setMaxVisibleItems(20)
        grid.addWidget(self.combo_b, 2, 1)

        grid.addWidget(QLabel("除零/无效值保护策略:"), 3, 0)
        self.combo_zero_policy = QComboBox()
        self.combo_zero_policy.addItem("分母为0时结果赋 0.0", "zero")
        self.combo_zero_policy.addItem("分母为0时结果赋 NaN (曲线断开)", "nan")
        grid.addWidget(self.combo_zero_policy, 3, 1)

        grid.addWidget(QLabel("新信号名称:"), 4, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如: Math_Signal_Result")
        grid.addWidget(self.name_edit, 4, 1)

        layout.addLayout(grid)

        display_names = sorted(self.display_to_key.keys())

        for combo in (self.combo_a, self.combo_b):
            for name in display_names:
                combo.addItem(name, self.display_to_key[name])

            completer = QCompleter(display_names, self)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            combo.setCompleter(completer)
            completer.activated.connect(lambda text, c=combo: self._on_completer_activated(c, text))

        self.combo_op.currentIndexChanged.connect(self.toggle_policy_visibility)
        self.toggle_policy_visibility()

        btn_layout = QHBoxLayout()
        self.btn_ok = QPushButton("生成通道")
        self.btn_cancel = QPushButton("取消")
        btn_layout.addWidget(self.btn_ok)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)

        self.btn_ok.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)

    def _on_completer_activated(self, combo, text):
        key = self.display_to_key.get(text)
        if key is not None:
            idx = combo.findText(text)
            if idx >= 0:
                combo.setCurrentIndex(idx)

    def toggle_policy_visibility(self):
        is_div = "/" in self.combo_op.currentText()
        self.combo_zero_policy.setEnabled(is_div)

    def get_config(self):
        key_a = self.combo_a.currentData()
        key_b = self.combo_b.currentData()
        if key_a is None:
            QMessageBox.warning(self, "选择错误", "信号 A 未选择有效项，请从下拉列表中选择一个信号。")
            return None
        if key_b is None:
            QMessageBox.warning(self, "选择错误", "信号 B 未选择有效项，请从下拉列表中选择一个信号。")
            return None
        return {
            "key_a": key_a,
            "key_b": key_b,
            "op": self.combo_op.currentText()[0],
            "policy": self.combo_zero_policy.currentData(),
            "new_name": self.name_edit.text().strip()
        }


class BusConfigWidget(QWidget):
    """单个总线的配置组件"""
    bus_id_changed = pyqtSignal(int, str)
    protocol_changed = pyqtSignal(int, str)
    parse_requested = pyqtSignal(int)
    protocol_removed = pyqtSignal(int)

    _arxml_cache = {}
    _cache_lock = threading.Lock()

    def __init__(self, bus_id, parent=None):
        super().__init__(parent)
        self.bus_id = bus_id
        self._instance_id = id(self)
        self.protocol_path = ""
        self.arxml_sub_bus = ""
        self.arxml_sub_bus_list = []
        self.init_ui()
        self.protocol_clear_btn = None

    def init_ui(self):
        layout = QHBoxLayout()
        layout.setContentsMargins(5, 4, 5, 4)
        layout.setSpacing(5)

        self.id_label = QLabel(f"Bus {self.bus_id}")
        self.id_label.setMinimumWidth(50)
        self.id_label.setStyleSheet("font-weight: bold; color: #2c3e50; font-size: 11px;")
        layout.addWidget(self.id_label)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(f"Bus {self.bus_id}")
        self.name_edit.setMinimumWidth(80)
        self.name_edit.textChanged.connect(self.on_name_changed)
        layout.addWidget(self.name_edit)

        self.type_label = QLabel("CAN2.0")
        self.type_label.setMinimumWidth(55)
        self.type_label.setStyleSheet("color: #2980b9; font-size: 10px; font-weight: bold;")
        layout.addWidget(self.type_label)

        self.id_count_label = QLabel("0 IDs")
        self.id_count_label.setMinimumWidth(50)
        self.id_count_label.setStyleSheet("color: #7f8c8d; font-size: 10px;")
        layout.addWidget(self.id_count_label)

        self.protocol_label = QLabel("未配置")
        self.protocol_label.setMinimumWidth(120)
        self.protocol_label.setStyleSheet("color: #e74c3c; font-size: 9px;")
        self.protocol_label.setWordWrap(True)
        layout.addWidget(self.protocol_label)

        self.select_protocol_btn = QPushButton("选择协议")
        self.select_protocol_btn.setFixedWidth(65)
        self.select_protocol_btn.setStyleSheet(
            "QPushButton { background-color: #3498db; color: white; font-size: 9px; }")
        self.select_protocol_btn.clicked.connect(self.select_protocol)
        layout.addWidget(self.select_protocol_btn)

        self.protocol_clear_btn = QPushButton("✕")
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

        self.parse_btn = QPushButton("解析")
        self.parse_btn.setFixedWidth(45)
        self.parse_btn.setStyleSheet("QPushButton { background-color: #27ae60; color: white; font-size: 9px; }")
        self.parse_btn.clicked.connect(lambda: self.parse_requested.emit(self.bus_id))
        layout.addWidget(self.parse_btn)

        self.status_indicator = QLabel("⚪")
        self.status_indicator.setFixedWidth(18)
        layout.addWidget(self.status_indicator)

        self.setLayout(layout)

    def on_name_changed(self, text):
        self.bus_id_changed.emit(self.bus_id, text.strip() if text.strip() else f"Bus {self.bus_id}")

    def select_protocol(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, f"选择 Bus {self.bus_id} 的协议文件", "",
            "DBC/ARXML Files (*.dbc *.arxml);;DBC Files (*.dbc);;ARXML Files (*.arxml);;All Files (*)"
        )
        if not file_path:
            return

        if self.protocol_clear_btn is None:
            self.protocol_clear_btn = QPushButton("✕")
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

        ext = os.path.splitext(file_path)[1].lower()

        if ext == '.dbc':
            self.protocol_path = file_path
            self.arxml_sub_bus = ""
            self.protocol_label.setText(os.path.basename(file_path))
            self.protocol_label.setStyleSheet("color: #27ae60; font-size: 9px;")
            if self.protocol_clear_btn:
                self.protocol_clear_btn.setVisible(True)
            self.protocol_changed.emit(self.bus_id, file_path)
        else:
            self.load_arxml_and_select_sub_bus(file_path)

    def load_arxml_and_select_sub_bus(self, arxml_path):
        try:
            with BusConfigWidget._cache_lock:
                if arxml_path in BusConfigWidget._arxml_cache:
                    cache_entry = BusConfigWidget._arxml_cache[arxml_path]
                    sub_buses = cache_entry.get('sub_buses', [])
                else:
                    sub_buses = self._extract_sub_buses_from_arxml(arxml_path)
                    BusConfigWidget._arxml_cache[arxml_path] = {
                        'sub_buses': sub_buses,
                        'dbs': {},
                        'selected_sub_bus': '',
                        'db_loaded': False,
                        'full_db': None
                    }

            self.arxml_sub_bus_list = sub_buses

            if len(sub_buses) > 1:
                dialog = QDialog(self)
                dialog.setWindowTitle("选择ARXML子总线/域控")
                dialog.setMinimumWidth(500)
                layout = QVBoxLayout(dialog)

                layout.addWidget(QLabel(f"ARXML文件中包含 {len(sub_buses)} 个子总线/域控，请选择对应的："))

                list_widget = QListWidget()
                for item in sub_buses:
                    list_widget.addItem(item)
                layout.addWidget(list_widget)

                btn_layout = QHBoxLayout()
                ok_btn = QPushButton("确认")
                cancel_btn = QPushButton("取消")
                btn_layout.addWidget(ok_btn)
                btn_layout.addWidget(cancel_btn)
                layout.addLayout(btn_layout)

                ok_btn.clicked.connect(dialog.accept)
                cancel_btn.clicked.connect(dialog.reject)

                if dialog.exec() == QDialog.DialogCode.Accepted:
                    selected = list_widget.currentItem()
                    if selected:
                        selected_sub_bus = selected.text()
                    else:
                        selected_sub_bus = sub_buses[0]
                else:
                    return
            else:
                selected_sub_bus = sub_buses[0]

            with BusConfigWidget._cache_lock:
                if arxml_path in BusConfigWidget._arxml_cache:
                    BusConfigWidget._arxml_cache[arxml_path]['selected_sub_bus'] = selected_sub_bus

            self.arxml_sub_bus = selected_sub_bus

            self.protocol_path = arxml_path
            self.protocol_label.setText(f"{os.path.basename(arxml_path)} [{selected_sub_bus}]")
            self.protocol_label.setStyleSheet("color: #27ae60; font-size: 9px;")
            self.protocol_changed.emit(self.bus_id, f"{arxml_path}||{selected_sub_bus}")

            if self.protocol_clear_btn:
                self.protocol_clear_btn.setVisible(True)

        except Exception as e:
            QMessageBox.warning(self, "ARXML加载失败", f"加载ARXML时出错: {e}")
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

    def _extract_sub_buses_from_arxml(self, arxml_path):
        sub_buses = []

        try:
            import canmatrix
            import canmatrix.formats.arxml

            with open(arxml_path, 'r', encoding='utf-8') as f:
                dbs = canmatrix.formats.arxml.load(f)

            if isinstance(dbs, dict):
                for bus_name in dbs.keys():
                    if bus_name and bus_name not in sub_buses:
                        sub_buses.append(bus_name)

                for bus_name, db in dbs.items():
                    if hasattr(db, '_pdu_containers'):
                        for pdu_name, pdu_info in db._pdu_containers.items():
                            if 'Bus' in pdu_name or 'CAN' in pdu_name:
                                if pdu_name not in sub_buses:
                                    sub_buses.append(pdu_name)

                    if hasattr(db, 'frames'):
                        for frame in db.frames:
                            if hasattr(frame, 'transmitters') and frame.transmitters:
                                for tx in frame.transmitters:
                                    if tx and tx not in sub_buses:
                                        sub_buses.append(tx)
                            if hasattr(frame, 'receivers') and frame.receivers:
                                for rx in frame.receivers:
                                    if rx and rx not in sub_buses:
                                        sub_buses.append(rx)

                            if hasattr(frame, 'pdu_refs') and frame.pdu_refs:
                                for pdu_ref in frame.pdu_refs:
                                    if isinstance(pdu_ref, dict):
                                        pdu_name = pdu_ref.get('name', '')
                                        if pdu_name and pdu_name not in sub_buses:
                                            sub_buses.append(pdu_name)

        except Exception as e:
            print(f"canmatrix 解析失败: {e}")

        seen = set()
        unique_sub_buses = []
        for item in sub_buses:
            if item not in seen:
                seen.add(item)
                unique_sub_buses.append(item)

        return unique_sub_buses if unique_sub_buses else ["Default"]

    def _convert_arxml_with_pdu_support(self, arxml_path, selected_sub_bus):
        try:
            import canmatrix
            import canmatrix.formats.arxml
        except ImportError:
            raise Exception("需要安装 canmatrix: pip install canmatrix")

        with open(arxml_path, 'r', encoding='utf-8') as f:
            dbs = canmatrix.formats.arxml.load(f)

        if not isinstance(dbs, dict):
            dbs = {"Default": dbs}

        target_db = None
        actual_bus_name = selected_sub_bus

        if selected_sub_bus and selected_sub_bus in dbs:
            target_db = dbs[selected_sub_bus]
        else:
            for name in dbs:
                if selected_sub_bus and selected_sub_bus.lower() in name.lower():
                    target_db = dbs[name]
                    actual_bus_name = name
                    break
            if target_db is None:
                actual_bus_name = list(dbs.keys())[0]
                target_db = dbs[actual_bus_name]

        print(f"✅ 使用子总线: {actual_bus_name}")
        print(f"   包含 {len(target_db.frames)} 个帧")

        pdu_signal_map = {}
        if hasattr(target_db, '_pdu_containers'):
            for pdu_name, pdu in target_db._pdu_containers.items():
                if hasattr(pdu, 'signals'):
                    pdu_signal_map[pdu_name] = list(pdu.signals)
                    print(f"   📦 PDU '{pdu_name}': {len(pdu.signals)} 个信号")

        frame_pdu_map = {}
        for frame in target_db.frames:
            if hasattr(frame, 'pdu_refs') and frame.pdu_refs:
                frame_pdu_map[frame.name] = []
                for pdu_ref in frame.pdu_refs:
                    if isinstance(pdu_ref, dict):
                        pdu_name = pdu_ref.get('name', '')
                        if pdu_name:
                            frame_pdu_map[frame.name].append(pdu_name)
                    elif isinstance(pdu_ref, str):
                        frame_pdu_map[frame.name].append(pdu_ref)
                    elif hasattr(pdu_ref, 'name'):
                        frame_pdu_map[frame.name].append(pdu_ref.name)

        from cantools.database.can import Database, Message, Signal

        db = Database()

        for frame in target_db.frames:
            try:
                if hasattr(frame, 'arbitration_id'):
                    if hasattr(frame.arbitration_id, 'id'):
                        frame_id = frame.arbitration_id.id
                    else:
                        frame_id = frame.arbitration_id
                else:
                    frame_id = getattr(frame, 'id', 0)

                frame_name = getattr(frame, 'name', f"FRAME_{frame_id:X}")
                frame_length = getattr(frame, 'size', 8)
                if frame_length <= 0 or frame_length > 64:
                    frame_length = 8

                msg = Message(
                    frame_id=frame_id,
                    name=frame_name,
                    length=frame_length,
                    senders=getattr(frame, 'transmitters', []),
                    signals=[],
                    comment=getattr(frame, 'comment', None)
                )

                all_signals = []

                if hasattr(frame, 'signals'):
                    all_signals.extend(frame.signals)

                if frame.name in frame_pdu_map:
                    for pdu_name in frame_pdu_map[frame.name]:
                        if pdu_name in pdu_signal_map:
                            all_signals.extend(pdu_signal_map[pdu_name])
                            print(f"   🔗 Frame '{frame_name}' 关联 PDU '{pdu_name}'")

                for sig in all_signals:
                    try:
                        sig_obj = self._convert_canmatrix_signal(sig)
                        if sig_obj:
                            existing_names = [s.name for s in msg.signals]
                            if sig_obj.name in existing_names:
                                suffix = 1
                                while f"{sig_obj.name}_{suffix}" in existing_names:
                                    suffix += 1
                                sig_obj.name = f"{sig_obj.name}_{suffix}"
                            msg.signals.append(sig_obj)
                    except Exception as e:
                        print(f"  ⚠️ 转换信号失败: {e}")

                if msg.signals:
                    db.messages.append(msg)
                    print(f"  ✅ Frame '{frame_name}' (ID: 0x{frame_id:X}) 转换成功，包含 {len(msg.signals)} 个信号")

            except Exception as e:
                print(f"⚠️ 转换Frame失败: {e}")
                import traceback
                traceback.print_exc()

        print(f"\n✅ 转换完成: {len(db.messages)} 个消息")
        total_signals = sum(len(m.signals) for m in db.messages)
        print(f"   包含 {total_signals} 个信号")

        return db, actual_bus_name

    def _convert_canmatrix_signal(self, cm_sig):
        from cantools.database.can import Signal

        try:
            sig_name = getattr(cm_sig, 'name', f"SIG_{id(cm_sig)}")

            is_little_endian = True
            if hasattr(cm_sig, 'is_little_endian'):
                is_little_endian = cm_sig.is_little_endian
            elif hasattr(cm_sig, 'byte_order'):
                is_little_endian = (cm_sig.byte_order == 0)

            start_bit = 0
            if hasattr(cm_sig, 'start_bit'):
                start_bit = cm_sig.start_bit
            elif hasattr(cm_sig, 'bit_offset'):
                start_bit = cm_sig.bit_offset
            elif hasattr(cm_sig, 'start'):
                start_bit = cm_sig.start

            bit_length = 1
            if hasattr(cm_sig, 'size'):
                bit_length = cm_sig.size
            elif hasattr(cm_sig, 'length'):
                bit_length = cm_sig.length

            scale = 1.0
            if hasattr(cm_sig, 'factor') and cm_sig.factor is not None:
                scale = cm_sig.factor
            elif hasattr(cm_sig, 'scale') and cm_sig.scale is not None:
                scale = cm_sig.scale

            offset = 0.0
            if hasattr(cm_sig, 'offset') and cm_sig.offset is not None:
                offset = cm_sig.offset

            unit = ""
            if hasattr(cm_sig, 'unit') and cm_sig.unit:
                unit = cm_sig.unit
            elif hasattr(cm_sig, 'units') and cm_sig.units:
                unit = cm_sig.units

            choices = {}
            if hasattr(cm_sig, 'choices') and cm_sig.choices:
                choices = cm_sig.choices
            elif hasattr(cm_sig, 'value_table') and cm_sig.value_table:
                choices = cm_sig.value_table

            comment = None
            if hasattr(cm_sig, 'comment') and cm_sig.comment:
                comment = cm_sig.comment

            try:
                sig = Signal(
                    name=sig_name,
                    start=start_bit,
                    length=bit_length,
                    scale=scale,
                    offset=offset,
                    unit=unit,
                    is_little_endian=is_little_endian,
                    comment=comment,
                    choices=choices
                )
            except TypeError as e:
                if 'scale' in str(e):
                    try:
                        sig = Signal(
                            name=sig_name,
                            start=start_bit,
                            length=bit_length,
                            scaling=scale,
                            offset=offset,
                            unit=unit,
                            is_little_endian=is_little_endian,
                            comment=comment,
                            choices=choices
                        )
                    except TypeError as e2:
                        if 'scaling' in str(e2):
                            try:
                                sig = Signal(
                                    name=sig_name,
                                    start=start_bit,
                                    length=bit_length,
                                    factor=scale,
                                    offset=offset,
                                    unit=unit,
                                    is_little_endian=is_little_endian,
                                    comment=comment,
                                    choices=choices
                                )
                            except TypeError as e3:
                                print(f"  ⚠️ 尝试使用最简参数创建 Signal")
                                sig = Signal(
                                    name=sig_name,
                                    start=start_bit,
                                    length=bit_length,
                                    is_little_endian=is_little_endian
                                )
                                sig.choices = choices
                                sig.comment = comment
                                sig.unit = unit
                                if hasattr(sig, 'scale'):
                                    sig.scale = scale
                                if hasattr(sig, 'offset'):
                                    sig.offset = offset
                else:
                    raise

            return sig

        except Exception as e:
            print(f"  转换信号失败: {e}")
            return None

    def _convert_pdu_signal(self, cm_sig, frame):
        from cantools.database.can import Signal

        try:
            sig_name = getattr(cm_sig, 'name', f"SIG_{id(cm_sig)}")

            is_little_endian = True
            if hasattr(cm_sig, 'is_little_endian'):
                is_little_endian = cm_sig.is_little_endian
            elif hasattr(cm_sig, 'byte_order'):
                is_little_endian = (cm_sig.byte_order == 0)

            start_bit = 0
            if hasattr(cm_sig, 'start_bit'):
                start_bit = cm_sig.start_bit
            elif hasattr(cm_sig, 'bit_offset'):
                start_bit = cm_sig.bit_offset
            elif hasattr(cm_sig, 'start'):
                start_bit = cm_sig.start

            bit_length = 1
            if hasattr(cm_sig, 'size'):
                bit_length = cm_sig.size
            elif hasattr(cm_sig, 'length'):
                bit_length = cm_sig.length

            scale = 1.0
            if hasattr(cm_sig, 'factor') and cm_sig.factor is not None:
                scale = cm_sig.factor
            elif hasattr(cm_sig, 'scale') and cm_sig.scale is not None:
                scale = cm_sig.scale

            offset = 0.0
            if hasattr(cm_sig, 'offset') and cm_sig.offset is not None:
                offset = cm_sig.offset

            unit = ""
            if hasattr(cm_sig, 'unit') and cm_sig.unit:
                unit = cm_sig.unit
            elif hasattr(cm_sig, 'units') and cm_sig.units:
                unit = cm_sig.units

            choices = {}
            if hasattr(cm_sig, 'choices') and cm_sig.choices:
                choices = cm_sig.choices
            elif hasattr(cm_sig, 'value_table') and cm_sig.value_table:
                choices = cm_sig.value_table

            sig = Signal(
                name=sig_name,
                start=start_bit,
                length=bit_length,
                scale=scale,
                offset=offset,
                unit=unit,
                is_little_endian=is_little_endian,
                comment=getattr(cm_sig, 'comment', None),
                choices=choices
            )

            return sig

        except Exception as e:
            print(f"  转换PDU信号失败: {e}")
            return None

    def _load_arxml_with_cantools(self, arxml_path):
        try:
            db = cantools.database.load_file(arxml_path)
            sub_buses = []

            for msg in db.messages:
                if hasattr(msg, 'senders') and msg.senders:
                    for sender in msg.senders:
                        if sender and sender not in sub_buses:
                            sub_buses.append(sender)
                if hasattr(msg, 'receivers') and msg.receivers:
                    for receiver in msg.receivers:
                        if receiver and receiver not in sub_buses:
                            sub_buses.append(receiver)

            for msg in db.messages:
                if msg.comment:
                    patterns = [
                        r'ECU[:_\s]*([a-zA-Z0-9_]+)',
                        r'Bus[:_\s]*([a-zA-Z0-9_]+)',
                        r'Cluster[:_\s]*([a-zA-Z0-9_]+)',
                        r'Domain[:_\s]*([a-zA-Z0-9_]+)',
                        r'Controller[:_\s]*([a-zA-Z0-9_]+)'
                    ]
                    for pattern in patterns:
                        matches = re.findall(pattern, msg.comment, re.IGNORECASE)
                        for match in matches:
                            if match and match not in sub_buses:
                                sub_buses.append(match)

            for msg in db.messages:
                if msg.name:
                    if 'Bus' in msg.name or 'ECU' in msg.name or 'Cluster' in msg.name:
                        parts = re.split(r'[_\-]', msg.name)
                        for part in parts:
                            if (len(part) > 2 and
                                    ('Bus' in part or 'ECU' in part or 'Cluster' in part) and
                                    part not in sub_buses):
                                sub_buses.append(part)

            if hasattr(db, 'attributes') and db.attributes:
                for attr_name, attr_value in db.attributes.items():
                    if attr_name:
                        match = re.search(r'(Bus|ECU|Cluster)[:_\s]*([a-zA-Z0-9_]+)', attr_name, re.IGNORECASE)
                        if match:
                            bus_name = match.group(2)
                            if bus_name and bus_name not in sub_buses:
                                sub_buses.append(bus_name)

            if not sub_buses:
                id_groups = defaultdict(list)
                for msg in db.messages:
                    group_key = msg.frame_id >> 8
                    id_groups[group_key].append(msg.frame_id)

                if len(id_groups) > 1 and len(id_groups) <= 10:
                    sub_buses = [f"Bus_{i + 1}" for i in range(len(id_groups))]
                else:
                    base_name = os.path.splitext(os.path.basename(arxml_path))[0]
                    sub_buses = [base_name]

            seen = set()
            unique_sub_buses = []
            for item in sub_buses:
                if item not in seen:
                    seen.add(item)
                    unique_sub_buses.append(item)

            exclude_keywords = ['CAN', 'FD', 'SIG', 'DATA', 'VAL', 'BIT', 'BYTE', 'CTRL']
            filtered = [s for s in unique_sub_buses if s.upper() not in exclude_keywords and len(s) > 2]

            return filtered if filtered else unique_sub_buses

        except Exception as e:
            print(f"使用 cantools 加载 ARXML 失败: {e}")
            return []

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

    @classmethod
    def clear_arxml_cache(cls):
        with cls._cache_lock:
            cls._arxml_cache.clear()


class ParseProgressDialog(QDialog):
    """解析进度对话框"""

    def __init__(self, bus_id, parent=None):
        super().__init__(parent)
        self.bus_id = bus_id
        self._instance_id = id(self)

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

        self.status_label = QLabel("正在初始化解析...")
        self.status_label.setStyleSheet("font-size: 12px; font-weight: bold; color: #2c3e50;")
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
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

        self.detail_label = QLabel("准备开始...")
        self.detail_label.setStyleSheet("color: #7f8c8d; font-size: 10px;")
        layout.addWidget(self.detail_label)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.cancel_btn = QPushButton("取消")
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

        self._cancelled = False

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

    def closeEvent(self, event):
        self._cancelled = True
        event.accept()


class ParseWorker(QThread):
    """后台解析线程"""
    progress_updated = pyqtSignal(int, int, str)
    parse_finished = pyqtSignal(int, dict)
    parse_error = pyqtSignal(int, str)

    def __init__(self, bus_id, log_path, db, arxml_sub_bus="", parent=None):
        super().__init__(parent)
        self.bus_id = bus_id
        self.log_path = log_path
        self.db = db
        self.arxml_sub_bus = arxml_sub_bus
        self._is_running = True

    def stop(self):
        self._is_running = False

    def _check_bus_match(self, msg_def, sub_bus: str) -> bool:
        if not sub_bus or sub_bus in ["Default", "All", "未配置", "全部", "None"]:
            return True

        target_bus = sub_bus.strip().lower()

        msg_bus = getattr(msg_def, 'bus_name', None) or getattr(msg_def, 'bus', None)
        if msg_bus:
            msg_bus_str = str(msg_bus).strip().lower()
            if msg_bus_str and msg_bus_str != "none":
                if msg_bus_str != target_bus and target_bus not in msg_bus_str:
                    if not self._has_sender_receiver_match(msg_def, target_bus):
                        return False

        if self._has_sender_receiver_match(msg_def, target_bus):
            return True

        return True

    def _has_sender_receiver_match(self, msg_def, target_bus):
        senders = getattr(msg_def, 'senders', []) or getattr(msg_def, 'transmitters', [])
        if isinstance(senders, (list, tuple, set)):
            for sender in senders:
                if sender and target_bus in str(sender).strip().lower():
                    return True

        receivers = getattr(msg_def, 'receivers', [])
        if isinstance(receivers, (list, tuple, set)):
            for receiver in receivers:
                if receiver and target_bus in str(receiver).strip().lower():
                    return True
        return False

    def run(self):
        try:
            if self.log_path.lower().endswith('.blf'):
                reader = can.BLFReader(self.log_path)
            else:
                reader = can.LogReader(self.log_path)

            msg_shard = {}
            msg_shard_hex = {}
            msg_shard_dec = {}
            msg_shard_std = {}
            msg_shard_ext = {}

            for msg in self.db.messages:
                msg_shard[msg.frame_id] = msg
                hex_key = f"0x{msg.frame_id:X}"
                msg_shard_hex[hex_key] = msg
                hex_key_no_prefix = f"{msg.frame_id:X}"
                msg_shard_hex[hex_key_no_prefix] = msg
                msg_shard_dec[str(msg.frame_id)] = msg

                ext_id_masked = msg.frame_id & 0x1FFFFFFF
                if ext_id_masked != msg.frame_id:
                    msg_shard[ext_id_masked] = msg
                    msg_shard_hex[f"0x{ext_id_masked:X}"] = msg
                    msg_shard_dec[str(ext_id_masked)] = msg

                std_id = msg.frame_id & 0x7FF
                if std_id != msg.frame_id and std_id not in msg_shard:
                    msg_shard[std_id] = msg

                if hasattr(msg, 'is_extended_frame') and msg.is_extended_frame:
                    msg_shard_ext[msg.frame_id] = msg
                    masked = msg.frame_id & 0x1FFFFFFF
                    if masked != msg.frame_id:
                        msg_shard_ext[masked] = msg

            print(f"总线 {self.bus_id}: 数据库中有 {len(msg_shard)} 个消息定义")
            sample_ids = list(msg_shard.keys())[:10]
            print(f"  消息ID示例: {[f'0x{id:X}' for id in sample_ids]}")

            msg_signal_map = {}
            for msg in self.db.messages:
                sig_map = {}
                for sig in msg.signals:
                    sig_map[sig.name] = sig
                msg_signal_map[msg.frame_id] = sig_map

            total_msgs = 0
            try:
                temp_reader = can.BLFReader(self.log_path) if self.log_path.lower().endswith('.blf') else can.LogReader(
                    self.log_path)

                count = 0
                for msg in temp_reader:
                    msg_bus_id = getattr(msg, 'channel', 0)
                    if msg_bus_id == self.bus_id:
                        count += 1
                temp_reader.stop()
                total_msgs = count
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

            if total_msgs > 0:
                progress_interval = max(1, total_msgs // 200)
            else:
                progress_interval = 1000

            last_progress_update = 0
            last_progress_percent = 0

            for msg in reader:
                if not self._is_running:
                    break

                if t0 is None:
                    t0 = msg.timestamp

                msg_bus_id = getattr(msg, 'channel', 0)
                if msg_bus_id != self.bus_id:
                    continue

                rel_t = msg.timestamp - t0

                msg_def = None
                msg_id = msg.arbitration_id

                msg_def = msg_shard.get(msg_id)
                if msg_def is None:
                    hex_key = f"0x{msg_id:X}"
                    msg_def = msg_shard_hex.get(hex_key)
                if msg_def is None:
                    msg_def = msg_shard_dec.get(str(msg_id))
                if msg_def is None:
                    ext_id_masked = msg_id & 0x1FFFFFFF
                    if ext_id_masked != msg_id:
                        msg_def = msg_shard.get(ext_id_masked)
                    if msg_def is None:
                        std_id = msg_id & 0x7FF
                        if std_id != msg_id:
                            msg_def = msg_shard.get(std_id)

                if msg_def is None:
                    unmatched_msg_ids.add(msg_id)
                    continue

                matched_msg_ids.add(msg_id)

                if self.arxml_sub_bus and self.arxml_sub_bus != "Default":
                    if not self._check_bus_match(msg_def, self.arxml_sub_bus):
                        continue

                total_messages_processed += 1

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
                                pack['unit'] = sig_obj.unit or ""
                                pack['comment'] = f"ID: 0x{msg.arbitration_id:X}"
                        total_signals_decoded += 1
                        msg_ids_with_signals.add(msg.arbitration_id)
                except Exception:
                    sig_map = msg_signal_map.get(msg_def.frame_id, {})
                    for sig_name, sig_obj in sig_map.items():
                        try:
                            sig_val = sig_obj.decode(msg.data)
                            if isinstance(sig_val, (int, float)):
                                pack = pool[sig_name]
                                pack['t'].append(rel_t)
                                pack['v'].append(float(sig_val))
                                if not pack['unit']:
                                    pack['unit'] = sig_obj.unit or ""
                                    pack['comment'] = f"ID: 0x{msg.arbitration_id:X}"
                                total_signals_decoded += 1
                                msg_ids_with_signals.add(msg.arbitration_id)
                        except Exception:
                            pass

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
            except:
                pass

            print(f"\n{'=' * 60}")
            print(f"总线 {self.bus_id} 解析统计:")
            print(f"  - 匹配到的消息ID数: {len(matched_msg_ids)}")
            print(f"  - 未匹配的消息ID数: {len(unmatched_msg_ids)}")
            if unmatched_msg_ids:
                print(f"  - 未匹配ID示例: {[hex(id) for id in list(unmatched_msg_ids)[:10]]}")
            print(f"  - 处理的报文总数: {total_messages_processed}")
            print(f"  - 成功解码的信号值: {total_signals_decoded}")
            print(f"  - 包含信号的CAN ID数: {len(msg_ids_with_signals)}")
            print(f"  - 成功注册的信号通道数: {len(pool)}")
            print(f"{'=' * 60}\n")

            stats = {
                'messages_processed': total_messages_processed,
                'signals_decoded': total_signals_decoded,
                'msg_ids_with_signals': len(msg_ids_with_signals),
                'matched_msg_ids': len(matched_msg_ids),
                'unmatched_msg_ids': len(unmatched_msg_ids),
                'total_signals_in_db': len(msg_signal_map),
                'pool': pool
            }

            self.progress_updated.emit(self.bus_id, 100, "解析完成!")
            self.parse_finished.emit(self.bus_id, stats)

        except Exception as e:
            self.parse_error.emit(self.bus_id, str(e))

class MDFPlotter(QWidget):
    def __init__(self):
        super().__init__()
        self.mdf_path = ""
        self.signals = {}
        self.mdf_file = None
        self.plot_widgets = []
        self.master_viewbox = None
        self.map_dialog = None
        self.web_view = None
        self.gps_df = None
        self._temp_map_dir = None
        self.cursor2_pos = None

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
        self.search_index = {}
        self.search_timer = None
        self._search_pending_text = ""
        self._parsing_bus_ids = set()
        self.plot_min_height = 150
        self._user_signal_order = {}
        self._view_mode = 'all'
        self._search_pending_text = ""

        self._list_update_timer = QTimer()
        self._list_update_timer.setSingleShot(True)
        self._list_update_timer.timeout.connect(self._update_signal_list_values)

        self._current_cursor_time = None
        self._mouse_in_plot = False
        self._sig_value_cache = {}

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
                        if lat_idx >= 0: self.lat_combo.setCurrentIndex(lat_idx)
                    if saved_lon:
                        lon_idx = self.lon_combo.findText(saved_lon)
                        if lon_idx >= 0: self.lon_combo.setCurrentIndex(lon_idx)
            except Exception as e:
                print(f"恢复地图配置失败: {e}")

        self.lat_combo.currentIndexChanged.connect(self.save_config)
        self.lon_combo.currentIndexChanged.connect(self.save_config)

    def init_ui(self):
        self.setWindowTitle('VET_DATA_v26.07.01_CAN_MultiBus_UI')
        self.setGeometry(100, 100, 1450, 950)

        main_layout = QHBoxLayout(self)

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

        view_switch_layout = QHBoxLayout()
        self.view_all_btn = QPushButton("全部信号")
        self.view_selected_btn = QPushButton("已选信号 (0)")

        for btn in (self.view_all_btn, self.view_selected_btn):
            btn.setCheckable(True)
            btn.setFixedHeight(28)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #ecf0f1;
                    color: #2c3e50;
                    border: 1px solid #bdc3c7;
                    border-radius: 4px;
                    padding: 4px 12px;
                    font-size: 11px;
                }
                QPushButton:checked {
                    background-color: #3498db;
                    color: white;
                    border: 1px solid #2980b9;
                    font-weight: bold;
                }
                QPushButton:hover:!checked {
                    background-color: #d5dbdb;
                }
            """)

        self.view_all_btn.setChecked(True)
        self.view_all_btn.clicked.connect(lambda: self._on_view_mode_changed('all'))
        self.view_selected_btn.clicked.connect(lambda: self._on_view_mode_changed('selected'))

        view_switch_layout.addWidget(self.view_all_btn)
        view_switch_layout.addWidget(self.view_selected_btn)

        select_buttons_layout = QHBoxLayout()
        self.select_all_button = QPushButton("全选")
        self.deselect_all_button = QPushButton("全部取消")
        select_buttons_layout.addWidget(self.select_all_button)
        select_buttons_layout.addWidget(self.deselect_all_button)

        config_buttons_layout = QHBoxLayout()
        self.save_config_button = QPushButton("保存配置")
        self.load_config_button = QPushButton("加载配置")
        config_buttons_layout.addWidget(self.save_config_button)
        config_buttons_layout.addWidget(self.load_config_button)

        self.math_channel_button = QPushButton("➕ 创建计算通道 (加减乘除)")
        self.math_channel_button.setStyleSheet(
            "QPushButton { background-color: #2b8a3e; color: white; font-weight: bold; }")

        signal_groupbox = QGroupBox("可用信号")
        signal_layout_outer = QVBoxLayout()
        signal_groupbox.setLayout(signal_layout_outer)

        self.hint_label = QLabel()
        self.hint_label.setStyleSheet(
            "color: #2980b9; padding: 5px; font-size: 11px; background-color: #d6eaf8; border-radius: 3px;")
        self.hint_label.setWordWrap(True)
        self.hint_label.hide()
        signal_layout_outer.addWidget(self.hint_label)

        self.stats_label = QLabel("没有可用信号，请加载数据文件")
        self.stats_label.setStyleSheet(
            "color: #95a5a6; padding: 10px; font-size: 12px;")
        self.stats_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stats_label.setWordWrap(True)
        signal_layout_outer.addWidget(self.stats_label)

        self.reset_order_btn = QPushButton("↺ 恢复默认排序")
        self.reset_order_btn.setFixedHeight(24)
        self.reset_order_btn.setStyleSheet("""
            QPushButton {
                background-color: #f39c12;
                color: white;
                border: none;
                border-radius: 3px;
                padding: 2px 8px;
                font-size: 10px;
            }
            QPushButton:hover { background-color: #e67e22; }
        """)
        self.reset_order_btn.setVisible(False)
        self.reset_order_btn.clicked.connect(self._reset_signal_order)
        signal_layout_outer.addWidget(self.reset_order_btn)

        self.signal_list_widget = QListWidget()
        self.signal_list_widget.setUniformItemSizes(True)
        self.signal_list_widget.setSpacing(2)
        self.signal_list_delegate = SignalItemDelegate(self)
        self.signal_list_widget.setItemDelegate(self.signal_list_delegate)
        signal_layout_outer.addWidget(self.signal_list_widget)

        self.signal_list_widget.itemChanged.connect(self._on_item_check_changed)
        self.signal_list_widget.model().rowsMoved.connect(self._on_signal_list_reordered)

        scroll_area_left = QScrollArea()
        scroll_area_left.setWidgetResizable(True)
        scroll_area_left.setWidget(signal_groupbox)
        scroll_area_left.setMinimumHeight(200)
        scroll_area_left.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        bus_groupbox = QGroupBox("CAN总线配置")
        bus_groupbox.setCheckable(True)
        bus_groupbox.setChecked(True)
        bus_groupbox.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
            }
            QGroupBox::indicator {
                width: 0px;
                height: 0px;
            }
        """)

        bus_content_widget = QWidget()
        bus_content_layout = QVBoxLayout(bus_content_widget)
        bus_content_layout.setSpacing(2)
        bus_content_layout.setContentsMargins(0, 0, 0, 0)

        bus_scroll = QScrollArea()
        bus_scroll.setWidgetResizable(True)
        bus_scroll.setMaximumHeight(500)
        bus_scroll.setMinimumHeight(200)
        bus_scroll.setFrameShape(QFrame.Shape.NoFrame)

        self.bus_container = QWidget()
        self.bus_container_layout = QVBoxLayout()
        self.bus_container_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.bus_container_layout.setSpacing(2)
        self.bus_container.setLayout(self.bus_container_layout)
        bus_scroll.setWidget(self.bus_container)
        bus_content_layout.addWidget(bus_scroll)

        bus_btn_layout = QHBoxLayout()
        self.refresh_bus_btn = QPushButton("刷新总线信息")
        self.refresh_bus_btn.clicked.connect(self.refresh_bus_info)
        bus_btn_layout.addWidget(self.refresh_bus_btn)
        bus_btn_layout.addStretch()
        bus_content_layout.addLayout(bus_btn_layout)

        bus_layout = QVBoxLayout(bus_groupbox)
        bus_layout.setContentsMargins(5, 5, 5, 5)
        bus_layout.addWidget(bus_content_widget)

        def toggle_bus(checked):
            bus_content_widget.setVisible(checked)
            if checked:
                bus_scroll.setMinimumHeight(200)
                bus_scroll.setMaximumHeight(500)
            else:
                bus_scroll.setMinimumHeight(0)
                bus_scroll.setMaximumHeight(0)

        bus_groupbox.toggled.connect(toggle_bus)

        map_groupbox = QGroupBox("GPS轨迹图")
        map_groupbox.setCheckable(True)
        map_groupbox.setChecked(True)
        map_groupbox.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
            }
            QGroupBox::indicator {
                width: 0px;
                height: 0px;
            }
        """)

        map_content_widget = QWidget()
        map_content_layout = QVBoxLayout(map_content_widget)
        map_content_layout.setContentsMargins(0, 0, 0, 0)

        lat_layout = QHBoxLayout()
        lat_label = QLabel("纬度信号:")
        self.lat_combo = QComboBox()
        lat_layout.addWidget(lat_label)
        lat_layout.addWidget(self.lat_combo)
        map_content_layout.addLayout(lat_layout)

        lon_layout = QHBoxLayout()
        lon_label = QLabel("经度信号:")
        self.lon_combo = QComboBox()
        lon_layout.addWidget(lon_label)
        lon_layout.addWidget(self.lon_combo)
        map_content_layout.addLayout(lon_layout)

        self.show_map_button = QPushButton("显示GPS轨迹")
        self.show_map_button.setStyleSheet(
            "QPushButton { background-color: #0066cc; color: white; font-weight: bold; }")
        map_content_layout.addWidget(self.show_map_button)

        map_layout = QVBoxLayout(map_groupbox)
        map_layout.setContentsMargins(5, 5, 5, 5)
        map_layout.addWidget(map_content_widget)

        map_groupbox.toggled.connect(map_content_widget.setVisible)

        button_layout = QHBoxLayout()
        self.plot_button = QPushButton("绘制所选信号")
        self.export_button = QPushButton("导出为CSV")
        self.save_data_button = QPushButton("数据截取")
        button_layout.addWidget(self.plot_button)
        button_layout.addWidget(self.export_button)
        button_layout.addWidget(self.save_data_button)

        left_panel.addLayout(file_select_layout)
        left_panel.addWidget(self.search_box)
        left_panel.addLayout(view_switch_layout)
        left_panel.addLayout(select_buttons_layout)
        left_panel.addLayout(config_buttons_layout)
        left_panel.addWidget(self.math_channel_button)
        left_panel.addWidget(scroll_area_left)
        left_panel.addWidget(bus_groupbox)
        left_panel.addWidget(map_groupbox)
        left_panel.addLayout(button_layout)
        left_panel.setStretch(6, 1)

        right_panel = QGroupBox("信号曲线")
        right_panel_layout = QVBoxLayout(right_panel)

        plot_control_layout = QHBoxLayout()
        plot_control_layout.addWidget(QLabel("信号高度:"))
        self.height_spin = QSpinBox()
        self.height_spin.setRange(80, 900)
        self.height_spin.setSingleStep(10)
        self.height_spin.setValue(150)
        self.height_spin.setSuffix(" px")
        self.height_spin.valueChanged.connect(self._on_plot_height_changed)
        plot_control_layout.addWidget(self.height_spin)
        plot_control_layout.addStretch()
        right_panel_layout.addLayout(plot_control_layout)

        self.plot_container = QWidget()
        self.plot_layout = QVBoxLayout(self.plot_container)
        self.plot_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll_area_right = QScrollArea()
        scroll_area_right.setWidgetResizable(True)
        scroll_area_right.setWidget(self.plot_container)
        right_panel_layout.addWidget(scroll_area_right)

        main_layout.addLayout(left_panel, 1)
        main_layout.addWidget(right_panel, 3)

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
        self.signal_list_widget.clear()
        self.hint_label.hide()

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
            self._user_signal_order.clear()
            if hasattr(self, 'reset_order_btn'):
                self.reset_order_btn.setVisible(False)
            self._sig_value_cache.clear()
            if hasattr(self, '_list_update_timer') and self._list_update_timer.isActive():
                self._list_update_timer.stop()
            self._current_cursor_time = None
            self._mouse_in_plot = False

            self.mdf_path = file_name
            self.current_file_label.setText(f"当前文件: {os.path.basename(file_name)}")

            self.clear_bus_config()
            BusConfigWidget.clear_arxml_cache()

            for bus_id, worker in self.parse_workers.items():
                if worker.isRunning():
                    worker.stop()
                    worker.wait()
            self.parse_workers.clear()

            BusConfigWidget.clear_arxml_cache()

            file_extension = os.path.splitext(file_name)[1].lower()
            if file_extension in ('.blf', '.asc', '.trc'):
                self.load_can_bus_info(file_name)
                self.create_bus_config_ui()
            else:
                self.load_signals(with_dbc=False)

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

            for msg in reader:
                if t0 is None:
                    t0 = msg.timestamp

                total_count += 1
                bus_id = getattr(msg, 'channel', 0)
                rel_t = msg.timestamp - t0 if t0 else 0

                is_fd = getattr(msg, 'is_fd', False) or getattr(msg, 'is_can_fd', False)

                if bus_id not in bus_stats:
                    bus_stats[bus_id] = {'count': 0, 'first_timestamp': msg.timestamp, 'is_fd': is_fd}
                bus_stats[bus_id]['count'] += 1
                if is_fd:
                    bus_stats[bus_id]['is_fd'] = True

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
            except:
                pass

            if total_count == 0:
                raise Exception("未读取到任何CAN报文")

            for bus_id, samples in bus_samples.items():
                actual_id_count = len(bus_id_sets.get(bus_id, set()))

                self.can_bus_data[bus_id] = {
                    'timestamps': samples['timestamps'],
                    'ids': list(samples['ids']),
                    'actual_ids': list(bus_id_sets.get(bus_id, set())),
                    'data': samples['data'],
                    'is_fd': samples['is_fd'],
                    'dlc': samples['dlc'],
                    'msg_count': bus_stats[bus_id]['count'],
                    'id_count': actual_id_count,
                    'name': f"Bus {bus_id}",
                    'has_fd': bus_stats[bus_id].get('is_fd', False)
                }

            self.bus_loaded = True

        except Exception as e:
            raise Exception(f"读取CAN总线信息失败: {e}")

    def create_bus_config_ui(self):
        self.clear_bus_config()

        if not self.can_bus_data:
            label = QLabel("未检测到CAN总线数据")
            label.setStyleSheet("color: #95a5a6; padding: 10px;")
            self.bus_container_layout.addWidget(label)
            return

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
        if '||' in protocol_info:
            protocol_path, sub_bus = protocol_info.split('||', 1)
            self.bus_protocols[bus_id] = protocol_path
            if bus_id not in self.db_per_bus:
                self.db_per_bus[bus_id] = {}
            self.db_per_bus[bus_id]['arxml_sub_bus'] = sub_bus
        else:
            self.bus_protocols[bus_id] = protocol_info
            if bus_id not in self.db_per_bus:
                self.db_per_bus[bus_id] = {}
            self.db_per_bus[bus_id]['arxml_sub_bus'] = ''

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

        try:
            protocol_path = self.bus_protocols[bus_id]
            selected_sub_bus = self.db_per_bus.get(bus_id, {}).get('arxml_sub_bus', '')

            keys_to_remove = [
                k for k, info in self.signals.items()
                if info.get('bus_id') == bus_id and k.startswith('CAN_')
            ]
            for key in keys_to_remove:
                self.signals.pop(key, None)
                self.can_parsed_data.pop(key, None)
                self.bus_signal_map.pop(key, None)

            ext = os.path.splitext(protocol_path)[1].lower()

            if ext == '.arxml':
                print(f"\n{'=' * 60}")
                print(f"加载 ARXML 文件 (支持PDU容器): {os.path.basename(protocol_path)}")
                print(f"选择的子总线/域控: {selected_sub_bus or '(全部)'}")
                print(f"{'=' * 60}")

                temp_widget = BusConfigWidget(bus_id, self)
                db, actual_bus_name = temp_widget._convert_arxml_with_pdu_support(
                    protocol_path, selected_sub_bus
                )
                selected_sub_bus = actual_bus_name

            else:
                print(f"\n加载 DBC 文件: {os.path.basename(protocol_path)}")
                db = cantools.database.load_file(protocol_path)
                print(f"✅ DBC加载成功: {len(db.messages)} 个消息")

            if db is None:
                raise Exception("协议文件加载失败")

            self.db_per_bus[bus_id] = self.db_per_bus.get(bus_id, {})
            self.db_per_bus[bus_id]['db'] = db
            self.db_per_bus[bus_id]['arxml_sub_bus'] = selected_sub_bus
            self.db_per_bus[bus_id]['parsed'] = False
            self.db_per_bus[bus_id]['is_arxml'] = (ext == '.arxml')

            worker = ParseWorker(bus_id, self.mdf_path, db, selected_sub_bus)
            worker.parse_finished.connect(self.on_parse_finished)
            worker.parse_error.connect(self.on_parse_error)
            self.parse_workers[bus_id] = worker
            worker.start()
            print(f"🚀 总线 {bus_id} 解析任务已启动")

        except Exception as e:
            self._parsing_bus_ids.discard(bus_id)
            if bus_id in self.bus_config_widgets:
                self.bus_config_widgets[bus_id].set_status('error')
            if not auto_parse:
                QMessageBox.warning(self, "解析失败", f"解析 Bus {bus_id} 时出错: {e}")
            import traceback
            traceback.print_exc()

    def start_parse_with_progress(self, bus_id):
        self.parse_bus(bus_id, auto_parse=True)

    def on_parse_progress(self, bus_id, progress, status_text):
        pass

    def on_parse_finished(self, bus_id, stats):
        self._parsing_bus_ids.discard(bus_id)
        pool = stats.pop('pool', {})

        if not pool:
            print(f"⚠️ 总线 {bus_id} 解析完成，但没有提取到任何信号数据")
            if bus_id in self.bus_config_widgets:
                self.bus_config_widgets[bus_id].set_status('error')
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

        self.refresh_signal_list_ui()

        if bus_id in self.parse_workers:
            del self.parse_workers[bus_id]

        print(f"✅ Bus {bus_id} 解析完成: {stats}")

    def on_parse_error(self, bus_id, error_msg):
        self._parsing_bus_ids.discard(bus_id)
        if bus_id in self.bus_config_widgets:
            self.bus_config_widgets[bus_id].set_status('error')

        if bus_id in self.parse_workers:
            del self.parse_workers[bus_id]

        print(f"❌ Bus {bus_id} 解析失败: {error_msg}")

    def refresh_bus_info(self):
        if not self.mdf_path or not os.path.exists(self.mdf_path):
            return

        file_extension = os.path.splitext(self.mdf_path)[1].lower()
        if file_extension not in ('.blf', '.asc', '.trc'):
            return

        self.load_can_bus_info(self.mdf_path)
        self.create_bus_config_ui()

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
            except:
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

    def load_signals(self, with_dbc=True):
        if not self.mdf_path or not os.path.exists(self.mdf_path):
            self.clear_signal_list()
            self.stats_label.setText(f"错误: 找不到文件 {self.mdf_path}")
            self.stats_label.setStyleSheet("color: #e74c3c; padding: 10px;")
            self.set_buttons_enabled(False)
            return

        self.clear_signal_list()

        self.signals.clear()
        self._user_signal_order.clear()
        if hasattr(self, 'reset_order_btn'):
            self.reset_order_btn.setVisible(False)
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
                self.stats_label.setText(f"不支持的文件类型: {file_extension}")
                self.stats_label.setStyleSheet("color: #e74c3c; padding: 10px;")
                self.set_buttons_enabled(False)
                return

            if not self.signals:
                self.stats_label.setText("文件中没有找到有效信号。")
                self.stats_label.setStyleSheet("color: #95a5a6; padding: 10px;")
                self.set_buttons_enabled(False)
                return

            if file_extension in ('.blf', '.asc', '.trc') and not self.can_parsed_data:
                self.hint_label.setText("💡 提示: 请在下方CAN总线配置中加载协议文件并点击\"解析\"按钮")
                self.hint_label.show()

            self.refresh_signal_list_ui()

        except Exception as e:
            self.clear_signal_list()
            self.stats_label.setText(f"加载文件时出错: {str(e)}")
            self.stats_label.setStyleSheet("color: #e74c3c; padding: 10px;")
            self.set_buttons_enabled(False)
            import traceback
            traceback.print_exc()

    def refresh_signal_list_ui(self):
        checked_keys = set()
        for i in range(self.signal_list_widget.count()):
            item = self.signal_list_widget.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                checked_keys.add(item.data(Qt.ItemDataRole.UserRole))

        self._user_signal_order = {
            k: v for k, v in self._user_signal_order.items() if k in self.signals
        }

        saved_lat_data = self.lat_combo.currentData()
        saved_lon_data = self.lon_combo.currentData()

        self.signal_list_widget.setUpdatesEnabled(False)
        self.signal_list_widget.blockSignals(True)
        self.lat_combo.blockSignals(True)
        self.lon_combo.blockSignals(True)

        self.signal_list_widget.clear()

        if not self.signals:
            self.stats_label.setText("没有可用信号，请加载数据文件")
            self.stats_label.setStyleSheet(
                "color: #95a5a6; padding: 10px; font-size: 12px;")
            self.stats_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.hint_label.hide()

            self.lat_combo.clear()
            self.lon_combo.clear()

            self.lat_combo.blockSignals(False)
            self.lon_combo.blockSignals(False)
            self.signal_list_widget.blockSignals(False)
            self.signal_list_widget.setUpdatesEnabled(True)
            self.set_buttons_enabled(False)
            return

        total_signals = len(self.signals)
        can_signals = sum(1 for k in self.signals if k.startswith('CAN_'))
        math_signals = sum(1 for k in self.signals if k.startswith('MATH_'))
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

        self.stats_label.setText(stats_text)
        self.stats_label.setStyleSheet(
            "color: #2c3e50; font-size: 10px; padding: 4px 6px; "
            "background-color: #ecf0f1; border-radius: 3px;")
        self.stats_label.setAlignment(Qt.AlignmentFlag.AlignLeft)

        def sort_key(k):
            if k in self._user_signal_order:
                return (0, self._user_signal_order[k])
            info = self.signals.get(k, {})
            if k.startswith('MATH_'):
                priority = 1
            elif k.startswith('CAN_'):
                priority = 2
            else:
                priority = 3
            display_name = info.get('display_name', k)
            return (priority, display_name)

        sorted_keys = sorted(self.signals.keys(), key=sort_key)

        for unique_key in sorted_keys:
            info = self.signals[unique_key]
            display_text = info.get('display_name', unique_key)

            bus_id = info.get('bus_id')
            if bus_id is not None and '[🚌' not in display_text:
                bus_name = self.can_bus_data.get(bus_id, {}).get('name', f"Bus {bus_id}")
                display_text = f"{display_text} [🚌 {bus_name}]"

            item = QListWidgetItem()
            item.setText("")
            item.setData(Qt.ItemDataRole.UserRole, unique_key)
            item.setData(Qt.ItemDataRole.UserRole + 1, info.get('comment', ''))
            item.setData(Qt.ItemDataRole.UserRole + 2, display_text)
            item.setToolTip(info.get('comment', ''))

            if unique_key in checked_keys:
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setCheckState(Qt.CheckState.Unchecked)

            self.signal_list_widget.addItem(item)

        self.lat_combo.clear()
        self.lon_combo.clear()
        existing_lat = set()
        existing_lon = set()

        for unique_key in sorted_keys:
            info = self.signals[unique_key]
            display_text = info.get('display_name', unique_key)

            if unique_key not in existing_lat:
                self.lat_combo.addItem(display_text, unique_key)
                existing_lat.add(unique_key)
            if unique_key not in existing_lon:
                self.lon_combo.addItem(display_text, unique_key)
                existing_lon.add(unique_key)

        if saved_lat_data:
            idx = self.lat_combo.findData(saved_lat_data)
            if idx >= 0:
                self.lat_combo.setCurrentIndex(idx)
        if saved_lon_data:
            idx = self.lon_combo.findData(saved_lon_data)
            if idx >= 0:
                self.lon_combo.setCurrentIndex(idx)

        self.lon_combo.blockSignals(False)
        self.lat_combo.blockSignals(False)
        self.signal_list_widget.blockSignals(False)
        if self._view_mode == 'selected':
            self.signal_list_widget.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        else:
            self.signal_list_widget.setDragDropMode(QListWidget.DragDropMode.NoDragDrop)
        self.signal_list_widget.setUpdatesEnabled(True)

        self._build_search_index()
        self._update_selected_count()
        self._update_list_visibility()
        self.reset_order_btn.setVisible(
            self._view_mode == 'selected' and bool(self._user_signal_order)
        )
        self.set_buttons_enabled(True)

    def _on_view_mode_changed(self, mode):
        if mode != 'selected':
            self._restore_signal_list_names()
        self._view_mode = mode
        self.view_all_btn.setChecked(mode == 'all')
        self.view_selected_btn.setChecked(mode == 'selected')
        self.reset_order_btn.setVisible(
            mode == 'selected' and bool(self._user_signal_order)
        )
        if mode == 'selected':
            self.signal_list_widget.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        else:
            self.signal_list_widget.setDragDropMode(QListWidget.DragDropMode.NoDragDrop)
        self._update_list_visibility()

    def _update_selected_count(self):
        count = sum(
            1 for i in range(self.signal_list_widget.count())
            if self.signal_list_widget.item(i).checkState() == Qt.CheckState.Checked
        )
        self.view_selected_btn.setText(f"已选信号 ({count})")

    def _update_list_visibility(self):
        text = self._search_pending_text
        show_selected_only = (self._view_mode == 'selected')

        for i in range(self.signal_list_widget.count()):
            item = self.signal_list_widget.item(i)
            key = item.data(Qt.ItemDataRole.UserRole)
            info = self.search_index.get(key, {})

            search_match = not text or text in info.get('search_text', '')

            if show_selected_only:
                view_match = item.checkState() == Qt.CheckState.Checked
            else:
                view_match = True

            self.signal_list_widget.setRowHidden(i, not (search_match and view_match))

    def _on_item_check_changed(self, item):
        self._update_selected_count()
        if self._view_mode == 'selected' and item.checkState() == Qt.CheckState.Unchecked:
            row = self.signal_list_widget.row(item)
            self.signal_list_widget.setRowHidden(row, True)

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
        self.signal_list_widget.setUpdatesEnabled(False)
        self._update_list_visibility()
        if self._view_mode == 'selected':
            self.signal_list_widget.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        else:
            self.signal_list_widget.setDragDropMode(QListWidget.DragDropMode.NoDragDrop)
        self.signal_list_widget.setUpdatesEnabled(True)

    def _build_search_index(self):
        self.search_index.clear()
        for unique_key, info in self.signals.items():
            search_text = f"{info.get('name', '')} {info.get('display_name', '')} {info.get('comment', '')}".lower()
            self.search_index[unique_key] = {
                'search_text': search_text
            }

    def get_selected_signals(self):
        selected_signals = []
        for i in range(self.signal_list_widget.count()):
            item = self.signal_list_widget.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                selected_signals.append(item.data(Qt.ItemDataRole.UserRole))
        return selected_signals

    def select_all_signals(self):
        self.signal_list_widget.blockSignals(True)
        for i in range(self.signal_list_widget.count()):
            self.signal_list_widget.item(i).setCheckState(Qt.CheckState.Checked)
        self.signal_list_widget.blockSignals(False)
        self._update_selected_count()
        self._update_list_visibility()

    def deselect_all_signals(self):
        self.signal_list_widget.blockSignals(True)
        for i in range(self.signal_list_widget.count()):
            self.signal_list_widget.item(i).setCheckState(Qt.CheckState.Unchecked)
        self.signal_list_widget.blockSignals(False)
        self._update_selected_count()
        self._update_list_visibility()

    def _get_signal(self, signal_info):
        unique_key = signal_info['name']

        if unique_key in self.custom_math_data:
            math_item = self.custom_math_data[unique_key]
            return Signal(samples=math_item['samples'],
                          timestamps=math_item['timestamps'],
                          name=unique_key,
                          unit="Math")

        if unique_key in self.can_parsed_data:
            can_item = self.can_parsed_data[unique_key]
            return Signal(samples=can_item['samples'],
                          timestamps=can_item['timestamps'],
                          name=unique_key,
                          unit=can_item.get('unit', ''))

        file_extension = os.path.splitext(self.mdf_path)[1].lower()
        signal = None

        if file_extension in ('.mdf', '.mf4'):
            try:
                signal = self.mdf_file.get(signal_info['name'], group=signal_info['group'],
                                           index=signal_info['channel'])
            except MdfException as e:
                print(f"MDF/MF4 获取信号失败: {e}")
                return None
        elif file_extension in ('.csv', '.vbo'):
            if isinstance(self.mdf_file, pd.DataFrame) and signal_info['name'] in self.mdf_file.columns:
                signal_data = self.mdf_file[signal_info['name']]
                timestamps = self.mdf_file.index.values
                if len(timestamps) == 0 or len(signal_data) == 0:
                    return None
                signal = Signal(samples=signal_data.values,
                                timestamps=timestamps.astype(float),
                                name=signal_info['name'],
                                unit="")

        if signal is not None and len(signal.samples) > 0:
            if signal.samples.dtype.kind in ('U', 'S', 'O'):
                str_samples = signal.samples.astype(str)
                unique_vals = sorted(list(set(str_samples)))
                val_map = {val: i for i, val in enumerate(unique_vals)}
                numeric_samples = np.array([val_map[s] for s in str_samples], dtype=float)

                new_sig = Signal(samples=numeric_samples, timestamps=signal.timestamps,
                                 name=signal.name, unit=signal.unit)
                new_sig.text_mapping = unique_vals
                return new_sig
            else:
                signal.samples = signal.samples.astype(float)

        return signal

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
            if not sig_info: continue

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
                vb.setMouseEnabled(x=True, y=True)
                vb.setLimits(xMin=t_min, xMax=t_max)

                left_axis = plot_widget.getPlotItem().getAxis('left')
                left_axis.setWidth(30)

                if hasattr(signal, 'text_mapping'):
                    plot_widget.plot(signal.timestamps, signal.samples, pen=pg.mkPen('c', width=2), stepMode='center')
                    ay = plot_widget.getPlotItem().getAxis('left')
                    ticks = [(j, label) for j, label in enumerate(signal.text_mapping)]
                    ay.setTicks([ticks])
                    plot_widget.setYRange(-0.5, len(signal.text_mapping) - 0.5, padding=0.1)
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
                        if y_padding == 0: y_padding = 1.0
                    else:
                        y_padding = y_range * 0.4

                    plot_widget.setYRange(y_min_data - y_padding, y_max_data + y_padding, padding=0)
                    plot_widget.plot(signal.timestamps, signal.samples, pen='y')
                    plot_widget.sigXRangeChanged.connect(self.on_x_range_changed)

                spot_item = pg.ScatterPlotItem(size=8, pen=pg.mkPen(None), brush=pg.mkBrush('r'))
                plot_widget.addItem(spot_item)

                vLine = pg.InfiniteLine(angle=90, movable=False, pen='g')
                plot_widget.addItem(vLine, ignoreBounds=True)

                text_item = pg.TextItem(text="", color=(255, 255, 0), anchor=(0, 1))
                plot_widget.addItem(text_item, ignoreBounds=True)

                plot_widget.vLine = vLine
                plot_widget.text_item = text_item
                plot_widget.spot_item = spot_item
                plot_widget.signal_data = signal

                plot_widget.setMinimumHeight(self.plot_min_height)

                title = f"信号: {signal_info['display_name']}"
                bus_id = signal_info.get('bus_id')
                if bus_id is not None:
                    title += f" [Bus {bus_id}]"
                plot_widget.setTitle(title)

                plot_widget.setLabel('bottom', '时间', units='s')
                plot_widget.showGrid(x=True, y=True)

                if i == 0:
                    self.master_viewbox = vb
                    self.master_viewbox.setXRange(t_min, t_max)
                else:
                    vb.setXLink(self.master_viewbox)

                plot_widget.scene().sigMouseMoved.connect(self.update_cursor_positions)

            except Exception as e:
                print(f"警告: 无法绘制信号 '{signal_info.get('display_name')} '。错误: {e}")

        self.plot_layout.addStretch(1)

        if self.map_dialog and self.map_dialog.isVisible():
            QTimer.singleShot(300, self.update_map_marker)

    def on_x_range_changed(self):
        if self.rescale_timer is not None:
            self.rescale_timer.start(150)

    def execute_auto_scale(self):
        for pw in self.plot_widgets:
            if not hasattr(pw, 'signal_data') or hasattr(pw.signal_data, 'text_mapping'):
                continue

            sig = pw.signal_data
            view_range = pw.getViewBox().viewRange()[0]
            idx_start = np.searchsorted(sig.timestamps, view_range[0])
            idx_end = np.searchsorted(sig.timestamps, view_range[1])
            visible_data = sig.samples[idx_start:idx_end]

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

        view_range = self.master_viewbox.viewRange()
        x_min, x_max = view_range[0]
        y_min, y_max = view_range[1]
        if not (x_min <= x_pos <= x_max and y_min <= mouse_point_master.y() <= y_max):
            self._mouse_in_plot = False
            self._restore_signal_list_names()
            return
        else:
            self._mouse_in_plot = True

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
                plot_widget.spot_item.setData([x_val], [y_val])
                display_y_val = f"{y_val:.3f}"

            if hasattr(signal, 'text_mapping') and not np.isnan(y_val):
                label_idx = int(round(y_val))
                if 0 <= label_idx < len(signal.text_mapping):
                    display_y_val = signal.text_mapping[label_idx]

            view_range = plot_widget.getPlotItem().getViewBox().viewRange()
            y_min, y_max = view_range[1]
            text_y_pos = y_max - (y_max - y_min) * 0.2

            x_min, x_max = view_range[0]
            x_range = x_max - x_min
            if x_range > 0 and (x_max - x_val) < 0.30 * x_range:
                plot_widget.text_item.setAnchor((1, 1))
            else:
                plot_widget.text_item.setAnchor((0, 1))

            plot_widget.text_item.setPos(x_val, text_y_pos)
            plot_widget.text_item.setText(f"t={x_val:.3f} s, val={display_y_val}")

        if self._view_mode == 'selected':
            self._current_cursor_time = x_pos
            if self._list_update_timer.isActive():
                self._list_update_timer.stop()
            self._list_update_timer.start(50)

        self.update_map_marker(evt)

    def _update_signal_list_values(self):
        try:
            if self._current_cursor_time is None or not self._mouse_in_plot:
                return

            x_pos = self._current_cursor_time

            self.signal_list_widget.setUpdatesEnabled(False)

            updated_count = 0
            for i in range(self.signal_list_widget.count()):
                item = self.signal_list_widget.item(i)
                if item.checkState() != Qt.CheckState.Checked:
                    continue
                if self.signal_list_widget.isRowHidden(i):
                    continue

                key = item.data(Qt.ItemDataRole.UserRole)
                original_name = item.data(Qt.ItemDataRole.UserRole + 2)
                if not original_name:
                    continue

                if key not in self._sig_value_cache:
                    sig_info = self.signals.get(key)
                    if not sig_info:
                        continue
                    sig = self._get_signal(sig_info)
                    if sig is None or len(sig.timestamps) == 0:
                        continue
                    timestamps = sig.timestamps
                    samples = sig.samples
                    self._sig_value_cache[key] = (timestamps, samples)
                else:
                    timestamps, samples = self._sig_value_cache[key]

                idx = np.searchsorted(timestamps, x_pos)
                idx = np.clip(idx, 0, len(timestamps) - 1)
                val = samples[idx]

                if np.isnan(val):
                    val_str = "NaN"
                else:
                    val_str = f"{val:.3f}"

                item.setData(Qt.ItemDataRole.UserRole + 3, val_str)
                updated_count += 1

            self.signal_list_widget.setUpdatesEnabled(True)
            self.signal_list_widget.viewport().update()
        except Exception as e:
            print(f"❌ 更新信号列表数值时出错: {e}")
            import traceback
            traceback.print_exc()

    def _restore_signal_list_names(self):
        self.signal_list_widget.setUpdatesEnabled(False)
        for i in range(self.signal_list_widget.count()):
            item = self.signal_list_widget.item(i)
            item.setData(Qt.ItemDataRole.UserRole + 3, None)
        self.signal_list_widget.setUpdatesEnabled(True)
        self._sig_value_cache.clear()
        self.signal_list_widget.viewport().update()

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
            if ok: QTimer.singleShot(500, self.update_map_marker)

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
            if not file_name: return

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
            if not file_name: return

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
                                    writer.add_message(msg)
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
        if not file_name: return

        try:
            all_timestamps = []
            for unique_key in selected_signals:
                signal_info = self.signals.get(unique_key)
                if not signal_info: continue

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
            if not ok or target_frequency <= 0: return

            raster = np.arange(t_start, t_end + 1 / target_frequency, 1 / target_frequency)
            data = {}
            for unique_key in selected_signals:
                signal_info = self.signals.get(unique_key)
                if not signal_info: continue

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

            if not data: return

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
            if config is None:
                return

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

                for i in range(self.signal_list_widget.count()):
                    item = self.signal_list_widget.item(i)
                    if item.data(Qt.ItemDataRole.UserRole) == unique_math_key:
                        item.setCheckState(Qt.CheckState.Checked)
                        break

                self._update_selected_count()
                self._update_list_visibility()

                QMessageBox.information(self, "计算通道", f"数学计算通道 '{new_name}' 创建成功！")

            except Exception as ex:
                QMessageBox.critical(self, "计算失败", f"数学通道运算出错: {ex}")

    def save_signal_config(self):
        selected_keys = self.get_selected_signals()
        if not selected_keys:
            QMessageBox.warning(self, "保存", "未选择任何信号。")
            return

        file_path, _ = QFileDialog.getSaveFileName(self, "保存配置", "", "JSON Files (*.json)")
        if not file_path: return

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
                'arxml_sub_bus': self.db_per_bus.get(bus_id, {}).get('arxml_sub_bus', '')
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
        if not file_name: return

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
                        self.db_per_bus[bus_id]['arxml_sub_bus'] = config.get('arxml_sub_bus', '')

                        if bus_id in self.bus_config_widgets:
                            self.bus_config_widgets[bus_id].name_edit.setText(self.can_bus_data[bus_id]['name'])
                            if protocol:
                                if config.get('arxml_sub_bus'):
                                    self.bus_config_widgets[bus_id].protocol_label.setText(
                                        f"{os.path.basename(protocol)} [{config.get('arxml_sub_bus')}]"
                                    )
                                else:
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
            for i in range(self.signal_list_widget.count()):
                item = self.signal_list_widget.item(i)
                unique_key = item.data(Qt.ItemDataRole.UserRole)
                item.setCheckState(
                    Qt.CheckState.Checked if unique_key in selected_keys else Qt.CheckState.Unchecked)
            self._update_selected_count()
            self._update_list_visibility()

            lat_key = config_data.get("gps_lat_key")
            lon_key = config_data.get("gps_lon_key")
            if lat_key:
                idx = self.lat_combo.findData(lat_key)
                if idx >= 0: self.lat_combo.setCurrentIndex(idx)
            if lon_key:
                idx = self.lon_combo.findData(lon_key)
                if idx >= 0: self.lon_combo.setCurrentIndex(idx)

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
        if hasattr(self, 'mdf_file') and self.mdf_file is not None:
            if isinstance(self.mdf_file, asammdf.MDF):
                try:
                    self.mdf_file.close()
                except:
                    pass
        event.accept()

    def _on_signal_list_reordered(self, parent, start, end, destination, row):
        if self._view_mode != 'selected':
            return

        new_order = {}
        for i in range(self.signal_list_widget.count()):
            item = self.signal_list_widget.item(i)
            key = item.data(Qt.ItemDataRole.UserRole)
            new_order[key] = i

        self._user_signal_order.update(new_order)
        self.reset_order_btn.setVisible(True)

    def _reset_signal_order(self):
        self._user_signal_order.clear()
        self.refresh_signal_list_ui()

    def _on_plot_height_changed(self, value):
        self.plot_min_height = value
        if self.plot_widgets:
            self.plot_selected_signals()


if __name__ == '__main__':
    pg.setConfigOptions(antialias=True)
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--enable-local-file-accesses --disable-web-security"

    app = QApplication(sys.argv)
    ex = MDFPlotter()
    ex.show()
    sys.exit(app.exec())