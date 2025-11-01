from rest_framework import test

from waldur_mastermind.common.serializers import validate_options


class ConditionalCascadeValidationTest(test.APITestCase):
    def test_validate_options_with_conditional_cascade(self):
        """Test that validate_options handles conditional_cascade fields properly"""

        options = {
            "name": {
                "type": "string",
                "label": "Name",
                "required": True,
            },
            "location": {
                "type": "conditional_cascade",
                "label": "Location",
                "required": True,
                "cascade_config": {
                    "steps": [
                        {
                            "name": "country",
                            "label": "Country",
                            "type": "select_string",
                            "choices": '[{"value": "us", "label": "United States"}, {"value": "eu", "label": "European Union"}]',
                        },
                        {
                            "name": "datacenter",
                            "label": "Data Center",
                            "type": "select_string",
                            "depends_on": "country",
                            "choices_map": '{"us": [{"value": "us-east", "label": "US East"}], "eu": [{"value": "eu-west", "label": "EU West"}]}',
                        },
                    ]
                },
            },
        }

        attributes = {
            "name": "test-instance",
            "location": {"country": "us", "datacenter": "us-east"},
        }

        # This should not raise "Not a valid string" error anymore
        try:
            validate_options(options, attributes)
        except Exception as e:
            self.fail(
                f"validate_options should not fail for conditional_cascade fields: {e}"
            )

    def test_validate_options_with_invalid_conditional_cascade(self):
        """Test that validate_options properly validates conditional_cascade fields"""

        options = {
            "location": {
                "type": "conditional_cascade",
                "label": "Location",
                "required": True,
            }
        }

        # Invalid attribute value (not a dict)
        attributes = {
            "location": "invalid_string_value"  # Should be an object
        }

        # This should raise a validation error for incorrect type
        with self.assertRaises(Exception):
            validate_options(options, attributes)

    def test_validate_options_mixed_field_types(self):
        """Test that validate_options handles mixed field types including conditional_cascade"""

        options = {
            "name": {
                "type": "string",
                "label": "Name",
                "required": True,
            },
            "size": {
                "type": "select_string",
                "label": "Size",
                "choices": ["small", "large"],
                "required": True,
            },
            "location": {
                "type": "conditional_cascade",
                "label": "Location",
                "required": False,
            },
        }

        attributes = {
            "name": "test-instance",
            "size": "large",
            "location": {"country": "us", "datacenter": "us-east"},
        }

        # This should work without errors
        try:
            validate_options(options, attributes)
        except Exception as e:
            self.fail(f"validate_options should handle mixed field types: {e}")

    def test_conditional_cascade_empty_object(self):
        """Test that empty cascade objects are valid"""

        options = {
            "location": {
                "type": "conditional_cascade",
                "label": "Location",
                "required": False,
            }
        }

        attributes = {
            "location": {}  # Empty cascade object
        }

        try:
            validate_options(options, attributes)
        except Exception as e:
            self.fail(f"Empty cascade objects should be valid: {e}")

    def test_conditional_cascade_partial_selection(self):
        """Test that partial cascade selections are valid"""

        options = {
            "location": {
                "type": "conditional_cascade",
                "label": "Location",
                "required": False,
            }
        }

        attributes = {
            "location": {
                "country": "us",
                # datacenter not selected yet - this should be valid
            }
        }

        try:
            validate_options(options, attributes)
        except Exception as e:
            self.fail(f"Partial cascade selections should be valid: {e}")

    def test_conditional_cascade_with_nested_objects(self):
        """Test that cascade fields can contain complex nested values"""

        options = {
            "complex_location": {
                "type": "conditional_cascade",
                "label": "Complex Location",
                "required": False,
            }
        }

        attributes = {
            "complex_location": {
                "region": "us-west",
                "zone": "us-west-2a",
                "metadata": {"rack_id": "123", "subnet": "10.0.1.0/24"},
            }
        }

        try:
            validate_options(options, attributes)
        except Exception as e:
            self.fail(f"Complex nested cascade values should be valid: {e}")

    def test_required_conditional_cascade_missing(self):
        """Test that required cascade fields are properly validated"""

        options = {
            "location": {
                "type": "conditional_cascade",
                "label": "Location",
                "required": True,  # This field is required
            }
        }

        attributes = {
            # Missing required cascade field
        }

        # This should raise a validation error
        with self.assertRaises(Exception):
            validate_options(options, attributes)
