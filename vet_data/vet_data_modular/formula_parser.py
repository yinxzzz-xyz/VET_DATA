"""Syntax-only parsing for the restricted formula language."""

import ast
import io
import tokenize
from dataclasses import dataclass


@dataclass(frozen=True)
class FormulaIssue:
    code: str
    message: str
    line: int | None = None
    column: int | None = None
    offending: str | None = None


class FormulaError(ValueError):
    def __init__(self, issue: FormulaIssue):
        super().__init__(issue.message)
        self.issue = issue


class FormulaSyntaxError(FormulaError):
    pass


@dataclass(frozen=True)
class ParsedFormula:
    original_formula: str
    parser_formula: str
    tree: ast.Expression


class FormulaParser:
    """Parse formula text without evaluating or semantically validating it."""

    def parse(self, formula: str) -> ParsedFormula:
        if not isinstance(formula, str) or not formula.strip():
            raise FormulaSyntaxError(FormulaIssue("empty_formula", "公式不能为空", 1, 0))
        parser_formula = self._normalize_power_tokens(formula.strip())
        try:
            tree = ast.parse(parser_formula, mode="eval")
        except SyntaxError as exc:
            raise FormulaSyntaxError(
                FormulaIssue(
                    "syntax_error", exc.msg, exc.lineno,
                    None if exc.offset is None else max(0, exc.offset - 1),
                    exc.text.strip() if exc.text else None,
                )
            ) from exc
        return ParsedFormula(formula, parser_formula, tree)

    @staticmethod
    def _normalize_power_tokens(formula: str) -> str:
        try:
            tokens = []
            for token in tokenize.generate_tokens(io.StringIO(formula).readline):
                if token.type == tokenize.OP and token.string == "^":
                    token = tokenize.TokenInfo(token.type, "**", token.start, token.end, token.line)
                tokens.append(token)
            return tokenize.untokenize(tokens)
        except (tokenize.TokenError, IndentationError) as exc:
            location = exc.args[1] if len(exc.args) > 1 else (1, 0)
            raise FormulaSyntaxError(
                FormulaIssue("syntax_error", str(exc.args[0]), location[0], location[1])
            ) from exc
