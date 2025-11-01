from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import serializers
from waldur_mastermind.marketplace.tests import factories


class ConditionalCascadeOptionsSerializerTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    def test_valid_conditional_cascade_option(self):
        """Test that a valid conditional cascade option passes validation"""
        cascade_config = {
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
                    "choices_map": '{"us": [{"value": "us-east-1", "label": "US East (Virginia)"}, {"value": "us-west-2", "label": "US West (Oregon)"}], "eu": [{"value": "eu-west-1", "label": "Europe (Ireland)"}]}',
                },
                {
                    "name": "rack",
                    "label": "Rack",
                    "type": "select_string",
                    "depends_on": "datacenter",
                    "choices_map": '{"us-east-1": [{"value": "rack-1a", "label": "Rack 1A"}, {"value": "rack-1b", "label": "Rack 1B"}], "us-west-2": [{"value": "rack-2a", "label": "Rack 2A"}]}',
                },
            ]
        }

        option_data = {
            "type": "conditional_cascade",
            "label": "Location Selection",
            "help_text": "Select country, data center, and rack",
            "cascade_config": cascade_config,
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_conditional_cascade_requires_cascade_config(self):
        """Test that conditional_cascade type requires cascade_config"""
        option_data = {
            "type": "conditional_cascade",
            "label": "Location Selection",
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("cascade_config is required", str(serializer.errors))

    def test_cascade_config_requires_steps(self):
        """Test that cascade_config requires at least one step"""
        option_data = {
            "type": "conditional_cascade",
            "label": "Location Selection",
            "cascade_config": {"steps": []},
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("At least one step is required", str(serializer.errors))

    def test_step_names_must_be_unique(self):
        """Test that step names must be unique within cascade"""
        cascade_config = {
            "steps": [
                {
                    "name": "country",
                    "label": "Country 1",
                    "type": "select_string",
                    "choices": '[{"value": "us", "label": "United States"}]',
                },
                {
                    "name": "country",  # Duplicate name
                    "label": "Country 2",
                    "type": "select_string",
                    "choices": '[{"value": "eu", "label": "European Union"}]',
                },
            ]
        }

        option_data = {
            "type": "conditional_cascade",
            "label": "Location Selection",
            "cascade_config": cascade_config,
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("Step names must be unique", str(serializer.errors))

    def test_dependency_must_exist_before_dependent_step(self):
        """Test that dependencies must be defined before dependent steps"""
        cascade_config = {
            "steps": [
                {
                    "name": "datacenter",
                    "label": "Data Center",
                    "type": "select_string",
                    "depends_on": "country",  # Depends on step not yet defined
                    "choices_map": '{"us": [{"value": "us-east", "label": "US East"}]}',
                },
                {
                    "name": "country",
                    "label": "Country",
                    "type": "select_string",
                    "choices": '[{"value": "us", "label": "United States"}]',
                },
            ]
        }

        option_data = {
            "type": "conditional_cascade",
            "label": "Location Selection",
            "cascade_config": cascade_config,
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("must be defined earlier", str(serializer.errors))

    def test_dependent_step_requires_choices_map(self):
        """Test that dependent steps require choices_map"""
        cascade_config = {
            "steps": [
                {
                    "name": "country",
                    "label": "Country",
                    "type": "select_string",
                    "choices": [{"value": "us", "label": "United States"}],
                },
                {
                    "name": "datacenter",
                    "label": "Data Center",
                    "type": "select_string",
                    "depends_on": "country",
                    # Missing choices_map
                },
            ]
        }

        option_data = {
            "type": "conditional_cascade",
            "label": "Location Selection",
            "cascade_config": cascade_config,
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("choices_map is required", str(serializer.errors))

    def test_independent_step_requires_choices(self):
        """Test that independent steps require choices"""
        cascade_config = {
            "steps": [
                {
                    "name": "country",
                    "label": "Country",
                    "type": "select_string",
                    # Missing choices
                },
            ]
        }

        option_data = {
            "type": "conditional_cascade",
            "label": "Location Selection",
            "cascade_config": cascade_config,
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("choices is required", str(serializer.errors))

    def test_offering_options_with_conditional_cascade(self):
        """Test that offering options can include conditional cascade fields"""
        options_data = {
            "order": ["location_selector"],
            "options": {
                "location_selector": {
                    "type": "conditional_cascade",
                    "label": "Location Selection",
                    "cascade_config": {
                        "steps": [
                            {
                                "name": "country",
                                "label": "Country",
                                "type": "select_string",
                                "choices": '[{"value": "us", "label": "United States"}]',
                            }
                        ]
                    },
                }
            },
        }

        serializer = serializers.OfferingOptionsSerializer(data=options_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)


class ConditionalCascadeOptionsIntegrationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        # Create a service provider for the customer
        self.service_provider = factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer)

    def test_offering_creation_with_conditional_cascade_options(self):
        """Test creating an offering with conditional cascade options"""
        self.client.force_authenticate(self.fixture.staff)

        cascade_options = {
            "order": ["location"],
            "options": {
                "location": {
                    "type": "conditional_cascade",
                    "label": "Location Selection",
                    "help_text": "Select your preferred location",
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
                                "choices_map": '{"us": [{"value": "us-east-1", "label": "US East"}], "eu": [{"value": "eu-west-1", "label": "EU West"}]}',
                            },
                        ]
                    },
                }
            },
        }

        category = factories.CategoryFactory()
        data = {
            "name": "Test Offering",
            "category": factories.CategoryFactory.get_url(category),
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "type": "Support.OfferingTemplate",
            "options": cascade_options,
        }

        response = self.client.post(factories.OfferingFactory.get_list_url(), data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        # Verify the offering was created with correct options
        offering_url = response.data["url"]
        response = self.client.get(offering_url)
        options = response.data["options"]

        self.assertEqual(options["options"]["location"]["type"], "conditional_cascade")
        self.assertEqual(
            len(options["options"]["location"]["cascade_config"]["steps"]), 2
        )
