from rest_framework import test

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import serializers


class ConditionalCascadeEdgeCasesTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    def test_cascade_config_with_unicode_characters(self):
        """Test that cascade configs handle unicode characters properly"""
        cascade_config = {
            "steps": [
                {
                    "name": "región",
                    "label": "Región",
                    "type": "select_string",
                    "choices": '[{"value": "españa", "label": "España"}, {"value": "méxico", "label": "México"}]',
                },
                {
                    "name": "ciudad",
                    "label": "Ciudad",
                    "type": "select_string",
                    "depends_on": "región",
                    "choices_map": '{"españa": [{"value": "madrid", "label": "Madrid"}], "méxico": [{"value": "ciudad_de_méxico", "label": "Ciudad de México"}]}',
                },
            ]
        }

        option_data = {
            "type": "conditional_cascade",
            "label": "Ubicación",
            "cascade_config": cascade_config,
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_cascade_config_with_numeric_values(self):
        """Test that cascade configs handle numeric values properly"""
        cascade_config = {
            "steps": [
                {
                    "name": "cpu_cores",
                    "label": "CPU Cores",
                    "type": "select_string",
                    "choices": '[{"value": "1", "label": "1 Core"}, {"value": "4", "label": "4 Cores"}, {"value": "8", "label": "8 Cores"}]',
                },
                {
                    "name": "memory_gb",
                    "label": "Memory (GB)",
                    "type": "select_string",
                    "depends_on": "cpu_cores",
                    "choices_map": '{"1": [{"value": "2", "label": "2 GB"}], "4": [{"value": "8", "label": "8 GB"}, {"value": "16", "label": "16 GB"}], "8": [{"value": "16", "label": "16 GB"}, {"value": "32", "label": "32 GB"}]}',
                },
            ]
        }

        option_data = {
            "type": "conditional_cascade",
            "label": "Server Configuration",
            "cascade_config": cascade_config,
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_cascade_config_with_special_characters(self):
        """Test that cascade configs handle special characters in values"""
        cascade_config = {
            "steps": [
                {
                    "name": "environment",
                    "label": "Environment",
                    "type": "select_string",
                    "choices": '[{"value": "dev-test", "label": "Development & Testing"}, {"value": "prod_live", "label": "Production/Live"}]',
                },
                {
                    "name": "subdomain",
                    "label": "Subdomain",
                    "type": "select_string",
                    "depends_on": "environment",
                    "choices_map": '{"dev-test": [{"value": "api-dev.example.com", "label": "api-dev.example.com"}], "prod_live": [{"value": "api.example.com", "label": "api.example.com"}]}',
                },
            ]
        }

        option_data = {
            "type": "conditional_cascade",
            "label": "Deployment Configuration",
            "cascade_config": cascade_config,
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_cascade_config_with_empty_choices_map(self):
        """Test that cascade configs handle empty choices_map properly"""
        cascade_config = {
            "steps": [
                {
                    "name": "region",
                    "label": "Region",
                    "type": "select_string",
                    "choices": '[{"value": "active", "label": "Active Region"}, {"value": "inactive", "label": "Inactive Region"}]',
                },
                {
                    "name": "datacenter",
                    "label": "Data Center",
                    "type": "select_string",
                    "depends_on": "region",
                    "choices_map": '{"active": [{"value": "dc1", "label": "DC1"}], "inactive": []}',  # Empty array for inactive
                },
            ]
        }

        option_data = {
            "type": "conditional_cascade",
            "label": "Region Selection",
            "cascade_config": cascade_config,
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_cascade_config_deeply_nested_dependencies(self):
        """Test that cascade configs support deep nesting (5+ levels)"""
        cascade_config = {
            "steps": [
                {
                    "name": "continent",
                    "label": "Continent",
                    "type": "select_string",
                    "choices": '[{"value": "na", "label": "North America"}]',
                },
                {
                    "name": "country",
                    "label": "Country",
                    "type": "select_string",
                    "depends_on": "continent",
                    "choices_map": '{"na": [{"value": "us", "label": "United States"}]}',
                },
                {
                    "name": "state",
                    "label": "State",
                    "type": "select_string",
                    "depends_on": "country",
                    "choices_map": '{"us": [{"value": "ca", "label": "California"}]}',
                },
                {
                    "name": "city",
                    "label": "City",
                    "type": "select_string",
                    "depends_on": "state",
                    "choices_map": '{"ca": [{"value": "san-francisco", "label": "San Francisco"}]}',
                },
                {
                    "name": "datacenter",
                    "label": "Data Center",
                    "type": "select_string",
                    "depends_on": "city",
                    "choices_map": '{"san-francisco": [{"value": "sfo-dc1", "label": "SFO-DC1"}]}',
                },
                {
                    "name": "rack",
                    "label": "Rack",
                    "type": "select_string",
                    "depends_on": "datacenter",
                    "choices_map": '{"sfo-dc1": [{"value": "rack-a1", "label": "Rack A1"}]}',
                },
            ]
        }

        option_data = {
            "type": "conditional_cascade",
            "label": "Detailed Location",
            "cascade_config": cascade_config,
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_cascade_config_with_select_string_multi(self):
        """Test that cascade configs support select_string_multi type"""
        cascade_config = {
            "steps": [
                {
                    "name": "services",
                    "label": "Base Services",
                    "type": "select_string",
                    "choices": '[{"value": "web", "label": "Web Server"}, {"value": "db", "label": "Database"}]',
                },
                {
                    "name": "addons",
                    "label": "Add-ons",
                    "type": "select_string_multi",  # Multi-select
                    "depends_on": "services",
                    "choices_map": '{"web": [{"value": "ssl", "label": "SSL Certificate"}, {"value": "cdn", "label": "CDN"}], "db": [{"value": "backup", "label": "Automated Backup"}, {"value": "replica", "label": "Read Replica"}]}',
                },
            ]
        }

        option_data = {
            "type": "conditional_cascade",
            "label": "Service Configuration",
            "cascade_config": cascade_config,
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_malformed_json_in_choices(self):
        """Test that malformed JSON in choices is properly rejected"""
        cascade_config = {
            "steps": [
                {
                    "name": "region",
                    "label": "Region",
                    "type": "select_string",
                    "choices": '[{"value": "us", "label": "US"}',  # Missing closing bracket
                },
            ]
        }

        option_data = {
            "type": "conditional_cascade",
            "label": "Bad JSON Test",
            "cascade_config": cascade_config,
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("choices must be valid JSON", str(serializer.errors))

    def test_malformed_json_in_choices_map(self):
        """Test that malformed JSON in choices_map is properly rejected"""
        cascade_config = {
            "steps": [
                {
                    "name": "region",
                    "label": "Region",
                    "type": "select_string",
                    "choices": '[{"value": "us", "label": "US"}]',
                },
                {
                    "name": "datacenter",
                    "label": "Data Center",
                    "type": "select_string",
                    "depends_on": "region",
                    "choices_map": '{"us": [{"value": "dc1", "label": "DC1"}',  # Missing closing brackets
                },
            ]
        }

        option_data = {
            "type": "conditional_cascade",
            "label": "Bad JSON Map Test",
            "cascade_config": cascade_config,
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("choices_map must be valid JSON", str(serializer.errors))

    def test_choices_wrong_format(self):
        """Test that choices in wrong format (not array of objects) are rejected"""
        cascade_config = {
            "steps": [
                {
                    "name": "region",
                    "label": "Region",
                    "type": "select_string",
                    "choices": '["us", "eu"]',  # Wrong format - should be objects with value/label
                },
            ]
        }

        option_data = {
            "type": "conditional_cascade",
            "label": "Wrong Format Test",
            "cascade_config": cascade_config,
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertFalse(serializer.is_valid())
        error_str = str(serializer.errors)
        self.assertIn("Choice", error_str)
        self.assertIn(
            "must be an object with 'value' and 'label' properties", error_str
        )
