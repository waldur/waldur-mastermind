from django.test import TestCase
from rest_framework import serializers

from waldur_mastermind.common.serializers import (
    get_hidden_options,
    transform_option_value,
    transform_option_values,
    validate_options,
)


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


class OptionVisibilityTest(TestCase):
    options = {
        "backups": {"type": "boolean", "label": "Backups"},
        "account": {
            "type": "select_string",
            "label": "Account",
            "choices": ["own", "new"],
            "required": True,
            "visible_if": {"field": "backups", "values": [True]},
        },
        "bucket": {
            "type": "string",
            "label": "Bucket",
            "required": True,
            "visible_if": {"field": "account", "values": ["own"]},
        },
        "kinds": {
            "type": "select_string_multi",
            "label": "Kinds",
            "choices": ["a", "b", "c"],
        },
        "note": {
            "type": "string",
            "label": "Note",
            "visible_if": {"field": "kinds", "values": ["b", "c"]},
        },
    }

    def test_options_without_parent_value_are_hidden(self):
        self.assertEqual(
            get_hidden_options(self.options, {}), {"account", "bucket", "note"}
        )

    def test_hiding_cascades(self):
        self.assertEqual(
            get_hidden_options(self.options, {"backups": False, "account": "own"}),
            {"account", "bucket", "note"},
        )
        self.assertEqual(
            get_hidden_options(self.options, {"backups": True, "account": "own"}),
            {"note"},
        )
        self.assertEqual(
            get_hidden_options(self.options, {"backups": True, "account": "new"}),
            {"bucket", "note"},
        )

    def test_boolean_parent_accepts_string_form(self):
        self.assertNotIn(
            "account", get_hidden_options(self.options, {"backups": "true"})
        )
        self.assertIn("account", get_hidden_options(self.options, {"backups": 1.5}))

    def test_missing_boolean_parent_counts_as_unchecked(self):
        options = {
            "backups": {"type": "boolean", "label": "Backups"},
            "reason": {
                "type": "string",
                "label": "Why no backups?",
                "visible_if": {"field": "backups", "values": [False]},
            },
        }
        self.assertEqual(get_hidden_options(options, {}), set())
        self.assertEqual(get_hidden_options(options, {"backups": None}), set())
        self.assertEqual(get_hidden_options(options, {"backups": True}), {"reason"})

    def test_multi_select_parent_matches_any_value(self):
        self.assertNotIn(
            "note", get_hidden_options(self.options, {"kinds": ["a", "c"]})
        )
        self.assertIn("note", get_hidden_options(self.options, {"kinds": ["a"]}))
        self.assertIn("note", get_hidden_options(self.options, {"kinds": 5}))

    def test_unknown_parent_and_cycles_hide_option(self):
        options = {
            "x": {"type": "boolean", "visible_if": {"field": "y", "values": [True]}},
            "y": {"type": "boolean", "visible_if": {"field": "x", "values": [True]}},
            "z": {"type": "boolean", "visible_if": {"field": "q", "values": [True]}},
            "legacy": None,
        }
        self.assertEqual(
            get_hidden_options(options, {"x": True, "y": True, "q": True}),
            {"x", "y", "z"},
        )

    def test_validate_options_skips_and_drops_hidden_options(self):
        attributes = {"backups": False, "account": "invalid", "name": "x"}
        self.assertEqual(
            validate_options(self.options, attributes),
            {"backups": False, "name": "x"},
        )

    def test_validate_options_requires_visible_options(self):
        with self.assertRaises(serializers.ValidationError):
            validate_options(self.options, {"backups": True})

    def test_validate_options_without_rules_returns_attributes_unchanged(self):
        options = {"field1": {"type": "integer", "required": True}}
        attributes = {"field1": 1, "other": 2}
        self.assertEqual(validate_options(options, attributes), attributes)


class OptionPatternTest(TestCase):
    options = {
        "slug": {"type": "string", "pattern": "[a-z]+", "pattern_error": "Bad slug"},
        "count": {"type": "integer", "pattern": "[a-z]+"},
    }

    def test_matching_value_is_accepted(self):
        attributes = {"slug": "abc"}
        self.assertEqual(validate_options(self.options, attributes), attributes)

    def test_partial_match_is_rejected_with_provider_message(self):
        with self.assertRaisesMessage(serializers.ValidationError, "Bad slug"):
            validate_options(self.options, {"slug": "abc1"})

    def test_value_is_not_trimmed_before_matching(self):
        with self.assertRaises(serializers.ValidationError):
            validate_options(self.options, {"slug": "abc "})

    def test_pattern_is_ignored_for_other_types(self):
        validate_options(self.options, {"slug": "abc", "count": 5})

    def test_slow_pattern_times_out_and_is_logged(self):
        options = {"slug": {"type": "string", "pattern": "(x+x+)+y"}}
        with self.assertLogs(
            "waldur_mastermind.common.serializers", level="WARNING"
        ) as logs:
            with self.assertRaisesMessage(
                serializers.ValidationError, "contact the service provider"
            ):
                validate_options(options, {"slug": "x" * 5000})
        self.assertIn("slug", logs.output[0])
        self.assertIn("(x+x+)+y", logs.output[0])

    def test_character_classes_match_ascii_only_as_in_javascript(self):
        options = {
            "word": {"type": "string", "pattern": r"\w+"},
            "digit": {"type": "string", "pattern": r"\d+"},
        }
        validate_options(options, {"word": "cheese", "digit": "42"})
        with self.assertRaises(serializers.ValidationError):
            validate_options(options, {"word": "käse"})
        with self.assertRaises(serializers.ValidationError):
            validate_options(options, {"digit": "٣"})


class OptionTransformTest(TestCase):
    transform = {"prefix": "acme-", "suffix": "-prod", "case": "lower"}
    options = {
        "bucket": {
            "type": "string",
            "pattern": "[A-Za-z]+",
            "transform": {"prefix": "acme-", "suffix": "-prod", "case": "lower"},
        },
        "count": {"type": "integer", "transform": {"prefix": "acme-"}},
    }

    def test_prefix_suffix_and_case_are_applied(self):
        self.assertEqual(
            transform_option_value(self.transform, "Backups"), "acme-backups-prod"
        )

    def test_transforming_twice_gives_the_same_value(self):
        once = transform_option_value(self.transform, "Backups")
        self.assertEqual(transform_option_value(self.transform, once), once)

    def test_case_does_not_change_prefix_and_suffix(self):
        transform = {"prefix": "Acme-", "case": "upper"}
        self.assertEqual(transform_option_value(transform, "logs"), "Acme-LOGS")

    def test_value_typed_with_the_prefix_does_not_get_it_twice(self):
        self.assertEqual(
            transform_option_value({"prefix": "acme-"}, "acme-logs"), "acme-logs"
        )

    def test_validate_options_returns_transformed_values(self):
        attributes = {"bucket": "Backups", "other": "kept"}
        self.assertEqual(
            validate_options(self.options, attributes),
            {"bucket": "acme-backups-prod", "other": "kept"},
        )
        # The submitted values are not modified.
        self.assertEqual(attributes["bucket"], "Backups")

    def test_pattern_is_checked_against_the_typed_part(self):
        # "acme-" and "-prod" contain dashes the pattern does not allow.
        self.assertEqual(
            validate_options(self.options, {"bucket": "acme-Backups-prod"}),
            {"bucket": "acme-backups-prod"},
        )
        with self.assertRaises(serializers.ValidationError):
            validate_options(self.options, {"bucket": "back-ups"})

    def test_prefix_alone_is_not_a_value(self):
        with self.assertRaises(serializers.ValidationError) as e:
            validate_options(self.options, {"bucket": "acme-"})
        self.assertEqual(
            str(e.exception.detail["bucket"]),
            "Enter a value besides the prefix and suffix.",
        )

    def test_prefix_typed_in_another_case_is_not_added_twice(self):
        self.assertEqual(
            transform_option_value(self.transform, "ACME-Logs-PROD"),
            "acme-logs-prod",
        )
        self.assertEqual(
            validate_options(self.options, {"bucket": "ACME-Logs"}),
            {"bucket": "acme-logs-prod"},
        )

    def test_prefix_case_matters_without_a_case_transform(self):
        self.assertEqual(
            transform_option_value({"prefix": "acme-"}, "ACME-logs"),
            "acme-ACME-logs",
        )

    def test_transform_option_values_does_not_validate(self):
        values = {"bucket": "back-ups", "count": 5}
        transformed = transform_option_values(self.options, values)
        self.assertEqual(transformed, {"bucket": "acme-back-ups-prod", "count": 5})
        self.assertEqual(
            transform_option_values(self.options, transformed), transformed
        )

    def test_empty_value_is_not_transformed(self):
        options = {"bucket": {"type": "string", "transform": {"prefix": "acme-"}}}
        self.assertEqual(validate_options(options, {}), {})
        with self.assertRaises(serializers.ValidationError):
            validate_options(options, {"bucket": ""})

    def test_transform_is_ignored_for_other_types(self):
        self.assertEqual(
            validate_options(self.options, {"bucket": "logs", "count": 5}),
            {"bucket": "acme-logs-prod", "count": 5},
        )
