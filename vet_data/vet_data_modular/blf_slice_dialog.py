"""Qt input, condition confirmation, and duplicate-selection dialogs."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QComboBox, QDialog, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QRadioButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .blf_slice_models import InputMode, SliceTask
from .blf_slice_service import resolve_output_directory, validate_task
from .blf_slice_table import TableParseError, TableParseResult, parse_condition_table


OTHER_OFFSET = "other"
SPECIAL_UTC_OFFSET_MINUTES = (
    -570, -210, 210, 270, 330, 345, 390, 525, 570, 630, 765, 825,
)


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
        root = QVBoxLayout(self)
        root.addWidget(QLabel("每个重复组必须选择一个保留文件；原文件不会被修改。"))
        self._groups = []
        for index, group in enumerate(self.groups, 1):
            box = QGroupBox(f"重复组 {index}（{group.size} 字节）")
            layout, buttons = QVBoxLayout(box), QButtonGroup(self)
            buttons.setExclusive(True)
            for path in group.paths:
                button = QRadioButton(str(path))
                button.setProperty("blf_path", path)
                layout.addWidget(button)
                buttons.addButton(button)
            self._groups.append(buttons)
            root.addWidget(box)
        actions = QHBoxLayout()
        actions.addStretch()
        ok, cancel = QPushButton("继续"), QPushButton("取消任务")
        ok.clicked.connect(self._accept_if_complete)
        cancel.clicked.connect(self.reject)
        actions.addWidget(ok)
        actions.addWidget(cancel)
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
        self.resize(1050, 620)
        self._table_result: TableParseResult | None = None
        root = QVBoxLayout(self)
        input_box, form = QGroupBox("输入"), QFormLayout()
        input_box.setLayout(form)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("单个 BLF", InputMode.FILE)
        self.mode_combo.addItem("BLF 文件夹", InputMode.FOLDER)
        self.input_edit, self.table_edit, self.output_edit = QLineEdit(), QLineEdit(), QLineEdit()
        form.addRow("输入方式", self.mode_combo)
        form.addRow("BLF 输入", self._path_row(self.input_edit, self._choose_input))
        form.addRow("工况表", self._path_row(self.table_edit, self._choose_table))
        form.addRow("输出目录", self._path_row(self.output_edit, self._choose_output))
        self.output_hint = QLabel("未单独选择时使用 BLF 输入所在目录。")
        form.addRow("", self.output_hint)
        root.addWidget(input_box)
        settings, settings_layout = QGroupBox("任务级设置"), QHBoxLayout()
        settings.setLayout(settings_layout)
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
        settings_layout.addWidget(QLabel("BLF 时区"))
        settings_layout.addWidget(self.blf_offset_combo)
        settings_layout.addWidget(self.blf_special_offset_combo)
        settings_layout.addWidget(QLabel("工况表时区"))
        settings_layout.addWidget(self.table_offset_combo)
        settings_layout.addWidget(self.table_special_offset_combo)
        settings_layout.addSpacing(20)
        settings_layout.addWidget(QLabel("批量设置：向前"))
        self.batch_before_spin, self.batch_after_spin = QSpinBox(), QSpinBox()
        for spin in (self.batch_before_spin, self.batch_after_spin):
            spin.setRange(0, 300)
            spin.setValue(60)
            spin.setSuffix(" 秒")
        settings_layout.addWidget(self.batch_before_spin)
        settings_layout.addWidget(QLabel("向后"))
        settings_layout.addWidget(self.batch_after_spin)
        self.apply_batch_button = QPushButton("应用到全部工况")
        self.apply_batch_button.clicked.connect(self._apply_batch_windows)
        settings_layout.addWidget(self.apply_batch_button)
        settings_layout.addStretch()
        self._sync_special_offset_visibility(self.blf_offset_combo, self.blf_special_offset_combo)
        self._sync_special_offset_visibility(self.table_offset_combo, self.table_special_offset_combo)
        root.addWidget(settings)
        self.condition_table = QTableWidget(0, len(self.COLUMNS))
        self.condition_table.setHorizontalHeaderLabels(self.COLUMNS)
        self.condition_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.condition_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.condition_table, 1)
        actions = QHBoxLayout()
        select_all, clear_all, start, cancel = QPushButton("全选"), QPushButton("全部取消"), QPushButton("开始切片"), QPushButton("取消")
        select_all.clicked.connect(lambda: self._set_all_selected(True))
        clear_all.clicked.connect(lambda: self._set_all_selected(False))
        start.clicked.connect(self._confirm_task)
        cancel.clicked.connect(self.reject)
        for widget in (select_all, clear_all):
            actions.addWidget(widget)
        actions.addStretch()
        actions.addWidget(start)
        actions.addWidget(cancel)
        root.addLayout(actions)

    def _path_row(self, edit, callback):
        widget, layout = QWidget(), QHBoxLayout()
        widget.setLayout(layout)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        button = QPushButton("浏览…")
        button.clicked.connect(callback)
        layout.addWidget(button)
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
                self.output_hint.setText(f"实际输出目录：{default}（默认）")

    def _choose_table(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择工况表", "", "工况表 (*.csv *.xlsx)")
        if path:
            self.table_edit.setText(path)
            self.load_condition_table(path)

    def _choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if path:
            self.output_edit.setText(path)
            self.output_hint.setText(f"实际输出目录：{path}")

    def load_condition_table(self, path):
        try:
            result = parse_condition_table(path)
        except (TableParseError, OSError) as exc:
            self._table_result = None
            self.condition_table.setRowCount(0)
            QMessageBox.warning(self, "工况表无效", str(exc))
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
