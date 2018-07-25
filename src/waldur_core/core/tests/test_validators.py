from ddt import data, ddt
from django.core.exceptions import ValidationError
from rest_framework import test

from waldur_core.core import validators
from waldur_core.structure.models import Customer


class NameValidationTest(test.APITransactionTestCase):
    def test_name_should_have_at_least_one_non_whitespace_character(self):
        with self.assertRaises(ValidationError):
            customer = Customer(name='      ')
            customer.full_clean()


@ddt
class MinCronValueValidatorTest(test.APITransactionTestCase):

    @data('*/1 * * * *', '*/10 * * * *', '*/59 * * * *')
    def test_validator_raises_validation_error_if_given_schedule_value_is_less_than_1_hours(self, value):
        validator = validators.MinCronValueValidator(limit_value=1)
        with self.assertRaises(ValidationError):
            validator(value)

    @data('hello world', '* * * * * *', '*/59')
    def test_validator_raises_validation_error_if_given_format_is_not_valid(self, value):
        validator = validators.MinCronValueValidator(limit_value=1)
        with self.assertRaises(ValidationError):
            validator(value)

    @data('0 * * * *', '0 0 * * *', '0 0 0 * *', '0 0 * * 0', '0 0 1 * *', '0 0 1 1 *', '0 0 1 1 *')
    def test_validator_does_not_raise_error_if_schedule_is_greater_than_or_equal_1_hour(self, value):
        validator = validators.MinCronValueValidator(limit_value=1)
        validator(value)


class CIDRListValidatorTest(test.APITransactionTestCase):

    def test_validator_accepts_valid_cidr_list(self):
        validators.validate_cidr_list('fc00::/7, 127.0.0.1/32')

    def test_validator_accepts_empty_list(self):
        validators.validate_cidr_list('   ')

    def test_invalid_values_are_rejected(self):
        with self.assertRaises(ValidationError):
            validators.validate_cidr_list('hello/25')

    def test_space_separated_list_rejected(self):
        with self.assertRaises(ValidationError):
            validators.validate_cidr_list('fc00::/7  127.0.0.1/32')
