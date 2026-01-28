from rest_framework import test
from rest_framework.exceptions import ValidationError

from waldur_mastermind.common.serializers import validate_options


class OptionsValidationTest(test.APITransactionTestCase):
    def test_greater_than_validator(self):
        options = {
            "hard_limit": {
                "type": "integer",
                "label": "Hard limit",
                "validators": [{"type": "gt", "target_field": "soft_limit"}],
            },
            "soft_limit": {
                "type": "integer",
                "label": "Soft limit",
            },
        }

        # valid case
        attributes = {"hard_limit": 8, "soft_limit": 4}
        try:
            validate_options(options, attributes)
        except ValidationError:
            self.fail("Validation should have passed")

        # invalid case
        attributes = {"hard_limit": 2, "soft_limit": 4}
        with self.assertRaises(ValidationError) as cm:
            validate_options(options, attributes)

        self.assertIn("hard_limit", cm.exception.detail)
        self.assertIn(
            "must be greater than soft_limit", str(cm.exception.detail["hard_limit"])
        )

    def test_greater_than_or_equal_validator(self):
        options = {
            "hard_limit": {
                "type": "integer",
                "validators": [{"type": "gte", "target_field": "soft_limit"}],
            },
            "soft_limit": {"type": "integer"},
        }

        # valid case: equal
        attributes = {"hard_limit": 4, "soft_limit": 4}
        try:
            validate_options(options, attributes)
        except ValidationError:
            self.fail("Validation should have passed")

        # valid case: greater
        attributes = {"hard_limit": 5, "soft_limit": 4}
        try:
            validate_options(options, attributes)
        except ValidationError:
            self.fail("Validation should have passed")

        # invalid case
        attributes = {"hard_limit": 3, "soft_limit": 4}
        with self.assertRaises(ValidationError) as cm:
            validate_options(options, attributes)
        self.assertIn(
            "must be greater than or equal to soft_limit",
            str(cm.exception.detail["hard_limit"]),
        )

    def test_less_than_validator(self):
        options = {
            "soft_limit": {
                "type": "integer",
                "validators": [{"type": "lt", "target_field": "hard_limit"}],
            },
            "hard_limit": {"type": "integer"},
        }

        # valid case
        attributes = {"soft_limit": 3, "hard_limit": 4}
        try:
            validate_options(options, attributes)
        except ValidationError:
            self.fail("Validation should have passed")

        # invalid case: equal
        attributes = {"soft_limit": 4, "hard_limit": 4}
        with self.assertRaises(ValidationError) as cm:
            validate_options(options, attributes)
        self.assertIn(
            "must be less than hard_limit", str(cm.exception.detail["soft_limit"])
        )

    def test_less_than_or_equal_validator(self):
        options = {
            "soft_limit": {
                "type": "integer",
                "validators": [{"type": "lte", "target_field": "hard_limit"}],
            },
            "hard_limit": {"type": "integer"},
        }

        # valid case: equal
        attributes = {"soft_limit": 4, "hard_limit": 4}
        try:
            validate_options(options, attributes)
        except ValidationError:
            self.fail("Validation should have passed")

        # valid case: less
        attributes = {"soft_limit": 3, "hard_limit": 4}
        try:
            validate_options(options, attributes)
        except ValidationError:
            self.fail("Validation should have passed")

        # invalid case
        attributes = {"soft_limit": 5, "hard_limit": 4}
        with self.assertRaises(ValidationError) as cm:
            validate_options(options, attributes)
        self.assertIn(
            "must be less than or equal to hard_limit",
            str(cm.exception.detail["soft_limit"]),
        )
