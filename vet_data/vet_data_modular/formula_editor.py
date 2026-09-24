"""Formula editor dialog with atomic, token-backed signal references."""

import json
from typing import Callable, Mapping

import numpy as np
from PyQt6.QtCore import QMimeData, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QKeyEvent, QKeySequence, QTextCharFormat, QTextCursor, QTextFormat
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QTextEdit,
    QVBoxLayout,
)

from .calculated_signal import (
    CalculationStatus, CalculatedSignalDefinition, CalculatedSignalResult,
)
from .formula_parser import FormulaError
from .formula_validator import ALLOWED_FUNCTIONS, parse_and_validate_formula


SIGNAL_KEY_ROLE = Qt.ItemDataRole.UserRole
SIGNAL_TOKEN_PROPERTY = int(QTextFormat.Property.UserProperty) + 1
SIGNAL_KEY_PROPERTY = int(QTextFormat.Property.UserProperty) + 2
SIGNAL_ATOM_PROPERTY = int(QTextFormat.Property.UserProperty) + 3
FUNCTION_NAMES = (
    "sqrt", "abs", "sin", "cos", "tan", "sind", "cosd", "tand",
    "log", "log10", "exp", "derivative", "integral",
)


class AtomicFormulaEdit(QTextEdit):
    """QTextEdit whose formatted signal ranges behave as indivisible atoms."""

    atomsChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._next_atom_id = 1
        self._known_bindings = set()
        self._adjusting_cursor = False
        self._pending_delete = None
        self.textChanged.connect(self.atomsChanged)
        self.cursorPositionChanged.connect(self._keep_cursor_out_of_atom)

    def insert_signal(self, label: str, token: str, key: str):
        atom_id = self._next_atom_id
        self._next_atom_id += 1
        self._known_bindings.add((token, key))
        char_format = QTextCharFormat()
        char_format.setProperty(SIGNAL_TOKEN_PROPERTY, token)
        char_format.setProperty(SIGNAL_KEY_PROPERTY, key)
        char_format.setProperty(SIGNAL_ATOM_PROPERTY, atom_id)
        char_format.setBackground(QColor("#d9ecff"))
        char_format.setForeground(QColor("#0b4f87"))
        char_format.setFontWeight(600)
        cursor = self._prepare_insertion_cursor()
        cursor.insertText(label, char_format)
        cursor.setCharFormat(QTextCharFormat())
        self.setTextCursor(cursor)
        self.setFocus()
        self.atomsChanged.emit()

    def insert_normal_text(self, text: str, cursor_back: int = 0):
        cursor = self._prepare_insertion_cursor()
        cursor.insertText(text)
        if cursor_back:
            cursor.movePosition(QTextCursor.MoveOperation.Left, n=cursor_back)
        self.setTextCursor(cursor)
        self.setFocus()

    def createMimeDataFromSelection(self):
        base = super().createMimeDataFromSelection()
        mime = QMimeData()
        for mime_type in base.formats():
            mime.setData(mime_type, base.data(mime_type))
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return mime
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        text = self.toPlainText()
        segments = []
        position = start
        for atom_start, atom_end, token, key, _atom_id in self.atom_ranges():
            if atom_end <= start or atom_start >= end:
                continue
            if atom_start > position:
                segments.append({"text": text[position:atom_start]})
            segments.append({
                "text": text[atom_start:atom_end], "token": token, "key": key,
            })
            position = atom_end
        if position < end:
            segments.append({"text": text[position:end]})
        mime.setData(
            "application/x-vet-data-formula-fragment",
            json.dumps(segments, ensure_ascii=False).encode("utf-8"),
        )
        return mime

    def copy(self):
        if self.textCursor().hasSelection():
            QApplication.clipboard().setMimeData(self.createMimeDataFromSelection())

    def paste(self):
        self.insertFromMimeData(QApplication.clipboard().mimeData())

    def insertFromMimeData(self, source: QMimeData):
        mime_type = "application/x-vet-data-formula-fragment"
        if not source.hasFormat(mime_type):
            super().insertFromMimeData(source)
            return
        try:
            segments = json.loads(bytes(source.data(mime_type)).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            super().insertFromMimeData(source)
            return
        edit_cursor = self.textCursor()
        edit_cursor.beginEditBlock()
        try:
            for segment in segments:
                binding = (segment.get("token"), segment.get("key"))
                if None not in binding and binding in self._known_bindings:
                    self.insert_signal(segment["text"], *binding)
                else:
                    self.insert_normal_text(str(segment.get("text", "")))
        finally:
            edit_cursor.endEditBlock()
        self.atomsChanged.emit()

    def atom_ranges(self):
        text = self.toPlainText()
        ranges = []
        position = 0
        while position < len(text):
            char_format = self._format_at(position)
            atom_id = char_format.property(SIGNAL_ATOM_PROPERTY)
            if atom_id is None:
                position += 1
                continue
            token = str(char_format.property(SIGNAL_TOKEN_PROPERTY))
            key = str(char_format.property(SIGNAL_KEY_PROPERTY))
            end = position + 1
            while end < len(text):
                next_format = self._format_at(end)
                if next_format.property(SIGNAL_ATOM_PROPERTY) != atom_id:
                    break
                end += 1
            ranges.append((position, end, token, key, atom_id))
            position = end
        return ranges

    def token_formula(self) -> str:
        text = self.toPlainText()
        atoms = self.atom_ranges()
        parts = []
        position = 0
        for start, end, token, _key, _atom_id in atoms:
            parts.append(text[position:start])
            parts.append(token)
            position = end
        parts.append(text[position:])
        return "".join(parts)

    def referenced_keys(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item[3] for item in self.atom_ranges()))

    def refresh_atom_labels(self, label_for_key):
        ranges = self.atom_ranges()
        if not ranges:
            return
        cursor_position = self.textCursor().position()
        self.blockSignals(True)
        try:
            for start, end, token, key, atom_id in reversed(ranges):
                label = label_for_key(key)
                if self.toPlainText()[start:end] == label:
                    continue
                cursor = QTextCursor(self.document())
                cursor.setPosition(start)
                cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                char_format = QTextCharFormat(cursor.charFormat())
                char_format.setProperty(SIGNAL_TOKEN_PROPERTY, token)
                char_format.setProperty(SIGNAL_KEY_PROPERTY, key)
                char_format.setProperty(SIGNAL_ATOM_PROPERTY, atom_id)
                cursor.insertText(label, char_format)
                cursor_position += len(label) - (end - start) if cursor_position >= end else 0
        finally:
            self.blockSignals(False)
        cursor = self.textCursor()
        cursor.setPosition(max(0, min(cursor_position, len(self.toPlainText()))))
        self.setTextCursor(cursor)

    def select_atom_for_position(self, position: int) -> bool:
        atom = self._atom_containing(position)
        if atom is None:
            return False
        self._select_atom(atom)
        return True

    def keyPressEvent(self, event: QKeyEvent):
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy()
            return
        if event.matches(QKeySequence.StandardKey.Paste):
            self._clear_pending_delete()
            self.paste()
            return
        if event.matches(QKeySequence.StandardKey.Cut):
            self.copy()
            cursor = self.textCursor()
            self._expand_selection_to_atoms(cursor)
            cursor.removeSelectedText()
            self.setTextCursor(cursor)
            self._clear_pending_delete()
            self._reset_typing_format()
            self.atomsChanged.emit()
            return

        key = event.key()
        cursor = self.textCursor()
        no_modifier = event.modifiers() == Qt.KeyboardModifier.NoModifier
        if no_modifier and key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            if cursor.hasSelection():
                position = (
                    cursor.selectionStart() if key == Qt.Key.Key_Left
                    else cursor.selectionEnd()
                )
                cursor.setPosition(position)
                self.setTextCursor(cursor)
                self._clear_pending_delete()
                self._reset_typing_format()
                return
            position = cursor.position()
            atom = (
                self._atom_ending_at(position) if key == Qt.Key.Key_Left
                else self._atom_starting_at(position)
            )
            if atom is not None:
                cursor.setPosition(atom[0] if key == Qt.Key.Key_Left else atom[1])
                self.setTextCursor(cursor)
                self._clear_pending_delete()
                self._reset_typing_format()
                return
            self._clear_pending_delete()
            super().keyPressEvent(event)
            self._reset_typing_format()
            return

        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            if cursor.hasSelection():
                atom = self._exact_selected_atom(cursor)
                pending = None if atom is None else (atom[4], key)
                if atom is not None and self._pending_delete == pending:
                    cursor.removeSelectedText()
                    self.setTextCursor(cursor)
                    self._clear_pending_delete()
                    self._reset_typing_format()
                    self.atomsChanged.emit()
                    return
                if atom is not None:
                    self._pending_delete = pending
                    self._select_atom(atom)
                    return
                self._expand_selection_to_atoms(cursor)
                self.setTextCursor(cursor)
                self._clear_pending_delete()
                super().keyPressEvent(event)
                self._reset_typing_format()
                return
            position = cursor.position()
            atom = (
                self._atom_ending_at(position) if key == Qt.Key.Key_Backspace
                else self._atom_starting_at(position)
            )
            if atom is not None:
                self._select_atom(atom)
                self._pending_delete = (atom[4], key)
                return
            inside = self._atom_containing(position)
            if inside is not None:
                self._select_atom(inside)
                self._pending_delete = (inside[4], key)
                return
            self._clear_pending_delete()
            super().keyPressEvent(event)
            self._reset_typing_format()
            return

        if event.text() and not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            cursor = self._prepare_insertion_cursor()
            self.setTextCursor(cursor)
            super().keyPressEvent(event)
            self._reset_typing_format()
            return

        self._clear_pending_delete()
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        self._clear_pending_delete()
        cursor = self.cursorForPosition(event.position().toPoint())
        atom = self._atom_containing(cursor.position())
        if atom is not None:
            self._select_atom(atom)
            event.accept()
            return
        super().mousePressEvent(event)
        self._reset_typing_format()

    def _prepare_insertion_cursor(self):
        cursor = self.textCursor()
        atom = self._exact_selected_atom(cursor) if cursor.hasSelection() else None
        if atom is not None:
            if self._pending_delete == (atom[4], Qt.Key.Key_Delete):
                cursor.setPosition(atom[0])
            else:
                cursor.setPosition(atom[1])
        elif cursor.hasSelection():
            self._expand_selection_to_atoms(cursor)
            cursor.removeSelectedText()
        self._clear_pending_delete()
        cursor.setCharFormat(QTextCharFormat())
        self.setTextCursor(cursor)
        return cursor

    def _reset_typing_format(self):
        cursor = self.textCursor()
        if not cursor.hasSelection():
            cursor.setCharFormat(QTextCharFormat())
            self.setTextCursor(cursor)

    def _clear_pending_delete(self):
        self._pending_delete = None

    def _keep_cursor_out_of_atom(self):
        if self._adjusting_cursor:
            return
        cursor = self.textCursor()
        if cursor.hasSelection():
            return
        self._clear_pending_delete()
        atom = self._atom_containing(cursor.position(), strict=True)
        if atom is None:
            return
        start, end, *_ = atom
        target = start if cursor.position() - start <= end - cursor.position() else end
        self._adjusting_cursor = True
        cursor.setPosition(target)
        self.setTextCursor(cursor)
        self._adjusting_cursor = False

    def _format_at(self, position: int) -> QTextCharFormat:
        cursor = QTextCursor(self.document())
        cursor.setPosition(position)
        cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
        return cursor.charFormat()

    def _atom_containing(self, position: int, strict: bool = False):
        for atom in self.atom_ranges():
            start, end = atom[:2]
            if (start < position < end) if strict else (start <= position < end):
                return atom
        return None

    def _atom_ending_at(self, position: int):
        return next((atom for atom in self.atom_ranges() if atom[1] == position), None)

    def _atom_starting_at(self, position: int):
        return next((atom for atom in self.atom_ranges() if atom[0] == position), None)

    def _exact_selected_atom(self, cursor):
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        return next((atom for atom in self.atom_ranges() if atom[0] == start and atom[1] == end), None)

    def _select_atom(self, atom):
        cursor = QTextCursor(self.document())
        cursor.setPosition(atom[0])
        cursor.setPosition(atom[1], QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cursor)

    def _expand_selection_to_atoms(self, cursor):
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        for atom in self.atom_ranges():
            if atom[0] < end and atom[1] > start:
                start = min(start, atom[0])
                end = max(end, atom[1])
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)


class FormulaEditorDialog(QDialog):
    """User-visible names backed by safe stable tokens and unique signal keys."""

    def __init__(
        self,
        available_signals: Mapping[str, Mapping],
        parent=None,
        preview_callback: Callable[[CalculatedSignalDefinition], CalculatedSignalResult] | None = None,
    ):
        super().__init__(parent)
        self.available_signals = available_signals
        self.preview_callback = preview_callback
        self.signal_tokens: dict[str, str] = {}
        self._key_tokens: dict[str, str] = {}
        self._syncing_atoms = False
        self._validated = None
        self._build_ui()
        self.populate_signals()

    def _build_ui(self):
        self.setWindowTitle("创建自定义公式计算通道")
        self.resize(850, 700)
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：WheelPower")
        self.unit_edit = QLineEdit()
        self.unit_edit.setPlaceholderText("可选，例如：kW、m/s²、rpm/s")
        form.addRow("新信号名称：", self.name_edit)
        form.addRow("结果单位：", self.unit_edit)
        root.addLayout(form)

        root.addWidget(QLabel("公式："))
        self.formula_edit = AtomicFormulaEdit()
        self.formula_edit.setPlaceholderText("从下方列表插入信号，例如：(VehicleSpeed + Torque)^2")
        self.formula_edit.setMinimumHeight(110)
        root.addWidget(self.formula_edit)

        content = QGridLayout()
        content.addWidget(QLabel("搜索信号："), 0, 0)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入部分名称，大小写不敏感")
        content.addWidget(self.search_edit, 0, 1)
        self.signal_list = QListWidget()
        self.signal_list.setMinimumHeight(210)
        content.addWidget(self.signal_list, 1, 0, 1, 2)
        self.insert_signal_button = QPushButton("插入信号")
        content.addWidget(self.insert_signal_button, 2, 0, 1, 2)
        self.binding_title = QLabel("已选信号：")
        content.addWidget(self.binding_title, 0, 2)
        self.binding_list = QListWidget()
        self.binding_list.setMinimumWidth(330)
        content.addWidget(self.binding_list, 1, 2, 2, 1)
        root.addLayout(content)

        root.addWidget(QLabel("插入函数："))
        functions = QGridLayout()
        self.function_buttons = {}
        for index, function in enumerate(FUNCTION_NAMES):
            button = QPushButton(function)
            button.setToolTip(f"插入 {function}() 并将光标定位到括号内")
            button.clicked.connect(lambda _checked=False, name=function: self.insert_function(name))
            functions.addWidget(button, index // 7, index % 7)
            self.function_buttons[function] = button
        root.addLayout(functions)

        actions = QHBoxLayout()
        self.validate_button = QPushButton("验证公式")
        self.preview_button = QPushButton("预览/诊断")
        actions.addWidget(self.validate_button)
        actions.addWidget(self.preview_button)
        actions.addStretch()
        root.addLayout(actions)
        self.diagnostic_output = QPlainTextEdit()
        self.diagnostic_output.setReadOnly(True)
        self.diagnostic_output.setMaximumHeight(150)
        root.addWidget(self.diagnostic_output)

        bottom = QHBoxLayout()
        bottom.addStretch()
        self.generate_button = QPushButton("生成通道")
        self.cancel_button = QPushButton("取消")
        bottom.addWidget(self.generate_button)
        bottom.addWidget(self.cancel_button)
        root.addLayout(bottom)

        self.search_edit.textChanged.connect(self.filter_signals)
        self.search_edit.returnPressed.connect(self.insert_selected_signal)
        self.signal_list.itemDoubleClicked.connect(
            lambda item: self.insert_signal_key(item.data(SIGNAL_KEY_ROLE))
        )
        self.insert_signal_button.clicked.connect(self.insert_selected_signal)
        self.formula_edit.atomsChanged.connect(self._sync_selected_signals)
        self.validate_button.clicked.connect(self.validate_formula)
        self.preview_button.clicked.connect(self.preview_formula)
        self.generate_button.clicked.connect(self._accept_if_valid)
        self.cancel_button.clicked.connect(self.reject)

    def populate_signals(self):
        self.signal_list.clear()
        ordered = sorted(
            self.available_signals,
            key=lambda key: (str(self.available_signals[key].get("display_name", key)).casefold(), key),
        )
        for key in ordered:
            item = QListWidgetItem(self.signal_display(key))
            item.setData(SIGNAL_KEY_ROLE, key)
            item.setToolTip(f"Unique key: {key}")
            self.signal_list.addItem(item)
        self.filter_signals(self.search_edit.text())

    def _short_name(self, key: str) -> str:
        info = self.available_signals[key]
        name = str(info.get("display_name", info.get("name", key)))
        for prefix in ("🧮 ", "🚌 ", "⏱️ "):
            name = name.removeprefix(prefix)
        return name

    def _visible_atom_label(self, key: str) -> str:
        name = self._short_name(key)
        referenced = self.formula_edit.referenced_keys()
        conflicts = {
            item for item in referenced
            if item != key and self._short_name(item).casefold() == name.casefold()
        }
        if not conflicts:
            return name
        info = self.available_signals[key]
        group, channel = info.get("group"), info.get("channel")
        if group is not None and group >= 0 and channel is not None and channel >= 0:
            return f"{name} [G{group}:C{channel}]"
        source = info.get("source") or ("Calculated" if key.startswith(("CALC_", "MATH_")) else "Data")
        return f"{name} [{source}:{key}]"

    def signal_display(self, key: str) -> str:
        info = self.available_signals[key]
        name = self._short_name(key)
        group, channel = info.get("group"), info.get("channel")
        source = info.get("source") or (
            "Calculated" if key.startswith(("CALC_", "MATH_")) else
            "CAN" if key.startswith("CAN_") else "Data"
        )
        details = [f"Source: {source}"]
        if group is not None and group >= 0:
            details.append(f"Group: {group}")
        if channel is not None and channel >= 0:
            details.append(f"Channel: {channel}")
        details.append(f"Key: {key}")
        return f"{name}  ({', '.join(details)})"

    def filter_signals(self, query: str):
        needle = query.strip().casefold()
        for row in range(self.signal_list.count()):
            item = self.signal_list.item(row)
            item.setHidden(bool(needle) and needle not in item.text().casefold())

    def insert_selected_signal(self):
        item = self.signal_list.currentItem()
        if item is not None and not item.isHidden():
            self.insert_signal_key(item.data(SIGNAL_KEY_ROLE))

    def insert_signal_key(self, key: str) -> str:
        if key not in self.available_signals:
            raise KeyError(key)
        token = self._key_tokens.get(key)
        if token is None:
            token = f"S{len(self._key_tokens) + 1:03d}"
            self._key_tokens[key] = token
            self.signal_tokens[token] = key
        self.formula_edit.insert_signal(self._short_name(key), token, key)
        self._sync_selected_signals()
        return token

    def insert_function(self, function: str):
        if function not in ALLOWED_FUNCTIONS:
            raise ValueError(f"不支持的函数: {function}")
        self.formula_edit.insert_normal_text(f"{function}()", cursor_back=1)

    def _insert_text(self, text: str, cursor_back: int = 0):
        self.formula_edit.insert_normal_text(text, cursor_back)

    def _sync_selected_signals(self):
        if self._syncing_atoms:
            return
        self._syncing_atoms = True
        try:
            self.formula_edit.refresh_atom_labels(self._visible_atom_label)
            self.binding_list.clear()
            for key in self.formula_edit.referenced_keys():
                self.binding_list.addItem(self.signal_display(key))
        finally:
            self._syncing_atoms = False

    def internal_formula(self) -> str:
        return self.formula_edit.token_formula()

    def validate_formula(self):
        try:
            self._validated = parse_and_validate_formula(
                self.internal_formula(), self.signal_tokens
            )
        except FormulaError as exc:
            issue = exc.issue
            location = ""
            if issue.line is not None:
                location = f"（第 {issue.line} 行"
                if issue.column is not None:
                    location += f"，第 {issue.column + 1} 列"
                location += "）"
            self.diagnostic_output.setPlainText(f"✗ {issue.message}{location}")
            return None
        self.diagnostic_output.setPlainText(
            f"✓ 公式有效\n依赖信号：{len(self._validated.dependencies)}\n"
            f"规范化公式：{self._validated.normalized_formula}"
        )
        return self._validated

    def build_definition(self, stable_id: str) -> CalculatedSignalDefinition | None:
        validated = self.validate_formula()
        if validated is None:
            return None
        return CalculatedSignalDefinition(
            stable_id=stable_id,
            display_name=self.name_edit.text().strip(),
            user_formula=self.formula_edit.toPlainText().strip(),
            normalized_formula=validated.normalized_formula,
            signal_tokens=dict(self.signal_tokens),
            dependencies=validated.dependencies,
            result_unit=self.unit_edit.text().strip(),
            comment=f"公式计算: {validated.normalized_formula}",
        )

    def preview_formula(self):
        definition = self.build_definition("__preview__")
        if definition is None:
            return None
        if self.preview_callback is None:
            self.diagnostic_output.setPlainText("✗ 预览服务不可用")
            return None
        result = self.preview_callback(definition)
        if result.status is not CalculationStatus.SUCCESS:
            self.diagnostic_output.setPlainText(
                f"✗ 预览失败：{result.error or '计算失败'}\n诊断：{result.diagnostics}"
            )
            return result
        diagnostics = result.diagnostics
        finite = result.samples[np.isfinite(result.samples)]
        lines = [
            "✓ 预览成功", f"依赖信号：{len(definition.dependencies)}",
            f"有效时间范围：{diagnostics.get('effective_start')} - {diagnostics.get('effective_end')}",
            f"输出样本数：{result.samples.size}",
            f"插值策略：{diagnostics.get('interpolation_policies', {})}",
            f"NaN：{result.nan_count}", f"Inf：{result.inf_count}",
            f"Engine diagnostics：{diagnostics}",
        ]
        if finite.size:
            lines.extend((f"最小值：{finite.min()}", f"最大值：{finite.max()}"))
        self.diagnostic_output.setPlainText("\n".join(lines))
        return result

    def _accept_if_valid(self):
        if not self.name_edit.text().strip():
            self.diagnostic_output.setPlainText("✗ 新信号名称不能为空")
            return
        if self.validate_formula() is not None:
            self.accept()
