from rest_framework import test

from waldur_mastermind.marketplace import serializers


class ConditionalCascadeErrorHandlingTest(test.APITestCase):
    def test_invalid_payload_from_frontend(self):
        """Test the exact payload that was causing the error"""
        # This is the payload that was causing the TypeError: Dict key must be str
        invalid_payload = {
            "order": ["conditional"],
            "options": {
                "conditional": {
                    "type": "conditional_cascade",
                    "name": "conditional",
                    "label": "Conditional",
                    "cascade_config": {
                        "steps": [
                            {
                                "name": "ints",
                                "label": "asd,asd",
                                "type": "select_string",
                                "choices": '[{"value": 2, "label": "asd"}, {"value": "asd3", "label": "asd"}]',
                            },
                            {
                                "name": "f2",
                                "label": "F2",
                                "type": "select_string_multi",
                                "depends_on": "ints",
                                # This is the problematic part - choices_map should be an object, not an array
                                "choices_map": '[{"value": 2, "label": "asd"}, {"value": "asd3", "label": "asd"}]',
                            },
                        ]
                    },
                }
            },
        }

        serializer = serializers.OfferingOptionsSerializer(data=invalid_payload)

        # This should not raise a TypeError, but return validation errors properly
        is_valid = serializer.is_valid()

        # The serializer should be invalid due to incorrect choices_map format
        self.assertFalse(is_valid)

        # But we should be able to access errors without JSON serialization issues
        errors = serializer.errors
        self.assertIsInstance(errors, dict)

        # Verify that the errors are properly structured with helpful message
        self.assertIn("options", errors)
        self.assertIn("conditional", errors["options"])

        # Check that the error message is helpful
        error_str = str(errors)
        self.assertIn("choices_map must be a JSON object", error_str)

    def test_exact_user_payload_that_failed(self):
        """Test the exact payload from the user's error log"""
        # This is the EXACT payload that caused the TypeError: Dict key must be str
        user_payload = {
            "order": ["cond"],
            "options": {
                "cond": {
                    "type": "conditional_cascade",
                    "label": "Cond",
                    "required": False,
                    "name": "cond",
                    "cascade_config": {
                        "steps": [
                            {
                                "name": "f1",
                                "type": "select_string",
                                "label": "f1",
                                "choices": '[{"label":"asdasd","value":"asd"},{"label":"asdasd2","value":"2"}]',
                            },
                            {
                                "name": "f2",
                                "label": "F2",
                                "type": "select_string",
                                "depends_on": "f1",
                                # This is WRONG - should be an object, not an array
                                "choices_map": '[{"label":"asdasd2","value":"asd4"},{"label":"asdasd3","value":"25"}]',
                            },
                        ]
                    },
                }
            },
        }

        serializer = serializers.OfferingOptionsSerializer(data=user_payload)

        # This should not raise a TypeError, but return validation errors properly
        is_valid = serializer.is_valid()

        # The serializer should be invalid due to incorrect choices_map format
        self.assertFalse(is_valid)

        # But we should be able to access errors without JSON serialization issues
        errors = serializer.errors
        self.assertIsInstance(errors, dict)

        # The error should be clear about the choices_map format issue
        error_str = str(errors)
        self.assertIn("choices_map must be a JSON object", error_str)

        # Verify that all error keys are strings (important for JSON serialization)
        import json

        try:
            # This should not raise TypeError: Dict key must be str
            json_errors = json.dumps(errors)
            self.assertIsInstance(json_errors, str)
        except TypeError as e:
            if "Dict key must be str" in str(e):
                self.fail(
                    "Error dictionary contains non-string keys that prevent JSON serialization"
                )
            raise

    def test_multiple_step_validation_errors(self):
        """Test that multiple validation errors in steps don't cause serialization issues"""
        payload_with_multiple_errors = {
            "order": ["multi_error"],
            "options": {
                "multi_error": {
                    "type": "conditional_cascade",
                    "label": "Multi Error Test",
                    "cascade_config": {
                        "steps": [
                            {
                                # Missing required fields: name, type
                                "label": "Incomplete Step 1",
                                "choices": "invalid json",  # Invalid JSON
                            },
                            {
                                # Missing required fields: name, type
                                "label": "Incomplete Step 2",
                                "depends_on": "nonexistent",
                                "choices_map": "also invalid json",  # Invalid JSON
                            },
                        ]
                    },
                }
            },
        }

        serializer = serializers.OfferingOptionsSerializer(
            data=payload_with_multiple_errors
        )

        # Should be invalid due to multiple errors
        is_valid = serializer.is_valid()
        self.assertFalse(is_valid)

        # Should be able to access errors without JSON serialization issues
        errors = serializer.errors
        self.assertIsInstance(errors, dict)

        # Verify JSON serialization works (no integer keys)
        import json

        try:
            json_errors = json.dumps(errors)
            self.assertIsInstance(json_errors, str)
        except TypeError as e:
            if "Dict key must be str" in str(e):
                self.fail(f"Error dictionary contains non-string keys: {errors}")
            raise

    def test_valid_conditional_cascade_from_frontend(self):
        """Test a corrected version of the frontend payload"""
        valid_payload = {
            "order": ["conditional"],
            "options": {
                "conditional": {
                    "type": "conditional_cascade",
                    "label": "Conditional",
                    "cascade_config": {
                        "steps": [
                            {
                                "name": "ints",
                                "label": "Numbers",
                                "type": "select_string",
                                "choices": '[{"value": "2", "label": "Two"}, {"value": "3", "label": "Three"}]',
                            },
                            {
                                "name": "f2",
                                "label": "Second Field",
                                "type": "select_string_multi",
                                "depends_on": "ints",
                                "choices_map": '{"2": [{"value": "2a", "label": "2A"}], "3": [{"value": "3a", "label": "3A"}]}',
                            },
                        ]
                    },
                }
            },
        }

        serializer = serializers.OfferingOptionsSerializer(data=valid_payload)

        # This should be valid
        is_valid = serializer.is_valid()
        self.assertTrue(is_valid, serializer.errors)

        # Verify the validated data
        validated_data = serializer.validated_data
        self.assertEqual(validated_data["order"], ["conditional"])
        self.assertEqual(
            validated_data["options"]["conditional"]["type"], "conditional_cascade"
        )
