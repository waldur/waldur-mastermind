import uuid
from unittest.mock import Mock, patch

from django.test import TestCase

from waldur_core.core.tasks import PollRuntimeStateTask, RuntimeStateException
from waldur_core.structure.tests import factories


class MockMinimalInstance:
    """Mock instance with minimal attributes."""

    def __init__(self, pk=123):
        self.pk = pk
        self.__class__.__name__ = "MinimalResource"


class MockInstanceWithName:
    """Mock instance with name but no UUID."""

    def __init__(self, pk=456, name="test-resource"):
        self.pk = pk
        self.name = name
        self.__class__.__name__ = "NamedResource"


class MockInstanceWithUUID:
    """Mock instance with UUID but no name."""

    def __init__(self, pk=789, uuid_val=None):
        self.pk = pk
        self.uuid = uuid_val or uuid.uuid4()
        self.__class__.__name__ = "UUIDResource"


class MockFullInstance:
    """Mock instance with all attributes."""

    def __init__(self, pk=999, name="full-resource", uuid_val=None):
        self.pk = pk
        self.name = name
        self.uuid = uuid_val or uuid.uuid4()
        self.__class__.__name__ = "FullResource"


class MockInstanceWithEmptyValues:
    """Mock instance with empty/falsy attribute values."""

    def __init__(self, pk=111):
        self.pk = pk
        self.name = ""  # Empty string
        self.uuid = None  # None value
        self.__class__.__name__ = "EmptyResource"


class MockInstanceWithSpecialChars:
    """Mock instance with special characters in name."""

    def __init__(self, pk=222):
        self.pk = pk
        self.name = 'test "quoted" resource & more'
        self.uuid = uuid.uuid4()
        self.__class__.__name__ = "SpecialCharsResource"


class PollRuntimeStateTaskTestGetInstanceInfo(TestCase):
    """Test the _get_instance_info helper method."""

    def setUp(self):
        self.task = PollRuntimeStateTask()

    def test_minimal_instance(self):
        """Test instance with only pk and class name."""
        instance = MockMinimalInstance(pk=123)
        info = self.task._get_instance_info(instance)

        self.assertEqual(info["pk"], 123)
        self.assertEqual(info["class"], "MinimalResource")
        self.assertNotIn("uuid", info)
        self.assertNotIn("name", info)

    def test_instance_with_name_only(self):
        """Test instance with name but no UUID."""
        instance = MockInstanceWithName(pk=456, name="test-resource")
        info = self.task._get_instance_info(instance)

        self.assertEqual(info["pk"], 456)
        self.assertEqual(info["class"], "NamedResource")
        self.assertEqual(info["name"], "test-resource")
        self.assertNotIn("uuid", info)

    def test_instance_with_uuid_only(self):
        """Test instance with UUID but no name."""
        test_uuid = uuid.uuid4()
        instance = MockInstanceWithUUID(pk=789, uuid_val=test_uuid)
        info = self.task._get_instance_info(instance)

        self.assertEqual(info["pk"], 789)
        self.assertEqual(info["class"], "UUIDResource")
        self.assertEqual(info["uuid"], str(test_uuid))
        self.assertNotIn("name", info)

    def test_full_instance(self):
        """Test instance with all attributes."""
        test_uuid = uuid.uuid4()
        instance = MockFullInstance(pk=999, name="full-resource", uuid_val=test_uuid)
        info = self.task._get_instance_info(instance)

        self.assertEqual(info["pk"], 999)
        self.assertEqual(info["class"], "FullResource")
        self.assertEqual(info["name"], "full-resource")
        self.assertEqual(info["uuid"], str(test_uuid))

    def test_instance_with_empty_values(self):
        """Test instance with empty/falsy attribute values."""
        instance = MockInstanceWithEmptyValues(pk=111)
        info = self.task._get_instance_info(instance)

        self.assertEqual(info["pk"], 111)
        self.assertEqual(info["class"], "EmptyResource")
        # Empty string and None should not be included
        self.assertNotIn("name", info)
        self.assertNotIn("uuid", info)

    def test_instance_with_special_characters(self):
        """Test instance with special characters in name."""
        instance = MockInstanceWithSpecialChars(pk=222)
        info = self.task._get_instance_info(instance)

        self.assertEqual(info["pk"], 222)
        self.assertEqual(info["class"], "SpecialCharsResource")
        self.assertEqual(info["name"], 'test "quoted" resource & more')
        self.assertIn("uuid", info)

    def test_instance_without_pk_attribute(self):
        """Test instance without pk attribute (edge case)."""
        instance = Mock()
        del instance.pk  # Remove pk attribute
        instance.__class__.__name__ = "NoPKResource"

        info = self.task._get_instance_info(instance)

        self.assertEqual(info["pk"], "unknown")
        self.assertEqual(info["class"], "NoPKResource")

    def test_real_model_instance(self):
        """Test with actual Django model instance."""
        project = factories.ProjectFactory(name="test-project")
        info = self.task._get_instance_info(project)

        self.assertEqual(info["pk"], project.pk)
        self.assertEqual(info["class"], "Project")
        self.assertEqual(info["name"], "test-project")
        self.assertEqual(info["uuid"], str(project.uuid))


class PollRuntimeStateTaskTestLogging(TestCase):
    """Test the enhanced logging in execute method."""

    def setUp(self):
        self.task = PollRuntimeStateTask()

    @patch("waldur_core.core.tasks.logger")
    def test_info_log_with_full_instance(self, mock_logger):
        """Test info logging with instance that has all attributes."""
        # Create mock instance with all attributes
        test_uuid = uuid.uuid4()
        instance = MockFullInstance(pk=999, name="test-vm", uuid_val=test_uuid)
        instance.runtime_state = "creating"
        instance.get_backend = Mock(return_value=Mock())
        instance.refresh_from_db = Mock()

        # Mock the backend method to do nothing
        backend = instance.get_backend.return_value
        backend.pull_state = Mock()

        # Mock retry to avoid actual retry behavior
        self.task.retry = Mock(side_effect=Exception("Task retried"))

        # Call execute and expect it to retry (and raise our mock exception)
        with self.assertRaises(Exception, msg="Task retried"):
            self.task.execute(
                instance, "pull_state", success_state="active", erred_state="error"
            )

        # Verify logging was called with enhanced message
        mock_logger.info.assert_called_once()
        log_call = mock_logger.info.call_args[0][0]
        self.assertIn("FullResource (PK: 999", log_call)
        self.assertIn(f"UUID: {test_uuid}", log_call)
        self.assertIn('name: "test-vm"', log_call)
        self.assertIn("runtime state (creating)", log_call)

    def test_runtime_exception_with_minimal_instance(self):
        """Test RuntimeStateException with minimal instance info."""
        instance = MockMinimalInstance(pk=123)
        instance.runtime_state = "error"
        instance.get_backend = Mock(return_value=Mock())
        instance.refresh_from_db = Mock()

        # Mock the backend method
        backend = instance.get_backend.return_value
        backend.pull_state = Mock()

        # Call execute and expect RuntimeStateException
        with self.assertRaises(RuntimeStateException) as cm:
            self.task.execute(
                instance, "pull_state", success_state="active", erred_state="error"
            )

        # Verify exception message contains enhanced info
        exception_msg = str(cm.exception)
        self.assertIn("MinimalResource (PK: 123", exception_msg)
        self.assertNotIn("UUID:", exception_msg)  # Should not contain UUID
        self.assertNotIn("name:", exception_msg)  # Should not contain name

    @patch("waldur_core.core.tasks.logger")
    def test_info_log_with_minimal_instance(self, mock_logger):
        """Test info logging with minimal instance."""
        instance = MockMinimalInstance(pk=456)
        instance.runtime_state = "pending"
        instance.get_backend = Mock(return_value=Mock())
        instance.refresh_from_db = Mock()

        # Mock the backend method
        backend = instance.get_backend.return_value
        backend.pull_state = Mock()

        # Mock retry
        self.task.retry = Mock(side_effect=Exception("Task retried"))

        # Call execute and expect retry
        with self.assertRaises(Exception, msg="Task retried"):
            self.task.execute(
                instance, "pull_state", success_state="active", erred_state="error"
            )

        # Verify logging was called with basic info only
        mock_logger.info.assert_called_once()
        log_call = mock_logger.info.call_args[0][0]
        self.assertIn("MinimalResource (PK: 456", log_call)
        self.assertNotIn("UUID:", log_call)
        self.assertNotIn("name:", log_call)

    def test_successful_execution_no_logging(self):
        """Test that successful execution doesn't trigger enhanced logging."""
        instance = MockFullInstance(pk=999)
        instance.runtime_state = "active"  # Success state
        instance.get_backend = Mock(return_value=Mock())
        instance.refresh_from_db = Mock()

        # Mock the backend method
        backend = instance.get_backend.return_value
        backend.pull_state = Mock()

        # Call execute - should succeed without logging
        with patch("waldur_core.core.tasks.logger") as mock_logger:
            result = self.task.execute(
                instance, "pull_state", success_state="active", erred_state="error"
            )

            # Should return the instance
            self.assertEqual(result, instance)

            # Should not have called info logging
            mock_logger.info.assert_not_called()

    def test_logging_handles_special_characters_safely(self):
        """Test that logging handles special characters in names without errors."""
        instance = MockInstanceWithSpecialChars(pk=333)
        instance.runtime_state = "updating"
        instance.get_backend = Mock(return_value=Mock())
        instance.refresh_from_db = Mock()

        # Mock the backend method
        backend = instance.get_backend.return_value
        backend.pull_state = Mock()

        # Mock retry
        self.task.retry = Mock(side_effect=Exception("Task retried"))

        # This should not raise any formatting errors
        with patch("waldur_core.core.tasks.logger") as mock_logger:
            with self.assertRaises(Exception, msg="Task retried"):
                self.task.execute(
                    instance, "pull_state", success_state="active", erred_state="error"
                )

            # Verify logging worked despite special characters
            mock_logger.info.assert_called_once()
            log_call = mock_logger.info.call_args[0][0]
            self.assertIn('test "quoted" resource & more', log_call)


class PollRuntimeStateTaskTestEdgeCases(TestCase):
    """Test edge cases and error scenarios."""

    def setUp(self):
        self.task = PollRuntimeStateTask()

    def test_instance_with_non_string_attributes(self):
        """Test instance where attributes are not strings."""
        instance = Mock()
        instance.pk = 555
        instance.name = 123  # Non-string name
        instance.uuid = "not-a-uuid-object"  # String instead of UUID
        instance.__class__.__name__ = "WeirdResource"

        info = self.task._get_instance_info(instance)

        # Should still work and convert to strings
        self.assertEqual(info["pk"], 555)
        self.assertEqual(info["class"], "WeirdResource")
        self.assertEqual(info["name"], 123)  # Should include truthy values
        self.assertEqual(info["uuid"], "not-a-uuid-object")

    def test_instance_with_property_attributes(self):
        """Test instance where name/uuid are properties."""

        class PropertyInstance:
            def __init__(self):
                self.pk = 777
                self._name = "property-resource"
                self._uuid = uuid.uuid4()

            @property
            def name(self):
                return self._name

            @property
            def uuid(self):
                return self._uuid

        instance = PropertyInstance()
        info = self.task._get_instance_info(instance)

        self.assertEqual(info["pk"], 777)
        self.assertEqual(info["class"], "PropertyInstance")
        self.assertEqual(info["name"], "property-resource")
        self.assertEqual(info["uuid"], str(instance.uuid))


class PollRuntimeStateTaskTestRealResources(TestCase):
    """Test with real resource models from Waldur plugins."""

    def setUp(self):
        self.task = PollRuntimeStateTask()

    def test_with_project_model(self):
        """Test _get_instance_info with Project model."""
        project = factories.ProjectFactory(name="integration-test-project")
        info = self.task._get_instance_info(project)

        self.assertEqual(info["pk"], project.pk)
        self.assertEqual(info["class"], "Project")
        self.assertEqual(info["name"], "integration-test-project")
        self.assertEqual(info["uuid"], str(project.uuid))

    def test_with_customer_model(self):
        """Test _get_instance_info with Customer model."""
        customer = factories.CustomerFactory(name="integration-test-customer")
        info = self.task._get_instance_info(customer)

        self.assertEqual(info["pk"], customer.pk)
        self.assertEqual(info["class"], "Customer")
        self.assertEqual(info["name"], "integration-test-customer")
        self.assertEqual(info["uuid"], str(customer.uuid))

    def test_with_service_settings_model(self):
        """Test _get_instance_info with ServiceSettings model."""
        settings = factories.ServiceSettingsFactory(name="test-settings")
        info = self.task._get_instance_info(settings)

        self.assertEqual(info["pk"], settings.pk)
        self.assertEqual(info["class"], "ServiceSettings")
        self.assertEqual(info["name"], "test-settings")
        self.assertEqual(info["uuid"], str(settings.uuid))

    @patch("waldur_core.core.tasks.logger")
    def test_integration_logging_with_real_model(self, mock_logger):
        """Integration test: actual logging with real Django model."""
        project = factories.ProjectFactory(name="log-test-project")
        project.runtime_state = "updating"
        project.get_backend = Mock(return_value=Mock())
        project.refresh_from_db = Mock()

        # Mock the backend method
        backend = project.get_backend.return_value
        backend.pull_state = Mock()

        # Mock retry
        self.task.retry = Mock(side_effect=Exception("Task retried"))

        # Execute and verify no exceptions are raised during logging
        with self.assertRaises(Exception, msg="Task retried"):
            self.task.execute(
                project, "pull_state", success_state="active", erred_state="error"
            )

        # Verify logging worked with real model
        mock_logger.info.assert_called_once()
        log_call = mock_logger.info.call_args[0][0]
        self.assertIn("Project (PK:", log_call)
        self.assertIn("UUID:", log_call)
        self.assertIn('name: "log-test-project"', log_call)
        self.assertIn("runtime state (updating)", log_call)

    def test_logging_with_unicode_characters(self):
        """Test logging handles Unicode characters correctly."""
        # Create project with Unicode characters in name
        project = factories.ProjectFactory(name="тест-проект-🚀")
        project.runtime_state = "creating"
        project.get_backend = Mock(return_value=Mock())
        project.refresh_from_db = Mock()

        # Mock the backend method
        backend = project.get_backend.return_value
        backend.pull_state = Mock()

        # Mock retry
        self.task.retry = Mock(side_effect=Exception("Task retried"))

        # This should not raise encoding/decoding errors
        with patch("waldur_core.core.tasks.logger") as mock_logger:
            with self.assertRaises(Exception, msg="Task retried"):
                self.task.execute(
                    project, "pull_state", success_state="active", erred_state="error"
                )

            # Verify logging worked with Unicode
            mock_logger.info.assert_called_once()
            log_call = mock_logger.info.call_args[0][0]
            self.assertIn("тест-проект-🚀", log_call)
