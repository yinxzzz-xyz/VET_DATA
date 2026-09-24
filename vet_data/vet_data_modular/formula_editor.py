"""Formula editor dialog for calculated signals."""

from typing import Callable, Mapping

import numpy as np

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from .calculated_signal import (
    CalculationStatus, CalculatedSignalDefinition, CalculatedSignalResult,
)
from .formula_parser import FormulaError
from .formula_validator import ALLOWED_FUNCTIONS, parse_and_validate_formula


SIGNAL_KEY_ROLE = Qt.ItemDataRole.UserRole
FUNCTION_NAMES = (
    "sqrt", "abs", "sin", "cos", "tan", "sind", "cosd", "tand",
    "log", "log10", "exp", "derivative", "integral",
)


class FormulaEditorDialog(QDialog):
    """Token-based editor; readable bindings stay visible beside the formula."""

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
        self._validated = None
        self._build_ui()
        self.populate_signals()

    def _build_ui(self):
        self.setWindowTitle("创建自定义公式计算通道")
        self.resize(820, 680)
        root = QVBoxLayout(self)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：WheelPower")
        self.unit_edit = QLineEdit()
        self.unit_edit.setPlaceholderText("可选，例如：kW、m/s²、rpm/s")
        form.addRow("新信号名称：", self.name_edit)
        form.addRow("结果单位：", self.unit_edit)
        root.addLayout(form)

        root.addWidget(QLabel("公式（信号通过下方列表插入为稳定 token）："))
        self.formula_edit = QPlainTextEdit()
        self.formula_edit.setPlaceholderText("例如：(S001 + S002)^2 或 derivative(S001)")
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

        content.addWidget(QLabel("Token 绑定："), 0, 2)
        self.binding_list = QListWidget()
        self.binding_list.setMinimumWidth(300)
        content.addWidget(self.binding_list, 1, 2, 2, 1)
        root.addLayout(content)

        root.addWidget(QLabel("插入函数："))
        functions = QHBoxLayout()
        self.function_buttons = {}
        for function in FUNCTION_NAMES:
            button = QPushButton(function)
            button.setToolTip(f"插入 {function}() 并将光标定位到括号内")
            button.clicked.connect(lambda _checked=False, name=function: self.insert_function(name))
            functions.addWidget(button)
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

    def signal_display(self, key: str) -> str:
        info = self.available_signals[key]
        name = str(info.get("display_name", info.get("name", key)))
        group, channel = info.get("group"), info.get("channel")
        source = info.get("source")
        if not source:
            source = "Calculated" if key.startswith(("CALC_", "MATH_")) else (
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
            self.binding_list.addItem(f"{token} = {self.signal_display(key)}")
        self._insert_text(token)
        return token

    def insert_function(self, function: str):
        if function not in ALLOWED_FUNCTIONS:
            raise ValueError(f"不支持的函数: {function}")
        self._insert_text(f"{function}()", cursor_back=1)

    def _insert_text(self, text: str, cursor_back: int = 0):
        self.formula_edit.setFocus()
        cursor = self.formula_edit.textCursor()
        cursor.insertText(text)
        if cursor_back:
            cursor.movePosition(QTextCursor.MoveOperation.Left, n=cursor_back)
        self.formula_edit.setTextCursor(cursor)

    def validate_formula(self):
        try:
            self._validated = parse_and_validate_formula(
                self.formula_edit.toPlainText(), self.signal_tokens
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
            "✓ 预览成功",
            f"依赖信号：{len(definition.dependencies)}",
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
