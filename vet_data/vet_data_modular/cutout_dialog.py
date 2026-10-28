"""Cut-out settings dialog: window summary, output content and destination."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from .data_cutout import (
    FORMAT_EXTENSIONS,
    CutoutContext,
    CutoutOption,
    build_options,
    default_option,
    default_output_name,
    option_by_key,
    sanitize_file_name,
    validate_output_path,
)
from .gui_state import create_gui_settings

LAST_MODE_KEY = "data_cutout/last_mode/{kind}"

INTRO_TEXT = (
    "将当前曲线视图显示的时间段从源文件中截取，另存为一个新文件。\n"
    "截取不会修改源文件，新文件只包含该时间段内的数据。"
)


def load_last_mode(settings, kind: str) -> str | None:
    if settings is None:
        return None
    try:
        value = settings.value(LAST_MODE_KEY.format(kind=kind))
    except (TypeError, ValueError, RuntimeError):
        return None
    return str(value) if value else None


def save_last_mode(settings, kind: str, option: CutoutOption) -> None:
    if settings is None:
        return
    settings.setValue(LAST_MODE_KEY.format(kind=kind), option.key)
    try:
        settings.sync()
    except (AttributeError, RuntimeError):
        pass


class CutoutDialog(QDialog):
    """Modal settings dialog shown before any cut-out work starts."""

    def __init__(
        self,
        context: CutoutContext,
        *,
        initial_dir: str | None = None,
        remembered_mode: str | None = None,
        settings=None,
        parent=None,
    ):
        super().__init__(parent)
        self.context = context
        self.options = tuple(build_options(context))
        self._settings = settings if settings is not None else create_gui_settings()
        self._name_edited = False
        self._radios: dict[str, QRadioButton] = {}
        self._descriptions: dict[str, QLabel] = {}

        self.setWindowTitle("数据截取")
        self.setModal(True)
        self.setMinimumWidth(620)
        self.setProperty("uiDialog", "dataCutout")

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        intro = QLabel(INTRO_TEXT)
        intro.setWordWrap(False)
        intro.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.intro_label = intro
        root.addWidget(intro)

        info_box = QGroupBox("本次截取")
        info_box.setProperty("uiPanel", "main")
        info_layout = QVBoxLayout(info_box)
        info_layout.setSpacing(4)
        self.source_label = QLabel(f"源文件：{Path(context.source_path).name}")
        self.source_label.setToolTip(context.source_path)
        self.range_label = QLabel(f"截取范围：{context.window.describe()}")
        info_layout.addWidget(self.source_label)
        info_layout.addWidget(self.range_label)
        root.addWidget(info_box)

        options_box = QGroupBox("输出内容")
        options_box.setProperty("uiPanel", "main")
        options_layout = QVBoxLayout(options_box)
        options_layout.setSpacing(8)
        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)
        for option in self.options:
            radio = QRadioButton(option.title)
            radio.setEnabled(option.enabled)
            if not option.enabled and option.disabled_reason:
                radio.setToolTip(option.disabled_reason)
            self._button_group.addButton(radio)
            self._radios[option.key] = radio
            options_layout.addWidget(radio)

            description_text = option.description
            if not option.enabled and option.disabled_reason:
                description_text = f"{option.disabled_reason}（该选项当前不可用）"
            description = QLabel(description_text)
            description.setWordWrap(True)
            description.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            description.setProperty("uiTextRole", "secondary")
            description.setToolTip(description_text)
            self._descriptions[option.key] = description
            description_row = QHBoxLayout()
            description_row.setContentsMargins(22, 0, 0, 0)
            description_row.setSpacing(0)
            description_row.addWidget(description)
            options_layout.addLayout(description_row)

        root.addWidget(options_box)

        destination_box = QGroupBox("输出位置")
        destination_box.setProperty("uiPanel", "main")
        destination_layout = QVBoxLayout(destination_box)
        destination_layout.setSpacing(6)

        dir_row = QHBoxLayout()
        dir_row.setSpacing(6)
        dir_row.addWidget(QLabel("输出目录"))
        self.directory_edit = QLineEdit(initial_dir or str(Path(context.source_path).parent))
        self.directory_edit.setProperty("uiControl", "main")
        self.directory_edit.textChanged.connect(self._sync_destination)
        dir_row.addWidget(self.directory_edit, 1)
        self.browse_button = QPushButton("浏览…")
        self.browse_button.setProperty("uiRole", "secondary")
        self.browse_button.clicked.connect(self._choose_directory)
        dir_row.addWidget(self.browse_button)
        destination_layout.addLayout(dir_row)

        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        name_row.addWidget(QLabel("文件名"))
        self.name_edit = QLineEdit()
        self.name_edit.setProperty("uiControl", "main")
        self.name_edit.textEdited.connect(self._on_name_edited)
        self.name_edit.textChanged.connect(self._sync_destination)
        name_row.addWidget(self.name_edit, 1)
        self.extension_label = QLabel()
        self.extension_label.setProperty("uiTextRole", "secondary")
        self.extension_label.setMinimumWidth(48)
        name_row.addWidget(self.extension_label)
        destination_layout.addLayout(name_row)

        self.path_preview = QLabel()
        self.path_preview.setWordWrap(True)
        self.path_preview.setProperty("uiTextRole", "secondary")
        destination_layout.addWidget(self.path_preview)
        root.addWidget(destination_box)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setProperty("uiRole", "secondary")
        self.cancel_button.clicked.connect(self.reject)
        self.accept_button = QPushButton("开始截取")
        self.accept_button.setProperty("uiRole", "primary")
        self.accept_button.setDefault(True)
        self.accept_button.clicked.connect(self.accept)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.accept_button)
        root.addLayout(buttons)

        for option in self.options:
            self._radios[option.key].toggled.connect(self._on_option_toggled)
        self._select_initial_option(remembered_mode)
        self._sync_destination()

    # ------------------------------------------------------------------ state
    def _select_initial_option(self, remembered_mode: str | None) -> None:
        remembered = option_by_key(self.options, remembered_mode)
        if remembered is not None and remembered.enabled:
            chosen = remembered
        else:
            chosen = default_option(self.options)
        if chosen is None:
            return
        self._name_edited = False
        self.name_edit.setText(
            Path(default_output_name(
                self.context.source_path, self.context.window,
                chosen.format, chosen.scope,
            )).stem
        )
        self._radios[chosen.key].setChecked(True)

    def selected_option(self) -> CutoutOption | None:
        for option in self.options:
            if self._radios[option.key].isChecked():
                return option
        return default_option(self.options)

    def output_path(self) -> Path:
        option = self.selected_option()
        extension = FORMAT_EXTENSIONS[option.format] if option is not None else ".bin"
        name = sanitize_file_name(self.name_edit.text()) or "cut"
        known = set(FORMAT_EXTENSIONS.values())
        lowered = name.lower()
        for other in known:
            if other != extension and lowered.endswith(other):
                name = name[: -len(other)]
                break
        if not name.lower().endswith(extension):
            name = f"{name}{extension}"
        directory = self.directory_edit.text().strip() or "."
        return Path(directory) / name

    # ------------------------------------------------------------- interaction
    def _on_name_edited(self, _text: str) -> None:
        self._name_edited = True

    def _on_option_toggled(self, checked: bool) -> None:
        if checked:
            self._sync_destination()

    def _choose_directory(self) -> None:
        current = self.directory_edit.text().strip() or str(Path(self.context.source_path).parent)
        chosen = QFileDialog.getExistingDirectory(self, "选择输出目录", current)
        if chosen:
            self.directory_edit.setText(chosen)

    def _sync_destination(self) -> None:
        option = self.selected_option()
        if option is None:
            self.extension_label.setText("")
            self.path_preview.setText("没有可用的输出选项。")
            self.accept_button.setEnabled(False)
            return
        self.extension_label.setText(FORMAT_EXTENSIONS[option.format])
        if not self._name_edited:
            name = Path(default_output_name(
                self.context.source_path, self.context.window,
                option.format, option.scope,
            )).stem
            blocker = self.name_edit.blockSignals(True)
            self.name_edit.setText(name)
            self.name_edit.blockSignals(blocker)
        self.path_preview.setText(f"将保存为：{self.output_path()}")
        self.accept_button.setEnabled(self._output_issue() is None)

    def _output_issue(self) -> str | None:
        if not self.directory_edit.text().strip():
            return "请选择输出目录。"
        issue = validate_output_path(self.output_path(), self.context.source_path)
        if not issue.ok:
            return issue.reason
        return None

    def accept(self) -> None:  # noqa: D401 - Qt override
        option = self.selected_option()
        if option is None:
            QMessageBox.warning(self, "数据截取", "当前没有可用的输出选项。")
            return
        issue = self._output_issue()
        if issue:
            QMessageBox.warning(self, "数据截取", issue)
            return
        output_path = self.output_path()
        if output_path.exists():
            answer = QMessageBox.question(
                self,
                "文件已存在",
                f"输出文件已存在，是否覆盖？\n{output_path}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        save_last_mode(self._settings, self.context.kind.value, option)
        super().accept()


class CutoutProgressDialog(QDialog):
    """Modal, cancellable progress window for one running cut-out job."""

    cancel_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("数据截取")
        self.setModal(True)
        self.setMinimumWidth(440)
        self.setProperty("uiDialog", "dataCutoutProgress")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(8)

        self.status_label = QLabel("正在准备截取…")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(14)
        layout.addWidget(self.progress_bar)

        self.detail_label = QLabel("")
        self.detail_label.setProperty("uiTextRole", "secondary")
        layout.addWidget(self.detail_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setProperty("uiRole", "secondary")
        self.cancel_button.clicked.connect(self.request_cancel)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

    def set_status(self, text: str) -> None:
        if text:
            self.status_label.setText(text)

    def set_progress(self, text: str, value: int, total: int) -> None:
        self.set_status(text)
        if total > 1:
            if self.progress_bar.maximum() != total:
                self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(max(0, min(int(value), total)))
            self.detail_label.setText(f"{int(value):,} / {total:,}")
        else:
            self.progress_bar.setRange(0, 0)
            self.detail_label.setText("")

    def request_cancel(self) -> None:
        if not self.cancel_button.isEnabled():
            return
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("正在取消…")
        self.status_label.setText("正在安全停止截取…")
        self.cancel_requested.emit()

    def reject(self) -> None:  # noqa: D401 - Qt override
        self.request_cancel()


__all__ = [
    "CutoutDialog",
    "CutoutProgressDialog",
    "INTRO_TEXT",
    "LAST_MODE_KEY",
    "load_last_mode",
    "save_last_mode",
]
