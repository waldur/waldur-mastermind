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
