"""Merged VET_DATA main window."""

import json
from dataclasses import replace
import os
from pathlib import Path
from uuid import uuid4

from PyQt6.QtWidgets import (
    QDialog, QFileDialog, QGroupBox, QInputDialog, QLabel, QMessageBox, QPushButton,
    QScrollArea, QSplitter,
)

from .blf_slice_dialog import BlfSliceDialog
from .blf_slice_progress import (
    BlfSliceProgressDialog, BlfSliceResultDialog, BlfSliceWorker,
    request_duplicate_selection,
)
from .calculated_signal import CalculationStatus
from .calculated_signal_config import (
    CalculatedSignalRestoreService,
    serialize_calculated_signal_config,
)
from .calculation_engine import CalculationEngine
from .collapsible import CollapsiblePanelsMixin
from .legacy import baseline
from .formula_editor import FormulaEditorDialog
from .formula_parser import FormulaError
from .formula_validator import parse_and_validate_formula
from .gui_state import (
    create_gui_settings, restore_collapsible_panel_state,
    restore_main_window_state, save_collapsible_panel_state,
    save_main_window_state,
)
from .math_channel import SearchableMathChannelDialog  # retains legacy compatibility
from .plot_panel import PlotPanelMixin
from .signal_panel import SignalPanelMixin
from .signal_resolver import SignalResolver
from .theme import DEFAULT_THEME, build_main_window_stylesheet
from .workers import (
    BusyLoadDialog, CanBusDetectionDialog, CanBusDetectionWorker,
    CsvExportSignal, CsvExportSnapshot, CsvExportWorker, DataLoadWorker,
    FormulaRestoreWorker, normalized_can_path,
)


class MDFPlotter(SignalPanelMixin, PlotPanelMixin, CollapsiblePanelsMixin, baseline.MDFPlotter):
    """26.09.01 baseline plus the v10 interaction enhancements."""
    def __init__(self, gui_settings=None):
        super().__init__()
        self.setWindowTitle("VET_DATA")
        self._data_load_worker = None; self._data_load_dialog = None
        self._can_bus_detection_worker = None; self._can_bus_detection_dialog = None
        self.can_bus_mapping = None
        self.can_detection_snapshot = None
        self._can_detection_generation = 0
        self._blf_slice_dialog = None; self._blf_slice_worker = None; self._blf_slice_progress = None
        self._blf_slice_result = None
        self._close_after_load = False; self._close_after_blf_slice = False
        self.calculated_signal_definitions = {}
        self._formula_restore_report = None
        self._formula_context_generation = 0
        self._formula_restore_worker = None
        self._formula_restore_background_enabled = True
        self._close_after_formula_restore = False
        self._formula_editor_dialog = None
        self._csv_export_worker = None
        self._csv_export_dialog = None
        self._close_after_csv_export = False
        self._close_after_arxml_conversion = False
        self.math_channel_button.setText("➕ 创建自定义公式计算通道")
        self.math_channel_button.setToolTip("使用多信号公式、数学函数、导数和积分创建通道")
        self._setup_signal_panel(); self._setup_plot_panel(); self._setup_collapsible_panels()
        self._setup_blf_slice_entry()
        self._gui_settings = gui_settings if gui_settings is not None else create_gui_settings()
        self._pending_splitter_sizes = restore_main_window_state(
            self, self.main_splitter, self._gui_settings
        )
        restore_collapsible_panel_state(self.collapsible_groups, self._gui_settings)
        self._setup_main_window_theme()

    def _setup_main_window_theme(self):
        tokens = DEFAULT_THEME
        self.setProperty("uiSurface", "main")
        self.setStyleSheet(build_main_window_stylesheet(tokens))
        self.layout().setSpacing(tokens.spacing.small)
        self.left_panel_content.layout().setContentsMargins(
            tokens.spacing.small, tokens.spacing.small,
            tokens.spacing.small, tokens.spacing.small,
        )
        self.left_panel_content.layout().setSpacing(tokens.spacing.small)
        self.top_action_layout.setSpacing(tokens.spacing.xsmall)
        self.top_primary_action_layout.setSpacing(tokens.spacing.xsmall)
        self.bottom_action_layout.setSpacing(tokens.spacing.xsmall)
        self.search_box.setProperty("uiControl", "main")
        self.search_box.setStyleSheet("")
        self.current_file_label.setProperty("uiTextRole", "secondary")
        self.main_splitter.setProperty("uiSplitter", "main")
        for group in self.findChildren(QGroupBox):
            if group.title() in {"可用信号", "CAN通道配置", "GPS轨迹图", "信号曲线"}:
                group.setProperty("uiPanel", "main")
                group.setStyleSheet("")
        primary_buttons = (self.load_file_button, self.plot_button)
        secondary_buttons = (
            self.select_all_button, self.deselect_all_button,
            self.save_config_button, self.load_config_button,
            self.math_channel_button, self.export_button,
            self.save_data_button, self.blf_slice_button,
            self.show_map_button, self.arxml2dbc_btn,
        )
        for button in primary_buttons:
            button.setProperty("uiRole", "primary")
            button.setStyleSheet("")
        for button in secondary_buttons:
            button.setProperty("uiRole", "secondary")
            button.setStyleSheet("")

    def showEvent(self, event):
        super().showEvent(event)
        if self._pending_splitter_sizes is not None:
            self.main_splitter.setSizes(list(self._pending_splitter_sizes))
            self._pending_splitter_sizes = None

    def export_selected_signals_to_csv(self):
        if self._csv_export_worker is not None and self._csv_export_worker.isRunning():
            QMessageBox.information(self, "导出CSV", "已有 CSV 导出任务正在运行。")
            return
        selected_signals = self.get_selected_signals()
        if not selected_signals:
            QMessageBox.warning(self, "导出CSV", "请选择至少一个信号进行导出。")
            return
        file_name, _ = QFileDialog.getSaveFileName(
            self, "导出CSV", "selected_signals.csv", "CSV Files (*.csv)"
        )
        if not file_name:
            return

        target_frequency, ok = QInputDialog.getDouble(
            self, "重采样频率", "请输入目标重采样频率 (Hz):",
            value=10.0, min=0.1, max=10000.0, decimals=2,
        )
        if not ok or target_frequency <= 0:
            return

        export_signals = []
        for unique_key in selected_signals:
            signal_info = self.signals.get(unique_key)
            if not signal_info:
                continue
            signal = self._get_signal(signal_info)
            if signal is not None and len(signal.timestamps) > 0:
                export_signals.append(CsvExportSignal(
                    str(signal_info["display_name"]), signal
                ))
        if not export_signals:
            QMessageBox.warning(self, "导出CSV", "无法获取信号数据。")
            return

        snapshot = CsvExportSnapshot(
            str(Path(file_name).resolve()), float(target_frequency),
            tuple(export_signals),
        )
        worker = CsvExportWorker(snapshot, self)
        dialog = BusyLoadDialog(self)
        dialog.setWindowTitle("导出 CSV")
        dialog.set_status("正在准备 CSV 导出…")
        worker.progress.connect(lambda _value, text: dialog.set_status(text))
        worker.completed.connect(self._complete_csv_export)
        worker.error.connect(self._fail_csv_export)
        worker.cancelled.connect(self._cancel_csv_export)
        worker.finished.connect(lambda: self._cleanup_csv_export(worker, dialog))
        self._csv_export_worker = worker
        self._csv_export_dialog = dialog
        worker.start()
        dialog.show()

    def _complete_csv_export(self, output_path):
        if not self._close_after_csv_export:
            QMessageBox.information(
                self, "导出CSV成功", f"成功导出信号到 {output_path}"
            )

    def _fail_csv_export(self, message):
        if not self._close_after_csv_export:
            QMessageBox.critical(self, "导出CSV失败", message)

    def _cancel_csv_export(self):
        pass

    def _cleanup_csv_export(self, worker, dialog):
        if worker is not self._csv_export_worker:
            dialog.close()
            dialog.deleteLater()
            worker.deleteLater()
            return
        self._csv_export_worker = None
        self._csv_export_dialog = None
        dialog.close()
        dialog.deleteLater()
        worker.deleteLater()
        if self._close_after_csv_export:
            self._close_after_csv_export = False
            self.close()

    def _formula_context_token(self):
        return (self._formula_context_generation, self.mdf_path, id(self.mdf_file))

    def _formula_resolver(self):
        return SignalResolver(
            self.signals,
            data_source=self.mdf_file,
            data_path=self.mdf_path,
            can_data=self.can_parsed_data,
            legacy_math_data=self.custom_math_data,
        )

    @staticmethod
    def _calculate_formula_definition(definition, validated, resolver):
        return CalculationEngine().calculate(definition, validated, resolver)
    def _preview_formula_definition(self, definition):
        validated = parse_and_validate_formula(
            definition.normalized_formula or definition.user_formula,
            definition.signal_tokens,
        )
        return CalculationEngine().calculate(
            definition, validated, self._formula_resolver()
        )

    def _register_calculated_result(self, definition, result):
        """Publish one calculated result through the existing unified catalog."""
        stable_id = definition.stable_id
        self.custom_math_data[stable_id] = {
            "timestamps": result.timestamps,
            "samples": result.samples,
            "unit": definition.result_unit,
        }
        self.signals[stable_id] = {
            "name": stable_id,
            "display_name": f"🧮 {definition.display_name}",
            "group": -99,
            "channel": -99,
            "source": "Calculated",
            "unit": definition.result_unit,
            "comment": definition.comment,
            "calculated_definition": definition.to_dict(),
        }

    def _signal_config_data(self):
        config = serialize_calculated_signal_config(
            tuple(self.calculated_signal_definitions.values())
        )
        config.update({
            "selected_signals": self.get_selected_signals(),
            "gps_lat_key": self.lat_combo.currentData(),
            "gps_lon_key": self.lon_combo.currentData(),
            "bus_config": {
                str(bus_id): {
                    "name": data.get("name", f"Bus {bus_id}"),
                    "protocol": self.bus_protocols.get(bus_id, ""),
                }
                for bus_id, data in self.can_bus_data.items()
            },
        })
        return config

    def save_signal_config(self):
        if not self.get_selected_signals() and not self.calculated_signal_definitions:
            QMessageBox.warning(self, "保存", "未选择任何信号。")
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self, "保存配置", "", "JSON Files (*.json)"
        )
        if not file_path:
            return
        try:
            with open(file_path, "w", encoding="utf-8") as stream:
                json.dump(self._signal_config_data(), stream, ensure_ascii=False, indent=4)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))

    def _formula_restore_inputs(self):
        managed_ids = set(self.calculated_signal_definitions)
        resolver = SignalResolver(
            {key: value for key, value in self.signals.items() if key not in managed_ids},
            data_source=self.mdf_file,
            data_path=self.mdf_path,
            can_data=self.can_parsed_data,
            legacy_math_data={
                key: value for key, value in self.custom_math_data.items()
                if key not in managed_ids
            },
        )
        return managed_ids, resolver

    def _apply_formula_restore_report(self, report, managed_ids):
        for stable_id in managed_ids:
            self.signals.pop(stable_id, None)
            self.custom_math_data.pop(stable_id, None)
        self.calculated_signal_definitions.clear()
        self.calculated_signal_definitions.update(report.definitions)
        for stable_id, result in report.results.items():
            self._register_calculated_result(report.definitions[stable_id], result)
        self._formula_restore_report = report

    def _restore_calculated_channels(self, config_data):
        """Synchronous service entry retained for tests and non-GUI callers."""
        managed_ids, resolver = self._formula_restore_inputs()
        report = CalculatedSignalRestoreService().restore(config_data, resolver)
        self._apply_formula_restore_report(report, managed_ids)
        return report
    def _apply_saved_bus_config(self, config_data):
        for bus_id_str, config in config_data.get("bus_config", {}).items():
            bus_id = int(bus_id_str)
            if bus_id not in self.can_bus_data:
                continue
            self.can_bus_data[bus_id]["name"] = config.get("name", f"Bus {bus_id}")
            protocol = config.get("protocol", "")
            if protocol:
                self.bus_protocols[bus_id] = protocol
                self.db_per_bus.setdefault(bus_id, {})
            if bus_id in self.bus_config_widgets:
                widget = self.bus_config_widgets[bus_id]
                widget.name_edit.setText(self.can_bus_data[bus_id]["name"])
                if protocol:
                    widget.set_protocol_display(os.path.basename(protocol))
                    widget.protocol_path = protocol

    @staticmethod
    def _restore_summary(report):
        lines = [f"成功恢复：{len(report.results)}", f"失败：{len(report.failures)}"]
        for stable_id, failure in report.failures.items():
            definition = report.definitions.get(stable_id)
            name = definition.display_name if definition else stable_id
            lines.extend(("", f"{name}：", failure.message))
        if report.migrated_legacy:
            lines.extend(("", "已将旧二元计算配置迁移为 Config V2 定义。"))
        return "\n".join(lines)

    def _finish_loaded_config(self, report, managed_ids, config_data, context):
        if context != self._formula_context_token():
            return
        self._apply_formula_restore_report(report, managed_ids)
        self._apply_saved_bus_config(config_data)
        self.refresh_signal_list_ui()
        selected_keys = set(config_data.get("selected_signals", ()))
        for unique_key, widgets in self.signal_widgets.items():
            widgets["checkbox"].setChecked(unique_key in selected_keys)
        for combo, config_key in (
            (self.lat_combo, "gps_lat_key"), (self.lon_combo, "gps_lon_key")
        ):
            saved_key = config_data.get(config_key)
            index = combo.findData(saved_key) if saved_key else -1
            if index >= 0:
                combo.setCurrentIndex(index)
        self.plot_selected_signals()
        summary = self._restore_summary(report)
        if report.failures:
            QMessageBox.warning(self, "计算通道恢复报告", summary)
        else:
            QMessageBox.information(self, "计算通道恢复报告", summary)

    def _formula_restore_failed(self, message, context):
        if context == self._formula_context_token():
            QMessageBox.critical(self, "加载失败", f"配置文件解析或恢复失败: {message}")

    def _formula_restore_finished(self):
        self._formula_restore_worker = None
        loading_data = self._data_load_worker is not None and self._data_load_worker.isRunning()
        self.load_config_button.setEnabled(not loading_data)
        self.load_config_button.setText("加载配置")
        if self._close_after_formula_restore:
            self._close_after_formula_restore = False
            self.close()

    def load_signal_config(self):
        if self._formula_restore_worker is not None and self._formula_restore_worker.isRunning():
            return
        file_name, _ = QFileDialog.getOpenFileName(
            self, "加载信号配置", "", "JSON Files (*.json)"
        )
        if not file_name:
            return
        if not self.signals:
            QMessageBox.warning(self, "错误", "请先加载数据文件，再加载配置。")
            return
        try:
            with open(file_name, "r", encoding="utf-8") as stream:
                config_data = json.load(stream)
            managed_ids, resolver = self._formula_restore_inputs()
            context = self._formula_context_token()
            if not self._formula_restore_background_enabled:
                report = CalculatedSignalRestoreService().restore(config_data, resolver)
                self._finish_loaded_config(report, managed_ids, config_data, context)
                return
            self.load_config_button.setEnabled(False)
            self.load_config_button.setText("正在恢复……")
            worker = FormulaRestoreWorker(config_data, resolver)
            self._formula_restore_worker = worker
            worker.completed.connect(
                lambda report: self._finish_loaded_config(
                    report, managed_ids, config_data, context
                )
            )
            worker.failed.connect(
                lambda message: self._formula_restore_failed(message, context)
            )
            worker.finished.connect(self._formula_restore_finished)
            worker.start_tracked()
        except Exception as exc:
            QMessageBox.critical(self, "加载失败", f"配置文件解析或恢复失败: {exc}")
    def create_math_channel(self):
        """Open the V1 formula editor; the legacy binary dialog stays internal."""
        if not self.signals:
            QMessageBox.warning(self, "计算通道", "当前未加载任何有效数据文件。")
            return
        dialog = FormulaEditorDialog(
            self.signals,
            self,
            preview_callback=self._preview_formula_definition,
            background_callback=lambda definition, validated: self._calculate_formula_definition(
                definition, validated, self._formula_resolver()
            ),
            context_callback=self._formula_context_token,
        )
        self._formula_editor_dialog = dialog
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            display_name = dialog.name_edit.text().strip()
            if not display_name:
                QMessageBox.warning(self, "错误", "新信号通道的名称不能为空。")
                return
            existing_names = {
                item.display_name.casefold()
                for item in self.calculated_signal_definitions.values()
            }
            existing_names.update(
                str(info.get("display_name", key)).removeprefix("🧮 ").casefold()
                for key, info in self.signals.items()
                if key.startswith(("CALC_", "MATH_"))
            )
            if display_name.casefold() in existing_names:
                QMessageBox.warning(
                    self, "错误", f"计算通道名称 '{display_name}' 已存在，请换一个名称。"
                )
                return
            stable_id = f"CALC_{uuid4().hex}"
            if dialog.generated_result is not None:
                if dialog.generated_context != self._formula_context_token():
                    QMessageBox.warning(self, "计算已失效", "数据文件已变化，旧计算结果已丢弃。")
                    return
                definition = replace(dialog.generated_definition, stable_id=stable_id)
                result = dialog.generated_result
            else:
                # Compatibility path for non-interactive tests/custom dialog subclasses.
                definition = dialog.build_definition(stable_id)
                if definition is None:
                    QMessageBox.warning(self, "公式错误", dialog.diagnostic_output.toPlainText())
                    return
                result = self._preview_formula_definition(definition)
            if result.status is not CalculationStatus.SUCCESS:
                QMessageBox.critical(
                    self, "计算失败", result.error or "公式计算失败"
                )
                return
            self.calculated_signal_definitions[stable_id] = definition
            self._register_calculated_result(definition, result)
            self.refresh_signal_list_ui()
            self.signal_widgets[stable_id]["checkbox"].setChecked(True)
            QMessageBox.information(
                self, "计算通道", f"公式计算通道 '{definition.display_name}' 创建成功！"
            )
        except FormulaError as exc:
            QMessageBox.warning(self, "公式错误", exc.issue.message)
        except Exception as exc:
            QMessageBox.critical(self, "计算失败", str(exc))
        finally:
            self._formula_editor_dialog = None

    def _setup_blf_slice_entry(self):
        self.blf_slice_button = QPushButton("BLF 工况切片")
        self.blf_slice_button.setToolTip("独立选择 BLF 和工况表并执行自动切片")
        self.blf_slice_button.clicked.connect(self.open_blf_slice_dialog)
        layout = self._find_layout_containing(self.layout(), self.math_channel_button)
        if layout is None:
            raise RuntimeError("无法定位主窗口底部操作区")
        layout.addWidget(self.blf_slice_button, 1)

    @classmethod
    def _find_layout_containing(cls, layout, widget):
        if layout is None:
            return None
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item.widget() is widget:
                return layout
            nested = item.layout()
            found = cls._find_layout_containing(nested, widget) if nested else None
            if found is not None:
                return found
            child = item.widget()
            found = cls._find_layout_containing(child.layout(), widget) if child else None
            if found is not None:
                return found
            if isinstance(child, QScrollArea) and child.widget() is not None:
                found = cls._find_layout_containing(
                    child.widget().layout(), widget
                )
                if found is not None:
                    return found
            if isinstance(child, QSplitter):
                for child_index in range(child.count()):
                    splitter_child = child.widget(child_index)
                    found = cls._find_layout_containing(
                        splitter_child.layout(), widget
                    )
                    if found is not None:
                        return found
        return None

    def open_blf_slice_dialog(self):
        if self._blf_slice_worker is not None and self._blf_slice_worker.isRunning():
            return
        if self._blf_slice_dialog is not None and self._blf_slice_dialog.isVisible():
            self._blf_slice_dialog.raise_()
            self._blf_slice_dialog.activateWindow()
            return
        self.blf_slice_button.setEnabled(False)
        dialog = BlfSliceDialog(self)
        self._blf_slice_dialog = dialog
        dialog.task_confirmed.connect(self._start_blf_slice)
        dialog.finished.connect(self._blf_slice_input_closed)
        dialog.show()

    def _blf_slice_input_closed(self):
        self._blf_slice_dialog = None
        if self._blf_slice_worker is None:
            self.blf_slice_button.setEnabled(True)

    def _start_blf_slice(self, task, table_result):
        worker = BlfSliceWorker(task, table_result, self)
        progress = BlfSliceProgressDialog(worker, self)
        self._blf_slice_worker, self._blf_slice_progress = worker, progress
        worker.duplicate_found.connect(lambda result: request_duplicate_selection(worker, result, self))
        worker.completed.connect(self._finish_blf_slice)
        worker.cancelled.connect(self._finish_blf_slice)
        worker.failed.connect(self._fail_blf_slice)
        worker.finished.connect(self._cleanup_blf_slice)
        progress.show()
        worker.start()

    def _finish_blf_slice(self, result):
        if self._blf_slice_progress is not None:
            self._blf_slice_progress.hide()
        self._show_blf_slice_result(result)

    def _fail_blf_slice(self, _message, result):
        if self._blf_slice_progress is not None:
            self._blf_slice_progress.hide()
        self._show_blf_slice_result(result)

    def _show_blf_slice_result(self, result):
        if self._blf_slice_result is not None:
            self._blf_slice_result.close()
            self._blf_slice_result.deleteLater()
        dialog = BlfSliceResultDialog(result, self)
        self._blf_slice_result = dialog
        dialog.finished.connect(self._clear_blf_slice_result)
        dialog.show()

    def _clear_blf_slice_result(self):
        self._blf_slice_result = None

    def _cleanup_blf_slice(self):
        worker = self._blf_slice_worker
        self._blf_slice_worker = None
        if self._blf_slice_progress is not None:
            self._blf_slice_progress.deleteLater()
            self._blf_slice_progress = None
        self.blf_slice_button.setEnabled(True)
        if worker is not None:
            worker.deleteLater()
        if self._close_after_blf_slice:
            self._close_after_blf_slice = False
            self.close()

    def load_file_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择数据文件", "", "支持的数据文件 (*.mdf *.mf4 *.blf *.asc *.trc *.csv *.vbo);;所有文件 (*)")
        if not path: return
        previous_worker = self._can_bus_detection_worker
        if previous_worker is not None and previous_worker.isRunning():
            previous_worker.cancel()
        self._can_detection_generation += 1
        detection_generation = self._can_detection_generation
        self.can_detection_snapshot = None
        suffix = Path(path).suffix.lower()
        if suffix in {".blf", ".asc", ".trc"}:
            self._formula_context_generation += 1
            self.mdf_path = path; self.current_file_label.setText(f"当前文件: {Path(path).name}")
            self.can_bus_data.clear()
            self.can_bus_mapping = None
            self.set_buttons_enabled(False)
            dialog = CanBusDetectionDialog(self)
            worker = CanBusDetectionWorker(path, self)
            self._can_bus_detection_dialog = dialog
            self._can_bus_detection_worker = worker
            dialog.cancel_requested.connect(worker.cancel)
            worker.status.connect(dialog.set_status)
            worker.result.connect(
                lambda result, generation=detection_generation, owner=worker:
                self._finish_can_bus_detection(result, generation, owner)
            )
            worker.error.connect(
                lambda message, generation=detection_generation, owner=worker:
                self._fail_can_bus_detection(message, generation, owner)
            )
            worker.cancelled.connect(
                lambda generation=detection_generation, owner=worker:
                self._cancel_can_bus_detection(generation, owner)
            )
            worker.finished.connect(
                lambda generation=detection_generation, owner=worker, owner_dialog=dialog:
                self._cleanup_can_bus_detection(generation, owner, owner_dialog)
            )
            dialog.show()
            worker.start()
            return
        self.set_buttons_enabled(False)
        self._data_load_dialog = BusyLoadDialog(self); self._data_load_dialog.show()
        worker = DataLoadWorker(path, self); self._data_load_worker = worker
        worker.status.connect(self._data_load_dialog.set_status)
        worker.result.connect(self._finish_data_load)
        worker.error.connect(self._fail_data_load)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _finish_data_load(self, result):
        old = getattr(self, "mdf_file", None)
        if old is not None and hasattr(old, "close"):
            try: old.close()
            except Exception: pass
        self._formula_context_generation += 1
        self.mdf_path = result.path; self.mdf_file = result.data
        self.current_file_label.setText(f"当前文件: {Path(result.path).name}")
        self.signals.clear(); self.signals.update(result.signals)
        self.can_parsed_data.clear(); self.bus_signal_map.clear(); self.custom_math_data.clear()
        self.calculated_signal_definitions.clear()
        self._formula_restore_report = None
        self.can_bus_data.clear(); self.db_per_bus.clear(); self.bus_protocols.clear(); self.can_bus_signals.clear()
        self.can_bus_mapping = None
        self.can_detection_snapshot = None
        self.clear_bus_config(); self._sig_value_cache.clear(); self._user_signal_order.clear()
        self.refresh_signal_list_ui(); self.load_config()
        self._close_load_dialog(); self._data_load_worker = None

    def _fail_data_load(self, message):
        self._close_load_dialog(); self._data_load_worker = None
        self.set_buttons_enabled(False)
        QMessageBox.critical(self, "加载文件失败", message)

    def _close_load_dialog(self):
        if self._data_load_dialog is not None:
            self._data_load_dialog.close(); self._data_load_dialog.deleteLater(); self._data_load_dialog = None

    def _is_current_can_detection(self, generation, worker):
        return (
            generation == self._can_detection_generation
            and worker is self._can_bus_detection_worker
        )

    def _finish_can_bus_detection(self, result, generation, worker):
        if not self._is_current_can_detection(generation, worker):
            return
        if result.snapshot.file_identity.path != normalized_can_path(self.mdf_path):
            return
        self.can_bus_data.clear()
        self.can_bus_data.update(result)
        self.can_bus_mapping = result.mapping
        self.can_detection_snapshot = result.snapshot
        self.bus_loaded = True
        self._print_bus_info()
        self.create_bus_config_ui()
        if Path(self.mdf_path).suffix.casefold() == ".blf":
            can_group = self.collapsible_groups.get("CAN通道配置")
            if can_group is not None:
                can_group.setChecked(True)

    def _fail_can_bus_detection(self, message, generation, worker):
        if not self._is_current_can_detection(generation, worker):
            return
        self.can_detection_snapshot = None
        QMessageBox.critical(self, "加载文件失败", message)

    def _cancel_can_bus_detection(self, generation, worker):
        if not self._is_current_can_detection(generation, worker):
            return
        self.can_detection_snapshot = None
        self.current_file_label.setText("当前文件: 未选择")

    def _cleanup_can_bus_detection(self, generation, worker, dialog):
        if not self._is_current_can_detection(generation, worker):
            dialog.close()
            dialog.deleteLater()
            worker.deleteLater()
            return
        self._can_bus_detection_worker = None
        if self._can_bus_detection_dialog is not None:
            self._can_bus_detection_dialog.close()
            self._can_bus_detection_dialog.deleteLater()
            self._can_bus_detection_dialog = None
        self.set_buttons_enabled(True)
        if worker is not None:
            worker.deleteLater()
        if self._close_after_load:
            self._close_after_load = False
            self.close()

    def closeEvent(self, event):
        converter = getattr(self, "_arxml_converter", None)
        arxml_worker = getattr(converter, "_conversion_worker", None)
        if arxml_worker is not None and arxml_worker.isRunning():
            if not self._close_after_arxml_conversion:
                self._close_after_arxml_conversion = True
                arxml_worker.finished.connect(self.close)
            converter._close_after_conversion = True
            converter.show()
            converter.raise_()
            event.ignore()
            return
        self._close_after_arxml_conversion = False
        if self._csv_export_worker is not None and self._csv_export_worker.isRunning():
            self._csv_export_worker.cancel()
            self._close_after_csv_export = True
            if self._csv_export_dialog is not None:
                self._csv_export_dialog.set_status("正在安全停止 CSV 导出…")
            event.ignore()
            return
        if self._can_bus_detection_worker is not None and self._can_bus_detection_worker.isRunning():
            self._can_bus_detection_worker.cancel()
            self._close_after_load = True
            if self._can_bus_detection_dialog is not None:
                self._can_bus_detection_dialog.set_status("正在安全停止 CAN 总线探测…")
            event.ignore()
            return
        if self._blf_slice_worker is not None and self._blf_slice_worker.isRunning():
            self._blf_slice_worker.cancel()
            self._close_after_blf_slice = True
            if self._blf_slice_progress is not None:
                self._blf_slice_progress.phase_label.setText("正在安全停止 BLF 切片…")
            event.ignore()
            return
        if self._formula_restore_worker is not None and self._formula_restore_worker.isRunning():
            if not self._close_after_formula_restore:
                self._close_after_formula_restore = True
                self._formula_restore_worker.finished.connect(self.close)
            self.load_config_button.setText("正在安全完成配置恢复……")
            event.ignore()
            return
        if self._data_load_worker is not None and self._data_load_worker.isRunning():
            # MDF constructors cannot be interrupted safely.  Keep the window
            # alive until the worker exits instead of destroying a live QThread.
            self._data_load_worker.cancel()
            if not self._close_after_load:
                self._close_after_load = True
                self._data_load_worker.finished.connect(self.close)
            if self._data_load_dialog is not None:
                self._data_load_dialog.set_status("正在安全停止文件读取…")
            event.ignore()
            return
        save_main_window_state(self, self.main_splitter, self._gui_settings)
        save_collapsible_panel_state(self.collapsible_groups, self._gui_settings)
        self._close_load_dialog()
        super().closeEvent(event)
