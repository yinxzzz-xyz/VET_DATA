"""Qt input, condition confirmation, and duplicate-selection dialogs."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QComboBox, QDialog, QFileDialog, QFormLayout,
    QGroupBox, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QRadioButton, QSpinBox, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget, QScrollArea,
)

from .blf_slice_models import InputMode, SliceTask
from .blf_slice_service import resolve_output_directory, validate_task
from .blf_slice_table import (
    DEFAULT_AFTER_SECONDS, DEFAULT_BEFORE_SECONDS, OPTIONAL_AFTER_HEADER,
    OPTIONAL_BEFORE_HEADER, REQUIRED_HEADERS, TableParseError, TableParseResult,
    parse_condition_table,
)
from .theme import DEFAULT_THEME, build_blf_slice_stylesheet


OTHER_OFFSET = "other"
SPECIAL_UTC_OFFSET_MINUTES = (
    -570, -210, 210, 270, 330, 345, 390, 525, 570, 630, 765, 825,
)


def condition_table_help_text() -> str:
    """Return format guidance using the parser's public headers and defaults."""
    recorded_at_header, content_header = REQUIRED_HEADERS
    return f"""工况表支持的文件格式：
• Excel 工作簿：.xlsx
• CSV 文件：.csv

第一行必须是表头，并且必须包含以下两列（顺序不限、不能重复）：
• {recorded_at_header}：工况发生时间
• {content_header}：工况名称或工况描述

可选表头：
• {OPTIONAL_BEFORE_HEADER}：从工况时间向前扩展的切片秒数
• {OPTIONAL_AFTER_HEADER}：从工况时间向后扩展的切片秒数

默认规则：
• 没有“{OPTIONAL_BEFORE_HEADER}”列或对应单元格为空时，使用当前默认值：{DEFAULT_BEFORE_SECONDS} 秒。
• 没有“{OPTIONAL_AFTER_HEADER}”列或对应单元格为空时，使用当前默认值：{DEFAULT_AFTER_SECONDS} 秒。

兼容规则：
• 允许存在“序号、时间来源、纬度、经度”等其他附加列，附加列会被忽略，不会导致解析失败。
• 表头顺序不固定，只要求必需表头存在且不重复。

时间格式：
• XLSX 可以使用真实的 Excel 日期时间单元格。
• 文本时间须使用项目支持的完整日期时间格式，推荐：YYYY-MM-DD HH:MM:SS。
• 同时兼容现有格式：YYYY/M/D H:MM（分钟精度会将秒补为 00）。

示例：
{recorded_at_header} | {content_header} | {OPTIONAL_BEFORE_HEADER} | {OPTIONAL_AFTER_HEADER}
2026-09-11 22:27:00 | 加速测试 | 30 | 30
2026-09-11 22:30:00 | 制动测试 |   |

示例第二行的可选秒数为空，因此使用上述当前默认值。"""


class _HelpToolButton(QToolButton):
    """Compact help button that also activates explicitly on Enter."""

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)


def offset_label(offset: timedelta) -> str:
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, remainder = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}:{remainder:02d}"


def populate_special_offset_combo(combo: QComboBox) -> None:
    combo.clear()
    for minutes in SPECIAL_UTC_OFFSET_MINUTES:
        value = timedelta(minutes=minutes)
        combo.addItem(offset_label(value), minutes)


def populate_offset_combo(
    combo: QComboBox,
    special_combo: QComboBox | None = None,
    default: timedelta = timedelta(hours=8),
) -> None:
    """Populate whole-hour offsets plus one gateway to real fractional offsets."""
    combo.clear()
    for minutes in range(-720, 841, 60):
        combo.addItem(offset_label(timedelta(minutes=minutes)), minutes)
    combo.addItem("其他...", OTHER_OFFSET)
    default_minutes = int(default.total_seconds() // 60)
    index = combo.findData(default_minutes)
    if index >= 0:
        combo.setCurrentIndex(index)
    elif special_combo is not None and default_minutes in SPECIAL_UTC_OFFSET_MINUTES:
        combo.setCurrentIndex(combo.findData(OTHER_OFFSET))
        special_combo.setCurrentIndex(special_combo.findData(default_minutes))
    else:
        raise ValueError("default UTC offset is not available in the GUI selector")


def selected_offset(combo: QComboBox, special_combo: QComboBox) -> timedelta:
    minutes = combo.currentData()
    if minutes == OTHER_OFFSET:
        minutes = special_combo.currentData()
    return timedelta(minutes=int(minutes))


def retain_duplicate_choices(files, groups, choices: dict[int, Path]) -> tuple[Path, ...]:
    """Apply one explicit retained path per exact-duplicate group."""
    rejected: set[Path] = set()
    for index, group in enumerate(groups):
        chosen = Path(choices[index]) if index in choices else None
        if chosen not in group.paths:
            raise ValueError(f"重复组 {index + 1} 必须且只能保留一个文件。")
        rejected.update(path for path in group.paths if path != chosen)
    return tuple(Path(path) for path in files if Path(path) not in rejected)


class DuplicateSelectionDialog(QDialog):
    def __init__(self, groups, parent=None):
        super().__init__(parent)
        self.groups = tuple(groups)
        self.setWindowTitle("确认重复 BLF")
        self.setModal(True)
        self.setProperty("uiDialog", "blfSlice")
        self.setMinimumSize(560, 360)
        self.resize(760, 480)
        self.setStyleSheet(build_blf_slice_stylesheet(DEFAULT_THEME))
        root = QVBoxLayout(self)
        root.setContentsMargins(
            DEFAULT_THEME.spacing.medium, DEFAULT_THEME.spacing.medium,
            DEFAULT_THEME.spacing.medium, DEFAULT_THEME.spacing.medium,
        )
        root.setSpacing(DEFAULT_THEME.spacing.small)
        guidance = QLabel("每个重复组必须选择一个保留文件；原文件不会被修改。")
        guidance.setProperty("uiTextRole", "secondary")
        root.addWidget(guidance)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        group_container = QWidget()
        group_layout = QVBoxLayout(group_container)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.setSpacing(DEFAULT_THEME.spacing.small)
        self._groups = []
        for index, group in enumerate(self.groups, 1):
            box = QGroupBox(f"重复组 {index}（{group.size} 字节）")
            box.setProperty("uiBlfSection", "true")
            layout, buttons = QVBoxLayout(box), QButtonGroup(self)
            buttons.setExclusive(True)
            for path in group.paths:
                button = QRadioButton(str(path))
                button.setProperty("blf_path", path)
                button.setToolTip(str(path))
                layout.addWidget(button)
                buttons.addButton(button)
            self._groups.append(buttons)
            group_layout.addWidget(box)
        group_layout.addStretch()
        scroll.setWidget(group_container)
        root.addWidget(scroll, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        self.continue_button = QPushButton("继续")
        self.cancel_button = QPushButton("取消任务")
        self.continue_button.setProperty("uiRole", "primary")
        self.cancel_button.setProperty("uiRole", "secondary")
        self.continue_button.clicked.connect(self._accept_if_complete)
        self.cancel_button.clicked.connect(self.reject)
        actions.addWidget(self.continue_button)
        actions.addWidget(self.cancel_button)
        root.addLayout(actions)

    def choices(self) -> dict[int, Path]:
        return {
            index: Path(group.checkedButton().property("blf_path"))
            for index, group in enumerate(self._groups)
            if group.checkedButton() is not None
        }

    def _accept_if_complete(self) -> None:
        if len(self.choices()) != len(self.groups):
            QMessageBox.warning(self, "选择不完整", "每个重复组必须选择一个保留文件。")
        else:
            self.accept()


class BlfSliceDialog(QDialog):
    task_confirmed = pyqtSignal(object, object)
    COLUMNS = ("选择", "原始行", "工况名称", "工况时间", "向前秒数", "向后秒数", "校验状态")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("BLF 工况切片")
        self.setModal(False)
        self.setProperty("uiDialog", "blfSlice")
        self.setMinimumSize(820, 560)
        self.resize(1050, 680)
        self.setStyleSheet(build_blf_slice_stylesheet(DEFAULT_THEME))
        self._table_result: TableParseResult | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(
            DEFAULT_THEME.spacing.medium, DEFAULT_THEME.spacing.medium,
            DEFAULT_THEME.spacing.medium, DEFAULT_THEME.spacing.medium,
        )
        root.setSpacing(DEFAULT_THEME.spacing.small)
        self.input_box, form = QGroupBox("输入"), QFormLayout()
        self.input_box.setObjectName("blfInputSection")
        self.input_box.setProperty("uiBlfSection", "true")
        self.input_box.setLayout(form)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(DEFAULT_THEME.spacing.small)
        form.setVerticalSpacing(DEFAULT_THEME.spacing.xsmall)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("单个 BLF", InputMode.FILE)
        self.mode_combo.addItem("BLF 文件夹", InputMode.FOLDER)
        self.input_edit, self.table_edit, self.output_edit = QLineEdit(), QLineEdit(), QLineEdit()
        for edit in (self.input_edit, self.table_edit, self.output_edit):
            edit.textChanged.connect(edit.setToolTip)
        form.addRow("输入方式", self.mode_combo)
        self.input_path_row = self._path_row(self.input_edit, self._choose_input)
        self.table_path_row = self._path_row(self.table_edit, self._choose_table)
        self.output_path_row = self._path_row(self.output_edit, self._choose_output)
        form.addRow("BLF 输入", self.input_path_row)
        self.table_label = QWidget()
        table_label_layout = QHBoxLayout(self.table_label)
        table_label_layout.setContentsMargins(0, 0, 0, 0)
        table_label_layout.setSpacing(DEFAULT_THEME.spacing.xsmall)
        table_label_layout.addWidget(QLabel("工况表"))
        self.table_help_button = _HelpToolButton()
        self.table_help_button.setObjectName("conditionTableHelpButton")
        self.table_help_button.setText("?")
        self.table_help_button.setToolTip("查看工况表格式说明")
        self.table_help_button.setAccessibleName("查看工况表格式说明")
        self.table_help_button.setAutoRaise(True)
        self.table_help_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        help_size = self.table_edit.sizeHint().height()
        self.table_help_button.setFixedSize(help_size, help_size)
        self.table_help_button.clicked.connect(self._show_table_format_help)
        table_label_layout.addWidget(self.table_help_button)
        form.addRow(self.table_label, self.table_path_row)
        form.addRow("输出目录", self.output_path_row)
        self.output_hint = QLabel("未单独选择时使用 BLF 输入所在目录。")
        self.output_hint.setProperty("uiTextRole", "secondary")
        self.output_hint.setToolTip(self.output_hint.text())
        form.addRow("", self.output_hint)
        root.addWidget(self.input_box)
        self.settings_box, settings_layout = QGroupBox("任务级设置"), QGridLayout()
        self.settings_box.setObjectName("blfSettingsSection")
        self.settings_box.setProperty("uiBlfSection", "true")
        self.settings_box.setLayout(settings_layout)
        settings_layout.setHorizontalSpacing(DEFAULT_THEME.spacing.small)
        settings_layout.setVerticalSpacing(DEFAULT_THEME.spacing.xsmall)
        self.blf_offset_combo, self.table_offset_combo = QComboBox(), QComboBox()
        self.blf_special_offset_combo, self.table_special_offset_combo = QComboBox(), QComboBox()
        populate_special_offset_combo(self.blf_special_offset_combo)
        populate_special_offset_combo(self.table_special_offset_combo)
        populate_offset_combo(self.blf_offset_combo, self.blf_special_offset_combo)
        populate_offset_combo(self.table_offset_combo, self.table_special_offset_combo)
        self.blf_offset_combo.currentIndexChanged.connect(
            lambda: self._sync_special_offset_visibility(self.blf_offset_combo, self.blf_special_offset_combo)
        )
        self.table_offset_combo.currentIndexChanged.connect(
            lambda: self._sync_special_offset_visibility(self.table_offset_combo, self.table_special_offset_combo)
        )
        settings_layout.addWidget(QLabel("BLF 时区"), 0, 0)
        settings_layout.addWidget(self.blf_offset_combo, 0, 1)
        settings_layout.addWidget(self.blf_special_offset_combo, 0, 2)
        settings_layout.addWidget(QLabel("工况表时区"), 0, 3)
        settings_layout.addWidget(self.table_offset_combo, 0, 4)
        settings_layout.addWidget(self.table_special_offset_combo, 0, 5)
        settings_layout.addWidget(QLabel("批量设置：向前"), 1, 0)
        self.batch_before_spin, self.batch_after_spin = QSpinBox(), QSpinBox()
        for spin in (self.batch_before_spin, self.batch_after_spin):
            spin.setRange(0, 300)
            spin.setValue(60)
            spin.setSuffix(" 秒")
        settings_layout.addWidget(self.batch_before_spin, 1, 1)
        settings_layout.addWidget(QLabel("向后"), 1, 3)
        settings_layout.addWidget(self.batch_after_spin, 1, 4)
        self.apply_batch_button = QPushButton("应用到全部工况")
        self.apply_batch_button.setProperty("uiRole", "secondary")
        self.apply_batch_button.clicked.connect(self._apply_batch_windows)
        settings_layout.addWidget(self.apply_batch_button, 1, 5)
        settings_layout.setColumnStretch(6, 1)
        self._sync_special_offset_visibility(self.blf_offset_combo, self.blf_special_offset_combo)
        self._sync_special_offset_visibility(self.table_offset_combo, self.table_special_offset_combo)
        root.addWidget(self.settings_box)
        self.condition_box = QGroupBox("工况表")
        self.condition_box.setObjectName("blfConditionSection")
        self.condition_box.setProperty("uiBlfSection", "true")
        condition_layout = QVBoxLayout(self.condition_box)
        self.condition_table = QTableWidget(0, len(self.COLUMNS))
        self.condition_table.setObjectName("blfConditionTable")
        self.condition_table.setHorizontalHeaderLabels(self.COLUMNS)
        self.condition_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.condition_table.setAlternatingRowColors(True)
        self.condition_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.condition_table.verticalHeader().setDefaultSectionSize(30)
        header = self.condition_table.horizontalHeader()
        for column in (0, 1, 3, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.condition_table.itemChanged.connect(self._sync_table_item_tooltip)
        condition_layout.addWidget(self.condition_table)
        root.addWidget(self.condition_box, 1)

        self.execution_box = QGroupBox("执行")
        self.execution_box.setObjectName("blfExecutionSection")
        self.execution_box.setProperty("uiBlfSection", "true")
        actions = QHBoxLayout(self.execution_box)
        self.select_all_button = QPushButton("全选")
        self.clear_all_button = QPushButton("全部取消")
        self.start_button = QPushButton("开始切片")
        self.cancel_button = QPushButton("取消")
        self.select_all_button.clicked.connect(lambda: self._set_all_selected(True))
        self.clear_all_button.clicked.connect(lambda: self._set_all_selected(False))
        self.start_button.clicked.connect(self._confirm_task)
        self.cancel_button.clicked.connect(self.reject)
        self.start_button.setProperty("uiRole", "primary")
        for widget in (self.select_all_button, self.clear_all_button, self.cancel_button):
            widget.setProperty("uiRole", "secondary")
        for widget in (self.select_all_button, self.clear_all_button):
            actions.addWidget(widget)
        actions.addStretch()
        actions.addWidget(self.start_button)
        actions.addWidget(self.cancel_button)
        root.addWidget(self.execution_box)

    def _path_row(self, edit, callback):
        widget, layout = QWidget(), QHBoxLayout()
        widget.setLayout(layout)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        button = QPushButton("浏览…")
        button.setProperty("uiRole", "secondary")
        button.clicked.connect(callback)
        layout.addWidget(button)
        widget.browse_button = button
        return widget

    def _choose_input(self):
        if self.mode_combo.currentData() is InputMode.FILE:
            path, _ = QFileDialog.getOpenFileName(self, "选择 BLF", "", "BLF 文件 (*.blf)")
        else:
            path = QFileDialog.getExistingDirectory(self, "选择直接包含 BLF 的文件夹")
        if path:
            self.input_edit.setText(path)
            if not self.output_edit.text().strip():
                default = resolve_output_directory(self.mode_combo.currentData(), path)
                self._set_output_hint(f"实际输出目录：{default}（默认）")

    def _choose_table(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择工况表", "", "工况表 (*.csv *.xlsx)")
        if path:
            self.table_edit.setText(path)
            self.load_condition_table(path)

    def _choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if path:
            self.output_edit.setText(path)
            self._set_output_hint(f"实际输出目录：{path}")

    def _show_table_format_help(self):
        QMessageBox.information(self, "工况表格式说明", condition_table_help_text())

    def _set_output_hint(self, text):
        self.output_hint.setText(text)
        self.output_hint.setToolTip(text)

    @staticmethod
    def _sync_table_item_tooltip(item):
        if item is not None and item.toolTip() != item.text():
            item.setToolTip(item.text())

    def load_condition_table(self, path):
        try:
            result = parse_condition_table(path)
        except (TableParseError, OSError) as exc:
            self._table_result = None
            self.condition_table.setRowCount(0)
            QMessageBox.warning(
                self,
                "工况表无效",
                f"{exc}\n\n请修改工况表格式后重试。",
            )
            return
        self._table_result = result
        self.condition_table.setRowCount(len(result.conditions))
        for row, condition in enumerate(result.conditions):
            selected = QTableWidgetItem()
            selected.setFlags(selected.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            selected.setCheckState(Qt.CheckState.Checked)
            self.condition_table.setItem(row, 0, selected)
            source = QTableWidgetItem(str(condition.original_row_number))
            source.setFlags(source.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.condition_table.setItem(row, 1, source)
            self.condition_table.setItem(row, 2, QTableWidgetItem(condition.name))
            self.condition_table.setItem(row, 3, QTableWidgetItem(condition.recorded_at.strftime("%Y-%m-%d %H:%M:%S")))
            for column, value in ((4, condition.before_seconds), (5, condition.after_seconds)):
                spin = QSpinBox()
                spin.setRange(0, 300)
                spin.setValue(value)
                self.condition_table.setCellWidget(row, column, spin)
            status = QTableWidgetItem("待校验")
            status.setFlags(status.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.condition_table.setItem(row, 6, status)

    def _set_all_selected(self, selected):
        state = Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked
        for row in range(self.condition_table.rowCount()):
            self.condition_table.item(row, 0).setCheckState(state)

    @staticmethod
    def _sync_special_offset_visibility(combo, special_combo):
        special_combo.setVisible(combo.currentData() == OTHER_OFFSET)

    def _apply_batch_windows(self):
        before, after = self.batch_before_spin.value(), self.batch_after_spin.value()
        for row in range(self.condition_table.rowCount()):
            self.condition_table.cellWidget(row, 4).setValue(before)
            self.condition_table.cellWidget(row, 5).setValue(after)

    def build_task_snapshot(self, now=None):
        table_path = Path(self.table_edit.text().strip())
        if self._table_result is None or table_path != self._table_result.source_path:
            self.load_condition_table(table_path)
        if self._table_result is None:
            raise ValueError("请先选择并成功解析工况表。")
        conditions = []
        for row, original in enumerate(self._table_result.conditions):
            try:
                recorded_at = datetime.strptime(self.condition_table.item(row, 3).text().strip(), "%Y-%m-%d %H:%M:%S")
            except ValueError as exc:
                self._mark_invalid(row, 3, "时间格式应为 yyyy-MM-dd HH:mm:ss")
                raise ValueError(f"第 {original.original_row_number} 行工况时间无效。") from exc
            conditions.append(replace(
                original,
                name=self.condition_table.item(row, 2).text(),
                recorded_at=recorded_at,
                before_seconds=self.condition_table.cellWidget(row, 4).value(),
                after_seconds=self.condition_table.cellWidget(row, 5).value(),
                selected=self.condition_table.item(row, 0).checkState() == Qt.CheckState.Checked,
            ))
        mode, input_path = self.mode_combo.currentData(), Path(self.input_edit.text().strip())
        output = self.output_edit.text().strip()
        return SliceTask(
            mode, input_path, table_path,
            resolve_output_directory(mode, input_path, output or None),
            tuple(conditions), now or datetime.now(),
            selected_offset(self.blf_offset_combo, self.blf_special_offset_combo),
            selected_offset(self.table_offset_combo, self.table_special_offset_combo),
        )

    def _mark_invalid(self, row, column, message):
        self.condition_table.item(row, 6).setText(message)
        self.condition_table.setCurrentCell(row, column)

    def _confirm_task(self):
        try:
            task = self.build_task_snapshot()
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "无法开始", str(exc))
            return
        validation = validate_task(task)
        if not validation.is_valid:
            first = validation.issues[0]
            if first.condition_row_number is not None:
                row = next((i for i, item in enumerate(task.conditions) if item.original_row_number == first.condition_row_number), 0)
                self._mark_invalid(row, {"name": 2, "recorded_at": 3, "before_seconds": 4, "after_seconds": 5}.get(first.field, 6), first.message)
            QMessageBox.warning(self, "参数校验失败", "\n".join(issue.message for issue in validation.issues))
            return
        self.task_confirmed.emit(task, self._table_result)
        self.accept()
