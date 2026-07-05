from decimal import Decimal
from unittest import TestCase

from waldur_mastermind.common import formula


class FormulaEvaluationTest(TestCase):
    def assert_close(self, result, expected, places=2):
        self.assertAlmostEqual(float(result), expected, places=places)

    def test_arithmetic(self):
        self.assertEqual(formula.evaluate("2 + 3 * 4"), Decimal("14"))

    def test_variables(self):
        self.assertEqual(formula.evaluate("usage * 2", usage=21), Decimal("42"))

    def test_virsma_ram_discount(self):
        # Published reference points of the Virsma calculator (natural log).
        expression = "MIN(70, LN(MAX(1, usage - 1)) * 10)"
        self.assert_close(formula.evaluate(expression, usage=10), 21.97)
        self.assert_close(formula.evaluate(expression, usage=1000), 69.07)
        self.assertEqual(formula.evaluate(expression, usage=1), Decimal("0"))
        # Cap engages on very large quantities.
        self.assertEqual(formula.evaluate(expression, usage=10**9), Decimal("70"))

    def test_virsma_disk_discount(self):
        expression = "MIN(70, LN(MAX(1, usage - 10)) * 5)"
        self.assertEqual(formula.evaluate(expression, usage=10), Decimal("0"))
        self.assert_close(formula.evaluate(expression, usage=5000), 42.58)

    def test_functions_are_case_insensitive(self):
        self.assertEqual(formula.evaluate("min(1, 2)"), Decimal("1"))
        self.assertEqual(formula.evaluate("Max(1, 2)"), Decimal("2"))

    def test_conditional_expression(self):
        expression = "10 if usage >= 100 else 0"
        self.assertEqual(formula.evaluate(expression, usage=100), Decimal("10"))
        self.assertEqual(formula.evaluate(expression, usage=99), Decimal("0"))

    def test_capped_fee(self):
        expression = "MIN(500, amount * 0.05)"
        self.assertEqual(formula.evaluate(expression, amount=300), Decimal("15.0"))
        self.assertEqual(formula.evaluate(expression, amount=100000), Decimal("500"))

    def test_unknown_variable_is_rejected(self):
        with self.assertRaises(formula.FormulaError):
            formula.evaluate("usage + 1", amount=1)

    def test_unknown_function_is_rejected(self):
        with self.assertRaises(formula.FormulaError):
            formula.evaluate("EXP(1)")

    def test_code_execution_is_rejected(self):
        for expression in (
            "__import__('os').system('true')",
            "(1).__class__",
            "[x for x in (1,)]",
            "'a' * 3",
            "lambda: 1",
        ):
            with self.assertRaises(formula.FormulaError):
                formula.evaluate(expression)

    def test_division_by_zero_is_rejected(self):
        with self.assertRaises(formula.FormulaError):
            formula.evaluate("1 / 0")

    def test_math_domain_error_is_rejected(self):
        with self.assertRaises(formula.FormulaError):
            formula.evaluate("LN(0)")

    def test_non_finite_result_is_rejected(self):
        with self.assertRaises(formula.FormulaError):
            formula.evaluate("10.0 ** 400 * 10.0 ** 400")

    def test_empty_formula_is_rejected(self):
        with self.assertRaises(formula.FormulaError):
            formula.evaluate("  ")

    def test_too_long_formula_is_rejected(self):
        with self.assertRaises(formula.FormulaError):
            formula.evaluate("1 + " * 300 + "1")


class FormulaValidationTest(TestCase):
    def test_valid_formula_passes(self):
        formula.validate("MIN(70, LN(MAX(1, usage - 1)) * 10)", ("usage",))

    def test_probe_catches_domain_errors(self):
        # LN(usage) breaks at usage=0 — must be rejected at save time
        # instead of failing during month close.
        with self.assertRaises(formula.FormulaError):
            formula.validate("LN(usage)", ("usage",))

    def test_syntax_error_is_rejected(self):
        with self.assertRaises(formula.FormulaError):
            formula.validate("MIN(70,", ("usage",))
