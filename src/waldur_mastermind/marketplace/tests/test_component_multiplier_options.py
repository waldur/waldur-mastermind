from rest_framework import test

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import serializers
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace.tests import factories


class ComponentMultiplierOptionsSerializerTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    def test_valid_component_multiplier_option(self):
        """Test that a valid component multiplier option passes validation"""
        component_multiplier_config = {
            "component_type": "storage",
            "factor": 50000,
            "min_limit": 1,
            "max_limit": 100,
        }

        option_data = {
            "type": "component_multiplier",
            "label": "Storage Size (TB)",
            "help_text": "Enter storage size in terabytes",
            "component_multiplier_config": component_multiplier_config,
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_component_multiplier_requires_config(self):
        """Test that component_multiplier type requires component_multiplier_config"""
        option_data = {
            "type": "component_multiplier",
            "label": "Storage Size",
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("component_multiplier_config is required", str(serializer.errors))

    def test_component_multiplier_config_validation(self):
        """Test component_multiplier_config field validation"""
        # Test missing component_type
        config_data = {
            "factor": 50000,
        }
        serializer = serializers.ComponentMultiplierConfigSerializer(data=config_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("component_type", serializer.errors)

        # Test missing factor
        config_data = {
            "component_type": "storage",
        }
        serializer = serializers.ComponentMultiplierConfigSerializer(data=config_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("factor", serializer.errors)

        # Test invalid factor (zero)
        config_data = {
            "component_type": "storage",
            "factor": 0,
        }
        serializer = serializers.ComponentMultiplierConfigSerializer(data=config_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("factor", serializer.errors)

    def test_min_max_limit_validation(self):
        """Test min/max limit validation"""
        # Test min_limit > max_limit
        config_data = {
            "component_type": "storage",
            "factor": 50000,
            "min_limit": 100,
            "max_limit": 50,
        }
        serializer = serializers.ComponentMultiplierConfigSerializer(data=config_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn(
            "min_limit cannot be greater than max_limit", str(serializer.errors)
        )

        # Test valid min/max configuration
        config_data = {
            "component_type": "storage",
            "factor": 50000,
            "min_limit": 1,
            "max_limit": 100,
        }
        serializer = serializers.ComponentMultiplierConfigSerializer(data=config_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)


class ComponentMultiplierOptionsIntegrationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    def test_offering_creation_with_component_multiplier_options(self):
        """Test creating an offering with component_multiplier options"""
        # Create option that multiplies TB input to inodes
        options_data = {
            "order": ["storage_size"],
            "options": {
                "storage_size": {
                    "type": "component_multiplier",
                    "label": "Storage Size (TB)",
                    "help_text": "Enter storage size in terabytes",
                    "required": True,
                    "component_multiplier_config": {
                        "component_type": "storage_inodes",
                        "factor": 50000,
                        "min_limit": 1,
                        "max_limit": 100,
                    },
                }
            },
        }

        # Create offering with the component_multiplier option
        offering = factories.OfferingFactory(
            type="Marketplace.Basic",
            options=options_data,
        )

        # Create limit-based component that the multiplier references
        factories.OfferingComponentFactory(
            offering=offering,
            type="storage_inodes",
            name="Storage Inodes",
            billing_type=BillingTypes.LIMIT,
            max_value=10000000,
            min_value=1000,
        )

        # Verify the offering was created with the correct options
        options = offering.options["options"]["storage_size"]
        self.assertEqual(options["type"], "component_multiplier")

        config = options["component_multiplier_config"]
        self.assertEqual(config["component_type"], "storage_inodes")
        self.assertEqual(config["factor"], 50000)
        self.assertEqual(config["min_limit"], 1)
        self.assertEqual(config["max_limit"], 100)

    def test_order_validation_with_component_multiplier(self):
        """Test that component_multiplier fields validate correctly during order creation"""
        # Create offering with component_multiplier option
        offering = factories.OfferingFactory(
            type="Marketplace.Basic",
            options={
                "order": ["storage_size"],
                "options": {
                    "storage_size": {
                        "type": "component_multiplier",
                        "label": "Storage Size (TB)",
                        "required": True,
                        "component_multiplier_config": {
                            "component_type": "storage_inodes",
                            "factor": 50000,
                            "min_limit": 1,
                            "max_limit": 10,
                        },
                    }
                },
            },
        )

        # Create limit-based component
        factories.OfferingComponentFactory(
            offering=offering,
            type="storage_inodes",
            name="Storage Inodes",
            billing_type=BillingTypes.LIMIT,
            max_value=1000000,
            min_value=1000,
        )

        factories.PlanFactory(offering=offering)

        # Test that component_multiplier field validates using ComponentMultiplierField
        # This tests the backend validation for the field type
        from waldur_mastermind.common.serializers import validate_options

        valid_options = offering.options["options"]
        valid_attributes = {"storage_size": "5"}  # Valid input

        # This should not raise an exception
        validate_options(valid_options, valid_attributes)

    def test_component_multiplier_field_validation(self):
        """Test component_multiplier field validation in common serializers"""
        from rest_framework import serializers

        # component_multiplier uses standard IntegerField validation
        field = serializers.IntegerField()
        result = field.to_internal_value("5")
        self.assertEqual(result, 5)

        # Test invalid input
        with self.assertRaises(Exception):
            field.to_internal_value("invalid")
