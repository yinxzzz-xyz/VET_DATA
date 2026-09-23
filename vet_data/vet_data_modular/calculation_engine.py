"""Safe vectorized execution of validated basic formulas."""

import ast
from typing import Mapping

import numpy as np

from .calculated_signal import (
    CalculationStatus,
    CalculatedSignalDefinition,
    CalculatedSignalResult,
)
from .formula_validator import ALLOWED_FUNCTIONS, ValidatedFormula
from .signal_resolver import SignalResolutionError, SignalResolver
from .time_alignment import (
    AlignmentInput,
    InterpolationPolicy,
    TimeAlignmentError,
    align_signals,
)


NEAR_ZERO_THRESHOLD = 1e-7
TRIG_SINGULARITY_THRESHOLD = 1e-12
_TIME_FUNCTIONS = frozenset({"derivative", "integral"})


class CalculationEngineError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CalculationEngine:
    """Resolve, align once, and execute an already validated formula AST."""

    def calculate(
        self,
        definition: CalculatedSignalDefinition,
        formula: ValidatedFormula,
        resolver: SignalResolver,
        interpolation_policies: Mapping[str, InterpolationPolicy] | None = None,
    ) -> CalculatedSignalResult:
        try:
            if not formula.dependencies:
                raise CalculationEngineError(
                    "no_signal_dependencies", "纯常量公式没有可用的时间轴"
                )
            policies = interpolation_policies or {}
            resolved_by_key = {
                key: resolver.resolve(key) for key in formula.dependencies
            }
            alignment_inputs = []
            for token in formula.signal_tokens:
                if token not in definition.signal_tokens:
                    raise CalculationEngineError(
                        "missing_token_binding", f"计算定义缺少 token 绑定: {token}"
                    )
                signal_key = definition.signal_tokens[token]
                if signal_key not in resolved_by_key:
                    raise CalculationEngineError(
                        "dependency_mismatch", f"验证依赖与计算定义不一致: {signal_key}"
                    )
                policy = policies.get(
                    token, policies.get(signal_key, InterpolationPolicy.LINEAR)
                )
                alignment_inputs.append(
                    AlignmentInput(
                        token,
                        resolved_by_key[signal_key].timestamps,
                        resolved_by_key[signal_key].samples,
                        policy,
                    )
                )

            alignment = align_signals(alignment_inputs)
            evaluator = _AstEvaluator(alignment.samples_by_key, alignment.timestamps)
            with np.errstate(all="ignore"):
                raw_samples = np.asarray(evaluator.evaluate(formula.tree))
            if raw_samples.ndim == 0:
                raw_samples = np.full(alignment.timestamps.shape, raw_samples.item())
            if raw_samples.shape != alignment.timestamps.shape:
                raise CalculationEngineError(
                    "invalid_result_shape", "公式结果形状与统一时间轴不一致"
                )

            raw_nan_count = int(np.count_nonzero(np.isnan(raw_samples)))
            raw_inf_count = int(np.count_nonzero(np.isinf(raw_samples)))
            samples = raw_samples.astype(float, copy=True)
            samples[np.isinf(samples)] = np.nan
            nan_count = int(np.count_nonzero(np.isnan(samples)))
            inf_count = int(np.count_nonzero(np.isinf(samples)))
            input_nan_count = sum(
                item.input_nan_count for item in alignment.diagnostics.signals.values()
            )
            input_inf_count = sum(
                item.input_inf_count for item in alignment.diagnostics.signals.values()
            )
            diagnostics = {
                "input_signal_count": alignment.diagnostics.input_signal_count,
                "effective_start": alignment.diagnostics.effective_start,
                "effective_end": alignment.diagnostics.effective_end,
                "output_sample_count": alignment.diagnostics.output_sample_count,
                "functions_used": list(formula.functions_used),
                "interpolation_policies": {
                    key: item.interpolation.value
                    for key, item in alignment.diagnostics.signals.items()
                },
                "input_nan_count": input_nan_count,
                "input_inf_count": input_inf_count,
                "raw_result_nan_count": raw_nan_count,
                "raw_result_inf_count": raw_inf_count,
                **evaluator.diagnostics,
            }
            return CalculatedSignalResult(
                timestamps=alignment.timestamps.copy(),
                samples=samples,
                unit=definition.result_unit,
                status=CalculationStatus.SUCCESS,
                nan_count=nan_count,
                inf_count=inf_count,
                diagnostics=diagnostics,
            )
        except (CalculationEngineError, SignalResolutionError, TimeAlignmentError) as exc:
            return CalculatedSignalResult(
                unit=definition.result_unit,
                status=CalculationStatus.ERROR,
                error=str(exc),
                diagnostics={"error_code": getattr(exc, "code", type(exc).__name__)},
            )


class _AstEvaluator:
    def __init__(self, values: Mapping[str, np.ndarray], timestamps: np.ndarray):
        self.values = values
        self.timestamps = timestamps
        self.diagnostics = {
            "divide_by_zero_count": 0,
            "domain_error_count": 0,
            "overflow_count": 0,
            "trigonometric_singularity_count": 0,
        }

    def evaluate(self, node):
        method = getattr(self, f"evaluate_{type(node).__name__}", None)
        if method is None:
            raise CalculationEngineError(
                "unsupported_ast", f"Calculation Engine 拒绝 AST 节点: {type(node).__name__}"
            )
        return method(node)

    def evaluate_Expression(self, node):
        return self.evaluate(node.body)

    def evaluate_Constant(self, node):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalculationEngineError("unsupported_ast", "Engine 仅接受数值常量")
        return node.value

    def evaluate_Name(self, node):
        if node.id not in self.values:
            raise CalculationEngineError("missing_aligned_signal", f"缺少已对齐信号: {node.id}")
        return self.values[node.id]

    def evaluate_UnaryOp(self, node):
        operand = self.evaluate(node.operand)
        if isinstance(node.op, ast.UAdd):
            return np.positive(operand)
        if isinstance(node.op, ast.USub):
            return np.negative(operand)
        raise CalculationEngineError("unsupported_ast", "Engine 拒绝一元运算符")

    def evaluate_BinOp(self, node):
        left = self.evaluate(node.left)
        right = self.evaluate(node.right)
        if isinstance(node.op, ast.Add):
            result = np.add(left, right)
            self._record_new_nonfinite(result, left, right)
            return result
        if isinstance(node.op, ast.Sub):
            result = np.subtract(left, right)
            self._record_new_nonfinite(result, left, right)
            return result
        if isinstance(node.op, ast.Mult):
            result = np.multiply(left, right)
            self._record_new_nonfinite(result, left, right)
            return result
        if isinstance(node.op, ast.Div):
            denominator = np.asarray(right)
            near_zero = np.abs(denominator) < NEAR_ZERO_THRESHOLD
            self.diagnostics["divide_by_zero_count"] += int(np.count_nonzero(near_zero))
            result = np.true_divide(left, right)
            self._record_new_nonfinite(result, left, right)
            return np.where(near_zero, np.nan, result)
        if isinstance(node.op, ast.Pow):
            result = np.power(left, right)
            self._record_new_nonfinite(result, left, right)
            return result
        raise CalculationEngineError("unsupported_ast", "Engine 拒绝二元运算符")

    def evaluate_Call(self, node):
        if not isinstance(node.func, ast.Name) or len(node.args) != 1 or node.keywords:
            raise CalculationEngineError("unsupported_ast", "Engine 拒绝非法函数调用")
        function = node.func.id
        if function not in ALLOWED_FUNCTIONS:
            raise CalculationEngineError("unsupported_function", f"Engine 不支持函数: {function}")
        value = self.evaluate(node.args[0])
        if function in _TIME_FUNCTIONS:
            return self._evaluate_time_function(function, value)
        return self._evaluate_function(function, value)

    def _evaluate_time_function(self, function, value):
        values = np.asarray(value, dtype=float)
        if values.ndim == 0:
            values = np.full(self.timestamps.shape, values.item(), dtype=float)
        if values.shape != self.timestamps.shape:
            raise CalculationEngineError(
                "invalid_time_function_shape",
                f"{function}() 参数形状与统一时间轴不一致",
            )
        if function == "derivative":
            if self.timestamps.size < 2:
                raise CalculationEngineError(
                    "insufficient_derivative_samples", "derivative() 至少需要 2 个时间点"
                )
            return np.gradient(values, self.timestamps, edge_order=1)
        if function == "integral":
            result = np.zeros(self.timestamps.shape, dtype=float)
            if self.timestamps.size > 1:
                dt = self.timestamps[1:] - self.timestamps[:-1]
                increments = (values[:-1] + values[1:]) * 0.5 * dt
                result[1:] = np.cumsum(increments)
            return result
        raise CalculationEngineError("unsupported_function", f"Engine 不支持函数: {function}")

    def _evaluate_function(self, function, value):
        if function == "abs":
            return np.abs(value)
        if function == "sqrt":
            invalid = np.asarray(value) < 0
            self.diagnostics["domain_error_count"] += int(np.count_nonzero(invalid))
            return np.where(invalid, np.nan, np.sqrt(value))
        if function in {"sin", "cos"}:
            return getattr(np, function)(value)
        if function == "tan":
            return self._safe_tan(value)
        if function in {"sind", "cosd", "tand"}:
            radians = np.deg2rad(value)
            if function == "tand":
                return self._safe_tan(radians)
            return np.sin(radians) if function == "sind" else np.cos(radians)
        if function in {"log", "log10"}:
            invalid = np.asarray(value) <= 0
            self.diagnostics["domain_error_count"] += int(np.count_nonzero(invalid))
            operation = np.log if function == "log" else np.log10
            return np.where(invalid, np.nan, operation(value))
        if function == "exp":
            result = np.exp(value)
            self.diagnostics["overflow_count"] += int(np.count_nonzero(np.isinf(result)))
            return result
        raise CalculationEngineError("unsupported_function", f"Engine 不支持函数: {function}")

    def _safe_tan(self, radians):
        singular = np.abs(np.cos(radians)) < TRIG_SINGULARITY_THRESHOLD
        self.diagnostics["trigonometric_singularity_count"] += int(np.count_nonzero(singular))
        return np.where(singular, np.nan, np.tan(radians))

    def _record_new_nonfinite(self, result, *operands):
        finite_inputs = np.ones(np.shape(result), dtype=bool)
        for operand in operands:
            finite_inputs = finite_inputs & np.isfinite(operand)
        self.diagnostics["domain_error_count"] += int(
            np.count_nonzero(np.isnan(result) & finite_inputs)
        )
        self.diagnostics["overflow_count"] += int(
            np.count_nonzero(np.isinf(result) & finite_inputs)
        )
