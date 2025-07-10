from django.test import TestCase

from waldur_mastermind.common.serializers import validate_options


class ValidateOptionsTest(TestCase):
    def test_validate_options_with_choices_for_choice_field(self):
        """Test that choices parameter is accepted for ChoiceField"""
        options = {
            "field1": {
                "type": "select_string",
                "choices": [("option1", "Option 1"), ("option2", "Option 2")],
                "required": True,
            }
        }
        attributes = {"field1": "option1"}

        # Should not raise an exception
        validate_options(options, attributes)

    def test_validate_options_with_choices_for_multiple_choice_field(self):
        """Test that choices parameter is accepted for MultipleChoiceField"""
        options = {
            "field1": {
                "type": "select_string_multi",
                "choices": [("option1", "Option 1"), ("option2", "Option 2")],
                "required": True,
            }
        }
        attributes = {"field1": ["option1", "option2"]}

        # Should not raise an exception
        validate_options(options, attributes)

    def test_validate_options_with_choices_for_char_field(self):
        """Test that choices parameter is ignored for CharField (no type specified)"""
        options = {
            "field1": {
                # No type specified, defaults to CharField
                "choices": [("option1", "Option 1"), ("option2", "Option 2")],
                "required": True,
            }
        }
        attributes = {"field1": "any_value"}

        # Should not raise an exception - choices should be ignored for CharField
        validate_options(options, attributes)

    def test_validate_options_choices_ignored_for_non_choice_fields(self):
        """Test that choices parameter is properly ignored for field types that don't support it"""
        # Test with different field types that don't support choices
        field_types = ["integer", "date", "time", "money", "boolean"]

        for field_type in field_types:
            with self.subTest(field_type=field_type):
                options = {
                    "test_field": {
                        "type": field_type,
                        "choices": [("option1", "Option 1"), ("option2", "Option 2")],
                        "required": True,
                    }
                }
                # Use appropriate attribute values for each field type
                attribute_values = {
                    "integer": 123,
                    "date": "2023-01-01",
                    "time": "12:00:00",
                    "money": 100,
                    "boolean": True,
                }
                attributes = {"test_field": attribute_values[field_type]}

                # Should not raise an exception - choices should be ignored
                validate_options(options, attributes)

    def test_validate_options_with_choices_for_integer_field(self):
        """Test that choices parameter is ignored for IntegerField"""
        options = {
            "field1": {
                "type": "integer",
                "choices": [("1", "One"), ("2", "Two")],
                "required": True,
            }
        }
        attributes = {"field1": 123}

        # Should not raise an exception - choices should be ignored for IntegerField
        validate_options(options, attributes)

    def test_validate_options_with_min_max_for_integer_field(self):
        """Test that min and max parameters work for IntegerField"""
        options = {
            "field1": {
                "type": "integer",
                "min": 1,
                "max": 100,
                "required": True,
            }
        }
        attributes = {"field1": 50}

        # Should not raise an exception
        validate_options(options, attributes)

    def test_validate_options_with_default_value(self):
        """Test that default values work correctly"""
        options = {
            "field1": {
                "type": "integer",
                "default": 42,
            }
        }
        attributes = {}

        # Should not raise an exception
        validate_options(options, attributes)
