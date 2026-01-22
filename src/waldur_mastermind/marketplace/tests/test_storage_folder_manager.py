from rest_framework import test

from waldur_mastermind.common.serializers import validate_options
from waldur_mastermind.marketplace import serializers


class StorageFolderManagerTest(test.APITestCase):
    def test_valid_configuration(self):
        """Test that valid configurations are accepted"""
        option_data = {
            "type": "storage_folder_manager",
            "label": "Storage Configuration",
            "storage_folder_config": {
                "component_type": "storage",
                "default_hard_quota_multiplier": 1.2,
                "inode_multiplier": 7000,
                "storage_data_types": [
                    {"key": "store", "label": "Store"},
                    {"key": "archive", "label": "Archive"},
                ],
                "permissions": [
                    {
                        "value": "2770",
                        "label": "2770 - Group write, setgid",
                        "enabled_for_data_types": ["store"],
                        "default_for_data_types": ["store"],
                    }
                ],
            },
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_missing_config_fails(self):
        """Test that missing storage_folder_config fails validation"""
        option_data = {
            "type": "storage_folder_manager",
            "label": "Storage Configuration",
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("storage_folder_config is required", str(serializer.errors))

    def test_duplicate_data_type_keys_fails(self):
        """Test that duplicate storage data type keys fail validation"""
        option_data = {
            "type": "storage_folder_manager",
            "label": "Storage Configuration",
            "storage_folder_config": {
                "component_type": "storage",
                "storage_data_types": [
                    {"key": "store", "label": "Store"},
                    {"key": "store", "label": "Store Duplicate"},  # Duplicate key
                ],
                "permissions": [{"value": "2770", "label": "2770 - Group write"}],
            },
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertFalse(serializer.is_valid())

    def test_order_validation(self):
        """Test that order attributes are validated correctly"""
        options = {
            "storage_config": {
                "type": "storage_folder_manager",
                "label": "Storage Configuration",
                "required": True,
            }
        }

        attributes = {
            "storage_config": {
                "storage_data_type": "store",
                "permissions": "2770",
                "hard_quota_space": 15.0,
                "soft_quota_inodes": 105000,
                "hard_quota_inodes": 105000,
            }
        }

        try:
            validate_options(options, attributes)
        except Exception as e:
            self.fail(f"validate_options should accept storage_folder_manager: {e}")

    def test_order_validation_missing_required_field(self):
        """Test that missing required fields fail validation"""
        options = {
            "storage_config": {
                "type": "storage_folder_manager",
                "label": "Storage Configuration",
                "required": True,
            }
        }

        attributes = {
            "storage_config": {
                "storage_data_type": "store",
                # Missing permissions field
                "hard_quota_space": 15.0,
            }
        }

        with self.assertRaises(Exception):
            validate_options(options, attributes)

    def test_order_validation_negative_quota(self):
        """Test that negative quotas fail validation"""
        options = {
            "storage_config": {
                "type": "storage_folder_manager",
                "label": "Storage Configuration",
                "required": True,
            }
        }

        attributes = {
            "storage_config": {
                "storage_data_type": "store",
                "permissions": "2770",
                "hard_quota_space": -5.0,  # Negative quota should fail
            }
        }

        with self.assertRaises(Exception):
            validate_options(options, attributes)
