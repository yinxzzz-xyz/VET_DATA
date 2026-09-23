import ast
import unittest
from pathlib import Path

from vet_data_modular.formula_parser import FormulaParser, FormulaSyntaxError
from vet_data_modular.formula_validator import (
    ALLOWED_FUNCTIONS,
    FormulaValidationError,
    FormulaValidator,
    parse_and_validate_formula,
)


BINDINGS = {
    "S001": "speed_G1_C2",
    "S002": "torque_G3_C4",
    "S003": "temperature",
    "S004": "voltage",
}


class FormulaParserValidatorTests(unittest.TestCase):
    def valid(self, formula, bindings=None):
        return parse_and_validate_formula(formula, BINDINGS if bindings is None else bindings)

    def assert_invalid(self, code, formula, bindings=None):
        with self.assertRaises((FormulaSyntaxError, FormulaValidationError)) as caught:
            self.valid(formula, bindings)
        self.assertEqual(caught.exception.issue.code, code)
        return caught.exception.issue

    def test_legal_constants_tokens_unary_and_binary_formulas(self):
        formulas = [
            "S001", "1", "1.5", "9500", "0.001", "-1",
            "S001+S002", "S001-S002", "S001*S002", "S001/S002",
            "S001^2", "(S001+S002-S003)^2/S004",
            "-S001", "+S001", "S001*-2",
        ]
        for formula in formulas:
            with self.subTest(formula=formula):
                self.assertIsInstance(self.valid(formula).tree, ast.Expression)

    def test_operator_precedence_is_represented_by_ast(self):
        unparenthesized = self.valid("S001+S002*S003").tree.body
        parenthesized = self.valid("(S001+S002)*S003").tree.body
        self.assertIsInstance(unparenthesized.op, ast.Add)
        self.assertIsInstance(unparenthesized.right.op, ast.Mult)
        self.assertIsInstance(parenthesized.op, ast.Mult)
        self.assertIsInstance(parenthesized.left.op, ast.Add)

    def test_all_v1_functions_are_accepted_with_one_argument(self):
        self.assertEqual(ALLOWED_FUNCTIONS, {
            "sqrt", "abs", "sin", "cos", "tan", "sind", "cosd", "tand",
            "log", "log10", "exp", "derivative", "integral",
        })
        for function in sorted(ALLOWED_FUNCTIONS):
            with self.subTest(function=function):
                result = self.valid(f"{function}(S001)")
                self.assertEqual(result.functions_used, (function,))

    def test_nested_functions_and_expression_arguments(self):
        nested = self.valid("sqrt(abs(S001-S002))")
        integral = self.valid("integral(S001-S002)")
        sine = self.valid("sin(S001+S002)")
        self.assertEqual(nested.functions_used, ("sqrt", "abs"))
        self.assertEqual(integral.dependencies, ("speed_G1_C2", "torque_G3_C4"))
        self.assertEqual(sine.signal_tokens, ("S001", "S002"))

    def test_dependencies_are_first_seen_ordered_and_deduplicated(self):
        result = self.valid("S002+S001*S002+S003+S001")
        self.assertEqual(result.signal_tokens, ("S002", "S001", "S003"))
        self.assertEqual(
            result.dependencies,
            ("torque_G3_C4", "speed_G1_C2", "temperature"),
        )

    def test_distinct_tokens_bound_to_same_key_deduplicate_dependency(self):
        result = self.valid("S001+S002", {"S001": "same", "S002": "same"})
        self.assertEqual(result.signal_tokens, ("S001", "S002"))
        self.assertEqual(result.dependencies, ("same",))

    def test_unknown_token_and_non_token_identifier_are_distinct(self):
        self.assert_invalid("unknown_signal_token", "S999")
        self.assert_invalid("unknown_identifier", "speed")
        self.assert_invalid("unknown_identifier", "S1")

    def test_unknown_and_invalid_function_calls(self):
        self.assert_invalid("unknown_function", "foo(S001)")
        self.assert_invalid("invalid_argument_count", "sqrt()")
        self.assert_invalid("invalid_argument_count", "sqrt(S001,S002)")
        self.assert_invalid("invalid_arguments", "sqrt(x=S001)")
        self.assert_invalid("invalid_arguments", "sqrt(*S001)")

    def test_dangerous_builtin_calls_are_security_violations(self):
        for formula in [
            '__import__("os")', 'open("x")', 'eval("x")',
            'exec("x")', 'getattr(S001,"x")',
        ]:
            with self.subTest(formula=formula):
                self.assert_invalid("security_violation", formula)

    def test_attribute_dynamic_call_and_subscript_are_forbidden(self):
        for formula in [
            '__import__("os").system("cmd")', "S001.__class__",
            "S001[0]", "(lambda x:x)(S001)",
        ]:
            with self.subTest(formula=formula):
                self.assert_invalid("forbidden_expression", formula)

    def test_comprehensions_collections_and_fstrings_are_forbidden(self):
        for formula in [
            "[x for x in S001]", "{x for x in S001}",
            "{x:x for x in S001}", "(x for x in S001)",
            "[S001]", "(S001,)", '{"x": S001}', "{S001}", 'f"{S001}"',
        ]:
            with self.subTest(formula=formula):
                self.assert_invalid("forbidden_expression", formula)

    def test_bool_compare_if_expression_and_walrus_are_forbidden(self):
        for formula in [
            "S001 > 1", "S001 and S002", "S001 if S002 else S003", "(x:=S001)",
        ]:
            with self.subTest(formula=formula):
                self.assert_invalid("forbidden_expression", formula)

    def test_unsupported_operators_are_distinguished(self):
        for formula in ["S001%2", "S001//2", "S001@S002", "S001<<2", "~S001"]:
            with self.subTest(formula=formula):
                self.assert_invalid("unsupported_operator", formula)

    def test_invalid_constants_are_rejected(self):
        for formula in ['"text"', "True", "None", "1j"]:
            with self.subTest(formula=formula):
                self.assert_invalid("unsupported_constant", formula)

    def test_empty_whitespace_and_incomplete_formulas_are_syntax_errors(self):
        for formula, code in [
            ("", "empty_formula"), ("   ", "empty_formula"), ("(", "syntax_error"),
            ("(S001", "syntax_error"), ("S001+", "syntax_error"),
            ("sqrt(", "syntax_error"),
        ]:
            with self.subTest(formula=formula):
                issue = self.assert_invalid(code, formula)
                self.assertIsNotNone(issue.line)

    def test_caret_is_power_never_xor_and_double_star_is_equivalent(self):
        caret = self.valid("S001^2")
        double_star = self.valid("S001**2")
        self.assertIsInstance(caret.tree.body.op, ast.Pow)
        self.assertEqual(caret.normalized_formula, "S001 ** 2")
        self.assertEqual(caret.normalized_formula, double_star.normalized_formula)

    def test_power_precedence_and_associativity_are_mathematical(self):
        cases = {
            "S001^2 + S002": "S001 ** 2 + S002",
            "S001^(2+1)": "S001 ** (2 + 1)",
            "-S001^2": "-S001 ** 2",
            "(-S001)^2": "(-S001) ** 2",
            "S001^-2": "S001 ** (-2)",
            "S001^2^3": "S001 ** 2 ** 3",
            "(S001+S002)^2": "(S001 + S002) ** 2",
        }
        for formula, normalized in cases.items():
            with self.subTest(formula=formula):
                self.assertEqual(self.valid(formula).normalized_formula, normalized)
        root = self.valid("S001^2^3").tree.body
        self.assertIsInstance(root.right, ast.BinOp)
        self.assertIsInstance(root.right.op, ast.Pow)

    def test_normalized_formula_is_deterministic(self):
        first = self.valid(" ( S001 + S002 ) ^ 2 / S003 ")
        second = self.valid("(S001+S002)^2/S003")
        self.assertEqual(first.normalized_formula, second.normalized_formula)
        self.assertEqual(first.normalized_formula, "(S001 + S002) ** 2 / S003")

    def test_bindings_are_not_modified(self):
        bindings = dict(BINDINGS)
        before = dict(bindings)
        self.valid("S001+S002", bindings)
        self.assertEqual(bindings, before)

    def test_structured_errors_include_position_and_offending_node(self):
        issue = self.assert_invalid("unknown_signal_token", "S001 + S999")
        self.assertEqual(issue.line, 1)
        self.assertEqual(issue.column, 7)
        self.assertEqual(issue.offending, "S999")

    def test_parser_and_validator_are_separate_gui_free_services(self):
        parsed = FormulaParser().parse("S001^2")
        result = FormulaValidator().validate(parsed, BINDINGS)
        self.assertEqual(result.normalized_formula, "S001 ** 2")
        root = Path(__file__).parents[1] / "vet_data" / "vet_data_modular"
        for name in ("formula_parser.py", "formula_validator.py"):
            source = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("PyQt", source)
            self.assertNotIn("QWidget", source)


if __name__ == "__main__":
    unittest.main()
