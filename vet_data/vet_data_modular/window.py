"""Merged VET_DATA main window."""

import json
from dataclasses import replace
import hashlib
import os
from pathlib import Path
from uuid import uuid4

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (
    QDialog, QFileDialog, QGroupBox, QInputDialog, QLabel, QMessageBox, QPushButton,
    QScrollArea, QSplitter, QTabWidget,
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
from .signal_panel import KEY_ROLE, SignalPanelMixin
from .signal_resolver import SignalResolver
from .theme import DEFAULT_THEME, build_main_window_stylesheet
from .workers import (
    BusyLoadDialog, CanBusDetectionDialog, CanBusDetectionWorker,
    CanRawMessageCache, CsvExportSignal, CsvExportSnapshot, CsvExportWorker, DataLoadWorker,
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
        self._cutout_worker = None
        self._cutout_progress = None
        self._cutout_option = None
        self._close_after_cutout = False
        self._close_after_arxml_conversion = False
        self._pending_can_config = None
        self._pending_signal_restore = None
        self._can_task_generation = 0
        self._can_raw_message_cache = {}
        self._reusable_can_config = None
        self._selected_can_config = {}
        self.math_channel_button.setText("公式")
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
        self.quick_actions_layout.setSpacing(tokens.spacing.xsmall)
        self.search_box.setProperty("uiControl", "main")
        self.search_box.setStyleSheet("")
        self.plot_mode_combo.setProperty("uiControl", "main")
        self.current_file_label.setProperty("uiTextRole", "secondary")
        self.main_splitter.setProperty("uiSplitter", "main")
        for group in self.findChildren(QGroupBox):
            if group.title() in {"可用信号", "CAN通道配置", "信号曲线"}:
                group.setProperty("uiPanel", "main")
                group.setStyleSheet("")
        primary_buttons = (self.load_file_button, self.plot_button)
        secondary_buttons = (
            self.select_all_button, self.deselect_all_button,
            self.save_config_button, self.load_config_button,
            self.math_channel_button, self.export_button,
            self.save_data_button, self.blf_slice_button,
            self.arxml2dbc_btn,
        )
        for button in primary_buttons:
            button.setProperty("uiRole", "primary")
            button.setStyleSheet("")
        for button in secondary_buttons:
            button.setProperty("uiRole", "secondary")
            button.setStyleSheet("")
        self.show_map_button.setProperty("uiRole", "stateToggle")
        for shortcut in (
            self.load_file_button, self.plot_button, self.math_channel_button,
            self.single_cursor_button, self.dual_cursor_button,
            self.plot_mode_combo,
        ):
            shortcut.setFixedHeight(36)
        self._update_cursor_button_icons()

    def _update_cursor_button_icons(self):
        inactive = DEFAULT_THEME.colors.text_primary
        active = DEFAULT_THEME.colors.surface
        self.single_cursor_button.setIcon(baseline._quick_action_icon(
            "single", active if self.single_cursor_button.isChecked() else inactive
        ))
        self.dual_cursor_button.setIcon(baseline._quick_action_icon(
            "dual", active if self.dual_cursor_button.isChecked() else inactive
        ))

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
        selected, identities = self._selected_can_config_payload()
        config.update({
            "selected_signals": selected,
            "signal_identities": identities,
            "gps_lat_key": self.lat_combo.currentData(),
            "gps_lon_key": self.lon_combo.currentData(),
            "bus_config": {
                str(bus_id): {
                    "name": data.get("name", f"Bus {bus_id}"),
                    "protocol": self.bus_protocols.get(bus_id, ""),
                    "raw_channel": (
                        self.can_bus_mapping.bus_to_raw.get(bus_id)
                        if self.can_bus_mapping is not None else None
                    ),
                }
                for bus_id, data in self.can_bus_data.items()
            },
        })
        return config

    def save_signal_config(self):
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
        unavailable = []
        for bus_id_str, config in config_data.get("bus_config", {}).items():
            bus_id = self._resolve_saved_bus_id(bus_id_str, config)
            if bus_id not in self.can_bus_data:
                continue
            self.can_bus_data[bus_id]["name"] = config.get("name", f"Bus {bus_id}")
            protocol = config.get("protocol", "")
            if protocol:
                self.bus_protocols[bus_id] = protocol
                self.db_per_bus.setdefault(bus_id, {})
                if not os.path.isfile(protocol) or not os.access(protocol, os.R_OK):
                    unavailable.append((bus_id, protocol))
            if bus_id in self.bus_config_widgets:
                widget = self.bus_config_widgets[bus_id]
                widget.name_edit.setText(self.can_bus_data[bus_id]["name"])
                if protocol:
                    widget.set_protocol_display(os.path.basename(protocol))
                    widget.protocol_path = protocol
        return unavailable

    def _resolve_saved_bus_id(self, saved_bus_id, config):
        saved_bus_id = int(saved_bus_id)
        raw_channel = config.get("raw_channel") if isinstance(config, dict) else None
        if raw_channel is not None and self.can_bus_mapping is not None:
            return self.can_bus_mapping.raw_to_bus.get(int(raw_channel), saved_bus_id)
        return saved_bus_id

    def _saved_configured_bus_ids(self, config_data):
        return {
            self._resolve_saved_bus_id(bus_id, item)
            for bus_id, item in config_data.get("bus_config", {}).items()
            if isinstance(item, dict) and item.get("protocol")
            and self._resolve_saved_bus_id(bus_id, item) in self.can_bus_data
        }

    @staticmethod
    def _identity_token(identity, bus_id=None):
        if isinstance(identity, list):
            if len(identity) == 1 and isinstance(identity[0], dict):
                return MDFPlotter._identity_token(identity[0], bus_id)
            return tuple(sorted(
                MDFPlotter._identity_token(item, bus_id)
                for item in identity
                if isinstance(item, dict)
            ))
        if not isinstance(identity, dict):
            return None
        channel = (
            ("raw", int(identity["raw_channel"]))
            if identity.get("raw_channel") is not None
            else ("bus", int(bus_id if bus_id is not None else identity.get("bus_id", -1)))
        )
        return (
            channel,
            int(identity.get("frame_id", -1)),
            bool(identity.get("is_extended", False)),
            str(identity.get("message_name", "")),
            str(identity.get("signal_name", "")),
        )

    @classmethod
    def _identity_alias_tokens(cls, identity, bus_id=None):
        """Accept older bus-id identities while preferring raw-channel identity."""
        primary = cls._identity_token(identity, bus_id)
        aliases = [primary] if primary is not None else []
        if isinstance(identity, dict) and identity.get("raw_channel") is not None:
            legacy = dict(identity)
            legacy.pop("raw_channel", None)
            aliases.append(cls._identity_token(legacy, bus_id))
        elif isinstance(identity, list) and identity:
            legacy_items = [dict(item) for item in identity if isinstance(item, dict)]
            for item in legacy_items:
                item.pop("raw_channel", None)
            if legacy_items:
                aliases.append(cls._identity_token(legacy_items, bus_id))
        return tuple(dict.fromkeys(token for token in aliases if token is not None))

    def _selection_record_token(self, key, identity):
        token = self._identity_token(identity) if identity else None
        return token if token is not None else ("legacy", str(key))

    def _load_selected_can_config(self, selected_keys, identity_map=None):
        identity_map = identity_map or {}
        self._selected_can_config.clear()
        for key in selected_keys or ():
            identity = identity_map.get(key)
            token = self._selection_record_token(key, identity)
            record = self._selected_can_config.setdefault(token, {})
            record.update({"key": key, "identity": identity})

    @staticmethod
    def _enrich_config_identities(config_data):
        bus_config = config_data.get("bus_config", {})
        identities = config_data.get("signal_identities", {})
        for value in identities.values():
            items = value if isinstance(value, list) else [value]
            for identity in items:
                if not isinstance(identity, dict) or identity.get("raw_channel") is not None:
                    continue
                bus_id = identity.get("bus_id")
                bus = bus_config.get(str(bus_id), {}) if bus_id is not None else {}
                if isinstance(bus, dict) and bus.get("raw_channel") is not None:
                    identity["raw_channel"] = int(bus["raw_channel"])
        return identities

    def _sync_selected_can_config(self):
        visible = {
            key for key, info in self.signals.items()
            if info.get("signal_type") == "can" or key.startswith("CAN_")
        }
        selected = set(self.get_selected_signals())
        for key in selected & visible:
            info = self.signals.get(key, {})
            identity = info.get("signal_identity") or info.get("config_identity")
            saved_key = info.get("selection_key", key)
            token = self._selection_record_token(saved_key, identity)
            record = self._selected_can_config.setdefault(token, {})
            record.update({"key": saved_key, "identity": identity, "ui_key": key})

    def _selected_can_config_payload(self):
        self._sync_selected_can_config()
        selected = list(self.get_selected_signals())
        identities = {}
        for record in self._selected_can_config.values():
            key = record["key"]
            if key not in selected:
                selected.append(key)
            if record.get("identity"):
                identities[key] = record["identity"]
        for key in self.get_selected_signals():
            info = self.signals.get(key, {})
            identity = info.get("signal_identity") or info.get("config_identity")
            if (info.get("signal_type") == "can" or key.startswith("CAN_")) and identity:
                identities[info.get("selection_key", key)] = identity
        return selected, identities

    def _selection_changed(self, replot=True, item=None):
        super()._selection_changed(replot=replot)
        if replot:
            if item is not None and item.checkState() != Qt.CheckState.Checked:
                self._deselect_can_signal(item.data(KEY_ROLE))
            self._sync_selected_can_config()

    def _deselect_can_signal(self, key):
        info = self.signals.get(key, {})
        saved_key = info.get("selection_key", key)
        identity = info.get("signal_identity") or info.get("config_identity")
        token = self._selection_record_token(saved_key, identity)
        self._selected_can_config.pop(token, None)

    def _apply_can_selection(self, selected_keys, identity_map=None, *, plot=True):
        selected_keys = list(dict.fromkeys(selected_keys or ()))
        identity_map = identity_map or {}
        self._selected_can_config.clear()
        self._load_selected_can_config(selected_keys, identity_map)
        current_by_identity = {}
        for key, info in self.signals.items():
            identity = info.get("signal_identity")
            if identity:
                for token in self._identity_alias_tokens(identity, info.get("bus_id")):
                    current_by_identity[token] = key
        self.refresh_signal_list_ui()
        undefined = []
        for saved_key in selected_keys:
            identity = identity_map.get(saved_key)
            if identity is None:
                identity = next((
                    record.get("identity")
                    for record in self._selected_can_config.values()
                    if record.get("key") == saved_key
                ), None)
            target = next((
                current_by_identity[token]
                for token in self._identity_alias_tokens(identity)
                if token in current_by_identity
            ), None) if identity else None
            if target is None and not identity and saved_key in self.signal_widgets:
                target = saved_key
            if target is None:
                target = self._add_unavailable_config_signal(saved_key, identity)
                undefined.append(saved_key)
            if target in self.signal_widgets:
                self.signals[target]["selection_key"] = saved_key
                self.signals[target]["config_identity"] = identity
                self.signal_widgets[target]["checkbox"].setChecked(True)
                token = self._selection_record_token(saved_key, identity)
                if token in self._selected_can_config:
                    self._selected_can_config[token]["ui_key"] = target
        if undefined:
            QMessageBox.warning(
                self, "部分信号当前不可用",
                "以下已选信号在当前解析目录中暂未匹配，仍保留为已选配置；请检查 DBC 和通道映射：\n"
                + "\n".join(undefined),
            )
        if plot:
            self.plot_selected_signals()
        return undefined

    def _identity_has_dbc_definition(self, identity):
        tokens = set(self._identity_alias_tokens(identity))
        for data in self.can_bus_data.values():
            stats = data.get("parse_stats", {})
            for candidates in stats.get("signal_identity", {}).values():
                for candidate in self._identity_alias_tokens(candidates):
                    if candidate in tokens:
                        return True
        return False

    def _add_unavailable_config_signal(self, saved_key, identity):
        token = self._selection_record_token(saved_key, identity)
        suffix = hashlib.sha1(repr(token).encode("utf-8")).hexdigest()[:12]
        key = saved_key if saved_key not in self.signals else f"CAN_CONFIG_{suffix}"
        if key not in self.signals:
            if isinstance(identity, list):
                identities = [item for item in identity if isinstance(item, dict)]
            elif isinstance(identity, dict):
                identities = [identity]
            else:
                identities = []
            label = next((item.get("signal_name") for item in identities if item.get("signal_name")), saved_key)
            bus_id = next((item.get("bus_id") for item in identities if item.get("bus_id") is not None), None)
            self.signals[key] = {
                "name": key,
                "display_name": f"🚌 {label}（当前不可用）",
                "group": -88,
                "channel": -88,
                "comment": "当前数据中尚无对应解析结果；已保留用户选择。",
                "bus_id": bus_id,
                "signal_type": "can",
                "signal_identity": identity,
                "config_identity": identity,
                "selection_key": saved_key,
                "available": False,
                "unavailable_reason": (
                    "当前 BLF 缺少对应报文或信号解码失败"
                    if self._identity_has_dbc_definition(identity)
                    else "DBC 中未定义或信号身份无法匹配"
                ),
            }
        self.refresh_signal_list_ui()
        return key

    def _restore_pending_can_config(self):
        config = self._pending_can_config
        if not config:
            return
        try:
            unavailable = self._apply_saved_bus_config(config)
            if unavailable:
                details = "\n".join(f"Bus {bus}: {path}" for bus, path in unavailable)
                QMessageBox.warning(
                    self, "DBC 不可用",
                    f"以下 DBC 文件不存在或不可读，请在 CAN 通道配置中重新选择：\n{details}",
                )
            self._pending_can_config = None
            self._pending_signal_restore = {
                "selected": list(config.get("selected_signals", ())),
                "identities": config.get("signal_identities", {}),
                "config": config,
                "expected_buses": self._saved_configured_bus_ids(config),
            }
            self._add_pending_selection_rows()
            self.auto_parse_enabled = True
            self._auto_parse_suppressed_for_restore = True
            readable_protocols = []
            for bus_id, protocol in self.bus_protocols.items():
                if os.path.isfile(protocol) and os.access(protocol, os.R_OK):
                    readable_protocols.append(bus_id)
                    self.on_bus_protocol_changed(bus_id, protocol)
            self._select_restored_can_catalog_signals(readable_protocols)
            expected_buses = set(self._pending_signal_restore["expected_buses"])
            for bus_id in expected_buses - set(readable_protocols):
                self.can_bus_data.setdefault(bus_id, {})["parse_state"] = "failed"
                self.can_bus_data[bus_id]["parse_error"] = "DBC 不存在或不可读"
                self._complete_pending_signal_restore(bus_id, plot=False)
            if not readable_protocols and not unavailable:
                QMessageBox.warning(
                    self, "需要选择 DBC",
                    "配置已读取并保留。请在每个 CAN 通道中重新选择可读取的 DBC，然后选择信号并点击“绘图”。",
                )
        except Exception as exc:
            self._pending_can_config = None
            QMessageBox.critical(self, "配置应用失败", f"CAN 配置无法应用：{exc}")

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

    def _select_restored_can_catalog_signals(self, readable_buses):
        """Restore checkboxes from DBC identities before deferred decoding."""
        pending = self._pending_signal_restore
        if not pending:
            return
        selected = pending.get("selected", ())
        identities = pending.get("identities", {})
        self._apply_can_selection(selected, identities, plot=False)
        needed_buses = {
            self.signals[key].get("bus_id")
            for key in selected if key in self.signals
            and self.signals[key].get("bus_id") is not None
        }
        pending["expected_buses"] = needed_buses & set(readable_buses)
        if not pending["expected_buses"]:
            for bus_id in tuple(readable_buses):
                self._complete_pending_signal_restore(bus_id, plot=False)

    def _finish_loaded_config(self, report, managed_ids, config_data, context):
        if context != self._formula_context_token():
            return
        self._apply_formula_restore_report(report, managed_ids)
        self._apply_saved_bus_config(config_data)
        self.refresh_signal_list_ui()
        selected_keys = set(config_data.get("selected_signals", ()))
        if Path(self.mdf_path).suffix.casefold() in {".blf", ".asc", ".trc"}:
            self._apply_can_selection(
                selected_keys, config_data.get("signal_identities", {}), plot=False
            )
        else:
            for unique_key, widgets in self.signal_widgets.items():
                widgets["checkbox"].setChecked(unique_key in selected_keys)
        for combo, config_key in (
            (self.lat_combo, "gps_lat_key"), (self.lon_combo, "gps_lon_key")
        ):
            saved_key = config_data.get(config_key)
            index = combo.findData(saved_key) if saved_key else -1
            if index >= 0:
                combo.setCurrentIndex(index)
        if Path(self.mdf_path).suffix.casefold() not in {".blf", ".asc", ".trc"}:
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
        try:
            with open(file_name, "r", encoding="utf-8") as stream:
                config_data = json.load(stream)
            if not isinstance(config_data, dict):
                raise ValueError("配置根节点必须是 JSON 对象")
            if not isinstance(config_data.get("selected_signals", []), list):
                raise ValueError("selected_signals 必须是数组")
            if any(not isinstance(key, str) for key in config_data.get("selected_signals", [])):
                raise ValueError("selected_signals 中的信号标识必须是字符串")
            if not isinstance(config_data.get("bus_config", {}), dict):
                raise ValueError("bus_config 必须是对象")
            for bus, item in config_data.get("bus_config", {}).items():
                try:
                    int(bus)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"bus_config 包含无效通道编号：{bus}") from exc
                if not isinstance(item, dict):
                    raise ValueError(f"bus_config[{bus}] 必须是对象")
                if any(not isinstance(item.get(field, ""), str) for field in ("name", "protocol")):
                    raise ValueError(f"bus_config[{bus}] 的 name/protocol 必须是字符串")
                if item.get("raw_channel") is not None and (
                    isinstance(item["raw_channel"], bool)
                    or not isinstance(item["raw_channel"], int)
                    or item["raw_channel"] < 0
                ):
                    raise ValueError(f"bus_config[{bus}] 的 raw_channel 必须是非负整数")
            if not isinstance(config_data.get("signal_identities", {}), dict):
                raise ValueError("signal_identities 必须是对象")
            if not isinstance(config_data.get("math_channels", {}), dict):
                raise ValueError("math_channels 必须是对象")
            self._enrich_config_identities(config_data)
            if not self.mdf_path:
                self._load_selected_can_config(config_data.get("selected_signals", ()), config_data.get("signal_identities", {}))
                self._pending_can_config = config_data
                QMessageBox.information(self, "配置已暂存", "配置已读取。加载 BLF 后会自动应用。")
                return
            if Path(self.mdf_path).suffix.casefold() in {".blf", ".asc", ".trc"} and not self.can_detection_snapshot:
                self._load_selected_can_config(config_data.get("selected_signals", ()), config_data.get("signal_identities", {}))
                self._pending_can_config = config_data
                QMessageBox.information(self, "配置已暂存", "配置已读取，将在当前 CAN 文件探测完成后自动应用。")
                return
            if Path(self.mdf_path).suffix.casefold() in {".blf", ".asc", ".trc"}:
                requested_protocols = {
                    int(bus): item.get("protocol", "")
                    for bus, item in config_data.get("bus_config", {}).items()
                    if isinstance(item, dict)
                }
                if any(
                    protocol and self.bus_protocols.get(bus, "") != protocol
                    for bus, protocol in requested_protocols.items()
                    if bus in self.can_bus_data
                ):
                    self._clear_can_parse_results_keep_detection()
                    self._load_selected_can_config(config_data.get("selected_signals", ()), config_data.get("signal_identities", {}))
                    self._pending_can_config = config_data
                    self._restore_pending_can_config()
                    return
            if Path(self.mdf_path).suffix.casefold() in {".blf", ".asc", ".trc"} and not self.signals:
                self._load_selected_can_config(config_data.get("selected_signals", ()), config_data.get("signal_identities", {}))
                self._pending_can_config = config_data
                self._restore_pending_can_config()
                return
            if not self.signals:
                raise ValueError("当前数据文件尚无可用信号，配置已保留但不能应用")
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
        self.blf_slice_button.setProperty("uiRole", "secondary")
        self.processing_ribbon_layout.insertWidget(
            max(0, self.processing_ribbon_layout.count() - 1),
            self.blf_slice_button,
        )

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
            if isinstance(child, QTabWidget):
                for page_index in range(child.count()):
                    found = cls._find_layout_containing(
                        child.widget(page_index).layout(), widget
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
        if self.can_parsed_data:
            self._sync_selected_can_config()
        self._reusable_can_selection = {
            record["key"]: record.get("identity")
            for record in self._selected_can_config.values()
        }
        if self.can_bus_data:
            self._reusable_can_config = {
                "bus_config": {
                    str(bus_id): {
                        "name": data.get("name", f"Bus {bus_id}"),
                        "protocol": self.bus_protocols.get(bus_id, ""),
                        "raw_channel": (
                            self.can_bus_mapping.bus_to_raw.get(bus_id)
                            if self.can_bus_mapping is not None else None
                        ),
                    }
                    for bus_id, data in self.can_bus_data.items()
                }
            }
        self._can_task_generation += 1
        self._parse_generation = self._can_task_generation
        self._clear_current_can_state()
        previous_worker = self._can_bus_detection_worker
        if previous_worker is not None and previous_worker.isRunning():
            previous_worker.cancel()
        self._can_detection_generation += 1
        detection_generation = self._can_detection_generation
        self.can_detection_snapshot = None
        suffix = Path(path).suffix.lower()
        if suffix in {".blf", ".asc", ".trc"}:
            self._add_pending_selection_rows()
            self._formula_context_generation += 1
            self.mdf_path = path; self.set_current_file_status(path)
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

    def _capture_current_can_selection(self):
        self._sync_selected_can_config()
        return {
            record["key"]: record.get("identity")
            for record in self._selected_can_config.values()
        }

    def _add_pending_selection_rows(self):
        for record in self._selected_can_config.values():
            saved_key = record["key"]
            identity = record.get("identity")
            token = self._selection_record_token(saved_key, identity)
            suffix = hashlib.sha1(repr(token).encode("utf-8")).hexdigest()[:12]
            key = saved_key if saved_key not in self.signals else f"CAN_CONFIG_{suffix}"
            items = identity if isinstance(identity, list) else [identity]
            items = [item for item in items if isinstance(item, dict)]
            label = next((item.get("signal_name") for item in items if item.get("signal_name")), saved_key)
            bus_id = next((item.get("bus_id") for item in items if item.get("bus_id") is not None), None)
            aliases = {
                alias for item in items
                for alias in self._identity_alias_tokens(item, bus_id)
            }
            existing_key = None
            for candidate_key, candidate in self.signals.items():
                candidate_identity = candidate.get("signal_identity") or candidate.get("config_identity")
                candidate_aliases = set(self._identity_alias_tokens(candidate_identity, candidate.get("bus_id")))
                if aliases and aliases.intersection(candidate_aliases):
                    existing_key = candidate_key
                    break
                if candidate.get("selection_key") == saved_key:
                    existing_key = candidate_key
                    break
            if existing_key is not None:
                record["ui_key"] = existing_key
                if existing_key in self.signal_widgets:
                    self.signal_widgets[existing_key]["checkbox"].setChecked(True)
                continue
            self.signals[key] = {
                "name": key,
                "display_name": f"🚌 {label}（等待当前 BLF 解析）",
                "group": -88,
                "channel": -88,
                "comment": "用户已选信号；当前 BLF 尚未解析。",
                "bus_id": bus_id,
                "signal_type": "can",
                "signal_identity": identity,
                "selection_key": saved_key,
                "available": False,
                "unavailable_reason": "等待当前 BLF 解析",
                "pending_selection_placeholder": True,
            }
            self.refresh_signal_list_ui()
            self.signal_widgets[key]["checkbox"].setChecked(True)
            record["ui_key"] = key

    def _clear_current_can_state(self):
        self._clear_cursor_positions_for_data_change()
        self._close_overlay_y_axis_managers(preserve_state=False)
        for worker in tuple(getattr(self, "parse_workers", {}).values()):
            try:
                if worker.isRunning():
                    worker.stop()
                    worker.wait()
            except RuntimeError:
                pass
        getattr(self, "parse_workers", {}).clear()
        getattr(self, "_parsing_bus_ids", set()).clear()
        getattr(self, "_can_raw_message_cache", {}).clear()
        self._pending_signal_restore = None
        self.clear_signal_curve_visibility()
        self.signals.clear()
        self.can_parsed_data.clear()
        self.bus_signal_map.clear()
        self.custom_math_data.clear()
        self.clear_bus_config()
        self.can_bus_data.clear()
        self.db_per_bus.clear()
        self.bus_protocols.clear()
        self.can_bus_signals.clear()
        self.can_bus_mapping = None
        self.can_detection_snapshot = None
        self.bus_loaded = False
        for plot in self.plot_widgets:
            self._delete_plot_widget(plot)
        self.plot_widgets.clear()
        self.master_viewbox = None
        self._sig_value_cache.clear()
        self.refresh_signal_list_ui()
        self._set_plot_empty_state("新文件尚未解析，等待 CAN 探测和信号解析")

    def _clear_can_parse_results_keep_detection(self):
        self._clear_cursor_positions_for_data_change()
        self._close_overlay_y_axis_managers(preserve_state=False)
        for worker in tuple(self.parse_workers.values()):
            try:
                if worker.isRunning():
                    worker.stop()
                    worker.wait()
            except RuntimeError:
                pass
        self.parse_workers.clear()
        self._parsing_bus_ids.clear()
        getattr(self, "_can_raw_message_cache", {}).clear()
        for key in tuple(self.can_parsed_data):
            self.can_parsed_data.pop(key, None)
        self.clear_signal_curve_visibility()
        self.bus_signal_map.clear()
        self.custom_math_data.clear()
        self.signals.clear()
        self._sig_value_cache.clear()
        for plot in self.plot_widgets:
            self._delete_plot_widget(plot)
        self.plot_widgets.clear()
        self.master_viewbox = None
        for dialog in tuple(self.progress_dialogs.values()):
            dialog.close()
            dialog.deleteLater()
        self.progress_dialogs.clear()
        self.refresh_signal_list_ui()
        self._set_plot_empty_state("正在按所选 DBC 重新解析当前 BLF")

    def _finish_data_load(self, result):
        old = getattr(self, "mdf_file", None)
        if old is not None and hasattr(old, "close"):
            try: old.close()
            except Exception: pass
        self._formula_context_generation += 1
        self._can_task_generation += 1
        self.mdf_path = result.path; self.mdf_file = result.data
        self.set_current_file_status(result.path)
        self.signals.clear(); self.signals.update(result.signals)
        self.can_parsed_data.clear(); self.bus_signal_map.clear(); self.custom_math_data.clear()
        self.calculated_signal_definitions.clear()
        self._formula_restore_report = None
        self.can_bus_data.clear(); self.db_per_bus.clear(); self.bus_protocols.clear(); self.can_bus_signals.clear()
        getattr(self, "_can_raw_message_cache", {}).clear()
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
        if self._pending_can_config is not None:
            self._restore_pending_can_config()
        elif self._reusable_can_config is not None:
            unavailable = self._apply_saved_bus_config(self._reusable_can_config or {})
            if unavailable:
                details = "\n".join(f"Bus {bus}: {path}" for bus, path in unavailable)
                QMessageBox.warning(
                    self, "DBC 不可用",
                    f"以下 DBC 文件不存在或不可读，请在 CAN 通道配置中重新选择：\n{details}",
                )
            self.auto_parse_enabled = True
            readable_protocols = []
            self._auto_parse_suppressed_for_restore = True
            self._pending_signal_restore = {
                "selected": list(self._reusable_can_selection or {}),
                "identities": {
                    key: identity
                    for key, identity in (self._reusable_can_selection or {}).items()
                    if identity
                },
                "expected_buses": self._saved_configured_bus_ids(self._reusable_can_config or {}),
            }
            self._add_pending_selection_rows()
            for bus_id in self.can_bus_data:
                protocol = self.bus_protocols.get(bus_id)
                if protocol and os.path.isfile(protocol) and os.access(protocol, os.R_OK):
                    readable_protocols.append(bus_id)
                    self.on_bus_protocol_changed(bus_id, protocol)
                elif protocol:
                    QMessageBox.warning(self, "DBC 不可用", f"无法读取 DBC：{protocol}\n请重新选择 DBC。")
            self._select_restored_can_catalog_signals(readable_protocols)
            if not readable_protocols and not unavailable:
                QMessageBox.warning(
                    self, "需要选择 DBC",
                    "当前会话没有可复用的 DBC。请在 CAN 通道配置中选择 DBC 并解析。",
                )
            for bus_id in self._pending_signal_restore["expected_buses"] - set(readable_protocols):
                self.can_bus_data.setdefault(bus_id, {})["parse_state"] = "failed"
                self.can_bus_data[bus_id]["parse_error"] = "DBC 不存在或不可读"
                self._complete_pending_signal_restore(bus_id, plot=False)
        if Path(self.mdf_path).suffix.casefold() == ".blf":
            self.left_tabs.setCurrentWidget(self.can_tab_page)

    def _fail_can_bus_detection(self, message, generation, worker):
        if not self._is_current_can_detection(generation, worker):
            return
        self.can_detection_snapshot = None
        QMessageBox.critical(self, "加载文件失败", message)

    def _cancel_can_bus_detection(self, generation, worker):
        if not self._is_current_can_detection(generation, worker):
            return
        self.can_detection_snapshot = None
        self.set_current_file_status()

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
        if self._cutout_worker is not None and self._cutout_worker.isRunning():
            self._cutout_worker.cancel()
            self._close_after_cutout = True
            if self._cutout_progress is not None:
                self._cutout_progress.set_status("正在安全停止数据截取…")
            event.ignore()
            return
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
