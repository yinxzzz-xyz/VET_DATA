"""Versioned calculated-signal configuration, migration, and restoration."""

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .calculated_signal import (
    CALCULATED_SIGNAL_SCHEMA_VERSION,
    CalculationStatus,
    CalculatedSignalDefinition,
    CalculatedSignalResult,
)
from .calculation_engine import CalculationEngine
from .dependency_graph import DependencyGraph
from .formula_parser import FormulaError
from .formula_validator import parse_and_validate_formula
from .signal_resolver import SignalResolver


class CalculatedSignalConfigError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LoadedCalculatedSignalConfig:
    definitions: tuple[CalculatedSignalDefinition, ...]
    migrated_legacy: bool = False


@dataclass(frozen=True)
class RestoreFailure:
    stable_id: str
    code: str
    message: str
    dependencies: tuple[str, ...] = ()


@dataclass
class RestoreReport:
    definitions: Mapping[str, CalculatedSignalDefinition]
    results: dict[str, CalculatedSignalResult] = field(default_factory=dict)
    failures: dict[str, RestoreFailure] = field(default_factory=dict)
    restore_order: tuple[str, ...] = ()
    migrated_legacy: bool = False


_REQUIRED_V2_FIELDS = {
    "schema_version", "stable_id", "display_name", "user_formula",
    "normalized_formula", "signal_tokens", "dependencies", "result_unit",
    "alignment_policy", "interpolation_policy", "numeric_policy", "comment",
}
_LEGACY_OPERATORS = {"+", "-", "*", "/"}


def serialize_calculated_signal_config(
    definitions: Sequence[CalculatedSignalDefinition],
) -> dict[str, Any]:
    """Serialize definitions only; calculated arrays are intentionally excluded."""
    ordered = sorted(definitions, key=lambda item: item.stable_id)
    return {
        "schema_version": CALCULATED_SIGNAL_SCHEMA_VERSION,
        "calculated_signals": [item.to_dict() for item in ordered],
    }


def load_calculated_signal_config(data: Mapping[str, Any]) -> LoadedCalculatedSignalConfig:
    if not isinstance(data, Mapping):
        raise CalculatedSignalConfigError("invalid_config", "计算通道配置必须是对象")
    if "schema_version" in data:
        version = data["schema_version"]
        if version != CALCULATED_SIGNAL_SCHEMA_VERSION:
            raise CalculatedSignalConfigError(
                "unsupported_schema_version", f"不支持的计算通道配置版本: {version}"
            )
        items = data.get("calculated_signals")
        if not isinstance(items, list):
            raise CalculatedSignalConfigError(
                "invalid_config", "V2 配置缺少 calculated_signals 列表"
            )
        return LoadedCalculatedSignalConfig(tuple(_definition_from_dict(item) for item in items))
    if "math_channels" in data:
        channels = data["math_channels"]
        if not isinstance(channels, Mapping):
            raise CalculatedSignalConfigError("invalid_legacy_config", "旧 math_channels 必须是对象")
        return LoadedCalculatedSignalConfig(
            tuple(migrate_legacy_definition(key, value) for key, value in sorted(channels.items())),
            migrated_legacy=True,
        )
    raise CalculatedSignalConfigError(
        "missing_schema_version", "配置缺少 schema_version，且不是可识别的旧二元配置"
    )


def migrate_legacy_definition(
    stable_id: str, legacy: Mapping[str, Any]
) -> CalculatedSignalDefinition:
    if not isinstance(legacy, Mapping):
        raise CalculatedSignalConfigError("invalid_legacy_config", f"{stable_id} 旧配置必须是对象")
    missing = [key for key in ("key_a", "key_b", "op") if key not in legacy]
    if missing:
        raise CalculatedSignalConfigError(
            "invalid_legacy_config", f"{stable_id} 缺少字段: {', '.join(missing)}"
        )
    operator = legacy["op"]
    if operator not in _LEGACY_OPERATORS:
        raise CalculatedSignalConfigError(
            "unsupported_legacy_operator", f"{stable_id} 不支持旧运算符: {operator}"
        )
    key_a, key_b = str(legacy["key_a"]), str(legacy["key_b"])
    bindings = {"S001": key_a, "S002": key_b}
    formula = f"S001 {operator} S002"
    validated = parse_and_validate_formula(formula, bindings)
    policy = str(legacy.get("policy", "nan"))
    if policy not in {"zero", "nan"}:
        raise CalculatedSignalConfigError(
            "unsupported_legacy_policy", f"{stable_id} 不支持旧除零策略: {policy}"
        )
    display_name = str(
        legacy.get("display_name", legacy.get("new_name", stable_id.removeprefix("MATH_")))
    )
    return CalculatedSignalDefinition(
        stable_id=str(stable_id), display_name=display_name, user_formula=formula,
        normalized_formula=validated.normalized_formula, signal_tokens=bindings,
        dependencies=validated.dependencies, result_unit=str(legacy.get("result_unit", "Math")),
        numeric_policy={"invalid": "nan", "divide_by_zero": policy},
        comment=str(legacy.get("comment", "Migrated legacy binary math channel")),
    )


def _definition_from_dict(item: Any) -> CalculatedSignalDefinition:
    if not isinstance(item, Mapping):
        raise CalculatedSignalConfigError("invalid_definition", "计算通道定义必须是对象")
    missing = sorted(_REQUIRED_V2_FIELDS - set(item))
    if missing:
        raise CalculatedSignalConfigError(
            "invalid_definition", f"计算通道定义缺少字段: {', '.join(missing)}"
        )
    if item["schema_version"] != CALCULATED_SIGNAL_SCHEMA_VERSION:
        raise CalculatedSignalConfigError(
            "unsupported_schema_version",
            f"定义 {item.get('stable_id', '')} 使用不支持的版本: {item['schema_version']}",
        )
    try:
        return CalculatedSignalDefinition(
            stable_id=str(item["stable_id"]), display_name=str(item["display_name"]),
            user_formula=str(item["user_formula"]),
            normalized_formula=str(item["normalized_formula"]),
            signal_tokens={str(k): str(v) for k, v in item["signal_tokens"].items()},
            dependencies=tuple(str(value) for value in item["dependencies"]),
            result_unit=str(item["result_unit"]), alignment_policy=str(item["alignment_policy"]),
            interpolation_policy=dict(item["interpolation_policy"]),
            numeric_policy=dict(item["numeric_policy"]), comment=str(item["comment"]),
            schema_version=CALCULATED_SIGNAL_SCHEMA_VERSION,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise CalculatedSignalConfigError("invalid_definition", f"无效计算通道定义: {exc}") from exc


class CalculatedSignalRestoreService:
    def __init__(self, engine: CalculationEngine | None = None):
        self.engine = engine or CalculationEngine()

    def restore(
        self, config_data: Mapping[str, Any], resolver: SignalResolver
    ) -> RestoreReport:
        loaded = load_calculated_signal_config(config_data)
        definitions = {item.stable_id: item for item in loaded.definitions}
        if len(definitions) != len(loaded.definitions):
            raise CalculatedSignalConfigError("duplicate_stable_id", "stable_id 不允许重复")
        graph = DependencyGraph(loaded.definitions).analyze()
        report = RestoreReport(
            definitions, restore_order=graph.order, migrated_legacy=loaded.migrated_legacy
        )
        for stable_id in graph.cycle_nodes:
            report.failures[stable_id] = RestoreFailure(
                stable_id, "dependency_cycle",
                f"计算通道依赖循环: {', '.join(graph.cycle_nodes)}", graph.cycle_nodes,
            )

        catalog = dict(resolver.signal_catalog)
        calculated_data = dict(resolver.legacy_math_data)
        for stable_id in graph.order:
            if stable_id in report.failures:
                continue
            definition = definitions[stable_id]
            failed_upstream = tuple(
                dep for dep in graph.calculated_dependencies[stable_id] if dep in report.failures
            )
            if failed_upstream:
                report.failures[stable_id] = RestoreFailure(
                    stable_id, "dependency_failed",
                    f"上游计算通道恢复失败: {', '.join(failed_upstream)}", failed_upstream,
                )
                continue
            unavailable = tuple(dep for dep in definition.dependencies if dep not in catalog)
            if unavailable:
                report.failures[stable_id] = RestoreFailure(
                    stable_id, "missing_dependency",
                    f"缺失依赖: {', '.join(unavailable)}", unavailable,
                )
                continue
            try:
                formula = definition.normalized_formula or definition.user_formula
                validated = parse_and_validate_formula(formula, definition.signal_tokens)
            except FormulaError as exc:
                report.failures[stable_id] = RestoreFailure(
                    stable_id, "invalid_formula", str(exc)
                )
                continue
            if validated.dependencies != definition.dependencies:
                report.failures[stable_id] = RestoreFailure(
                    stable_id, "dependency_mismatch", "配置依赖与安全解析结果不一致"
                )
                continue
            current_resolver = SignalResolver(
                catalog, data_source=resolver.data_source, data_path=resolver.data_path,
                can_data=resolver.can_data, legacy_math_data=calculated_data,
            )
            result = self.engine.calculate(definition, validated, current_resolver)
            if result.status is not CalculationStatus.SUCCESS:
                report.failures[stable_id] = RestoreFailure(
                    stable_id, "calculation_failed", result.error or "计算失败"
                )
                continue
            report.results[stable_id] = result
            catalog[stable_id] = {
                "name": stable_id, "display_name": definition.display_name,
                "unit": definition.result_unit, "comment": definition.comment,
            }
            calculated_data[stable_id] = {
                "timestamps": result.timestamps, "samples": result.samples,
                "unit": definition.result_unit,
            }
        return report