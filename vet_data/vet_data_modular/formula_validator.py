"""Semantic and security validation for parsed V1 formulas."""

import ast
import re
from dataclasses import dataclass
from typing import Mapping

from .formula_parser import FormulaError, FormulaIssue, ParsedFormula

SIGNAL_TOKEN_PATTERN = re.compile(r"S[0-9]{3,}\Z", re.ASCII)
ALLOWED_FUNCTIONS = frozenset({
    "sqrt", "abs", "sin", "cos", "tan", "sind", "cosd", "tand",
    "log", "log10", "exp", "derivative", "integral",
})
_DANGEROUS_CALLS = frozenset({"__import__", "open", "eval", "exec", "getattr"})
_BINARY_OPERATORS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
_UNARY_OPERATORS = (ast.UAdd, ast.USub)


class FormulaValidationError(FormulaError):
    pass


@dataclass(frozen=True)
class ValidatedFormula:
    original_formula: str
    normalized_formula: str
    tree: ast.Expression
    signal_tokens: tuple[str, ...]
    dependencies: tuple[str, ...]
    functions_used: tuple[str, ...]


class FormulaValidator:
    def validate(self, parsed: ParsedFormula, token_bindings: Mapping[str, str]) -> ValidatedFormula:
        visitor = _ValidationVisitor(token_bindings)
        visitor.visit(parsed.tree)
        return ValidatedFormula(
            parsed.original_formula, ast.unparse(parsed.tree), parsed.tree,
            tuple(visitor.signal_tokens), tuple(visitor.dependencies),
            tuple(visitor.functions_used),
        )


class _ValidationVisitor:
    def __init__(self, token_bindings):
        self.bindings = token_bindings
        self.signal_tokens = []
        self.dependencies = []
        self.functions_used = []

    def visit(self, node):
        method = getattr(self, f"visit_{type(node).__name__}", None)
        if method is None:
            self._fail("forbidden_expression", f"禁止的公式语法: {type(node).__name__}", node)
        method(node)

    def visit_Expression(self, node):
        self.visit(node.body)

    def visit_Constant(self, node):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            self._fail("unsupported_constant", "仅允许整数和浮点数常量", node)

    def visit_Name(self, node):
        token = node.id
        if not SIGNAL_TOKEN_PATTERN.fullmatch(token):
            self._fail("unknown_identifier", f"未知标识符: {token}", node, token)
        if token not in self.bindings:
            self._fail("unknown_signal_token", f"信号 token 未绑定: {token}", node, token)
        if token not in self.signal_tokens:
            self.signal_tokens.append(token)
            dependency = self.bindings[token]
            if dependency not in self.dependencies:
                self.dependencies.append(dependency)

    def visit_BinOp(self, node):
        if not isinstance(node.op, _BINARY_OPERATORS):
            self._fail("unsupported_operator", f"不支持的二元运算符: {type(node.op).__name__}", node)
        self.visit(node.left)
        self.visit(node.right)

    def visit_UnaryOp(self, node):
        if not isinstance(node.op, _UNARY_OPERATORS):
            self._fail("unsupported_operator", f"不支持的一元运算符: {type(node.op).__name__}", node)
        self.visit(node.operand)

    def visit_Call(self, node):
        if not isinstance(node.func, ast.Name):
            self._fail("forbidden_expression", "禁止属性或动态函数调用", node)
        function = node.func.id
        if function in _DANGEROUS_CALLS:
            self._fail("security_violation", f"禁止调用函数: {function}", node, function)
        if function not in ALLOWED_FUNCTIONS:
            self._fail("unknown_function", f"未知函数: {function}", node, function)
        if node.keywords or any(isinstance(argument, ast.Starred) for argument in node.args):
            self._fail("invalid_arguments", f"函数 {function} 不允许关键字或展开参数", node)
        if len(node.args) != 1:
            self._fail("invalid_argument_count", f"函数 {function} 必须有且仅有 1 个参数", node)
        if function not in self.functions_used:
            self.functions_used.append(function)
        self.visit(node.args[0])

    @staticmethod
    def _fail(code, message, node, offending=None):
        raise FormulaValidationError(FormulaIssue(
            code, message, getattr(node, "lineno", None),
            getattr(node, "col_offset", None), offending or type(node).__name__,
        ))


def parse_and_validate_formula(formula: str, token_bindings: Mapping[str, str]) -> ValidatedFormula:
    """Convenience API retaining parser/validator separation internally."""
    from .formula_parser import FormulaParser
    return FormulaValidator().validate(FormulaParser().parse(formula), token_bindings)
