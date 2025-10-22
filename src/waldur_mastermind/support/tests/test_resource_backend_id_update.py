from unittest import mock

from constance.test.unittest import override_config
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from waldur_core.core.tests.helpers import load_json_resource
from waldur_mastermind.marketplace.models import Order
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.support.backend.atlassian import ServiceDeskBackend
from waldur_mastermind.support.tests import fixtures


@override_config(ATLASSIAN_CUSTOM_ISSUE_FIELD_MAPPING_ENABLED=True)
class ResourceBackendIdUpdateTest(TestCase):
    """Test resource backend_id update functionality when custom field mapping is enabled."""

    def setUp(self):
        super().setUp()

        self.fixture = fixtures.SupportFixture()
        self.backend = ServiceDeskBackend()

        # Mock ServiceDesk client
        service_desk_patcher = mock.patch(
            "waldur_mastermind.support.backend.atlassian.ServiceDesk"
        )
        mocked_service_desk_class = service_desk_patcher.start()
        self.mocked_service_desk = mocked_service_desk_class.return_value
        self.backend.manager = self.mocked_service_desk

        # Create a real order for testing
        self.order = marketplace_factories.OrderFactory()

        # Set up issue connected to the order
        self.issue = self.fixture.issue
        self.issue.key = "TEST-123"
        self.issue.backend_id = "TEST-123"
        self.issue.resource_content_type = ContentType.objects.get_for_model(Order)
        self.issue.resource_object_id = self.order.id

        # Store original backend_id for comparison
        self.original_backend_id = self.order.backend_id

        # Default custom field value
        self.waldur_backend_id_value = "test-backend-id-123"

        # Mock the get method to return custom field data
        def get_side_effect(path, **kwargs):
            if "/rest/api/2/field" in path:
                return load_json_resource(
                    "jira_fields.json", "waldur_mastermind.support.tests"
                )
            elif "/rest/api/2/issue/" in path and "?fields=" not in path:
                # Return full issue data with custom fields
                return {
                    "fields": {
                        "summary": "Test Issue",
                        "description": "Test Description",
                        "customfield_10200": self.waldur_backend_id_value,
                        "status": {"name": "Open"},
                        "priority": {"name": "Medium"},
                    }
                }
            else:
                return {}

        self.mocked_service_desk.get.side_effect = get_side_effect

        # Mock field lookup
        def get_field_id_by_name_side_effect(field_name):
            if field_name == "waldur_backend_id":
                return "customfield_10200"
            elif field_name == "Impact":
                return "field104"
            else:
                from waldur_mastermind.support.backend.atlassian import JiraBackendError

                raise JiraBackendError(f"Field {field_name} not found")

        self.backend.get_field_id_by_name = mock.Mock(
            side_effect=get_field_id_by_name_side_effect
        )

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def test_resource_backend_id_updated_when_custom_field_has_value(self):
        """Test that resource backend_id is updated when waldur_backend_id custom field has a value."""
        self.waldur_backend_id_value = "new-backend-id-456"

        self.backend._update_resource_backend_id_from_custom_fields(self.issue)

        self.order.refresh_from_db()
        self.assertEqual(self.order.backend_id, "new-backend-id-456")

    def test_resource_backend_id_not_updated_when_values_match(self):
        """Test that resource backend_id is not updated when it already matches the custom field."""
        matching_id = "matching-backend-id"
        self.order.backend_id = matching_id
        self.order.save()
        self.waldur_backend_id_value = matching_id

        self.backend._update_resource_backend_id_from_custom_fields(self.issue)

        self.order.refresh_from_db()
        self.assertEqual(self.order.backend_id, matching_id)

    def test_resource_backend_id_not_updated_when_custom_field_empty(self):
        """Test that resource backend_id is not updated when custom field is empty."""
        self.waldur_backend_id_value = ""
        original_backend_id = self.order.backend_id

        self.backend._update_resource_backend_id_from_custom_fields(self.issue)

        self.order.refresh_from_db()
        self.assertEqual(self.order.backend_id, original_backend_id)

    def test_resource_backend_id_not_updated_when_custom_field_none(self):
        """Test that resource backend_id is not updated when custom field is None."""
        self.waldur_backend_id_value = None
        original_backend_id = self.order.backend_id

        self.backend._update_resource_backend_id_from_custom_fields(self.issue)

        self.order.refresh_from_db()
        self.assertEqual(self.order.backend_id, original_backend_id)

    def test_resource_backend_id_handles_whitespace_in_custom_field(self):
        """Test that whitespace in custom field value is stripped."""
        self.waldur_backend_id_value = "  backend-id-with-spaces  "

        self.backend._update_resource_backend_id_from_custom_fields(self.issue)

        self.order.refresh_from_db()
        self.assertEqual(self.order.backend_id, "backend-id-with-spaces")

    def test_skips_update_when_issue_not_connected_to_resource(self):
        """Test that update is skipped when issue is not connected to a resource."""
        self.issue.resource_content_type = None
        self.issue.resource_object_id = None
        original_backend_id = self.order.backend_id

        # Should not raise an exception
        self.backend._update_resource_backend_id_from_custom_fields(self.issue)

        # Order should not be affected
        self.order.refresh_from_db()
        self.assertEqual(self.order.backend_id, original_backend_id)

    def test_handles_exception_when_getting_jira_issue(self):
        """Test that exceptions are handled gracefully when getting Jira issue data."""
        self.mocked_service_desk.get.side_effect = Exception("API error")
        original_backend_id = self.order.backend_id

        # Should not raise an exception, should log warning
        with mock.patch(
            "waldur_mastermind.support.backend.atlassian.logger"
        ) as mock_logger:
            self.backend._update_resource_backend_id_from_custom_fields(self.issue)
            mock_logger.warning.assert_called_once()

        # Order should not be changed
        self.order.refresh_from_db()
        self.assertEqual(self.order.backend_id, original_backend_id)

    def test_fallback_to_known_field_id_when_field_lookup_fails(self):
        """Test fallback to known field ID when get_field_id_by_name fails."""
        from waldur_mastermind.support.backend.atlassian import JiraBackendError

        # Make field lookup fail
        self.backend.get_field_id_by_name.side_effect = JiraBackendError(
            "Field not found"
        )

        self.backend._update_resource_backend_id_from_custom_fields(self.issue)

        # Should still work using the fallback field ID
        self.order.refresh_from_db()
        self.assertEqual(self.order.backend_id, self.waldur_backend_id_value)


@override_config(ATLASSIAN_CUSTOM_ISSUE_FIELD_MAPPING_ENABLED=False)
class ResourceBackendIdUpdateDisabledTest(TestCase):
    """Test that resource backend_id update is skipped when custom field mapping is disabled."""

    def setUp(self):
        super().setUp()

        self.fixture = fixtures.SupportFixture()
        self.backend = ServiceDeskBackend()

        # Create a real order for testing
        self.order = marketplace_factories.OrderFactory()

        # Set up issue connected to the order
        self.issue = self.fixture.issue
        self.issue.key = "TEST-123"
        self.issue.resource_content_type = ContentType.objects.get_for_model(Order)
        self.issue.resource_object_id = self.order.id

    def test_skips_update_when_custom_field_mapping_disabled(self):
        """Test that update is skipped when custom field mapping is disabled."""
        original_backend_id = self.order.backend_id

        self.backend._update_resource_backend_id_from_custom_fields(self.issue)

        # Should not change the backend_id
        self.order.refresh_from_db()
        self.assertEqual(self.order.backend_id, original_backend_id)


@override_config(ATLASSIAN_CUSTOM_ISSUE_FIELD_MAPPING_ENABLED=True)
class ResourceBackendIdIntegrationTest(TestCase):
    """Test integration of resource backend_id update with issue synchronization."""

    def setUp(self):
        super().setUp()

        self.fixture = fixtures.SupportFixture()
        self.backend = ServiceDeskBackend()

        # Mock ServiceDesk client
        service_desk_patcher = mock.patch(
            "waldur_mastermind.support.backend.atlassian.ServiceDesk"
        )
        mocked_service_desk_class = service_desk_patcher.start()
        self.mocked_service_desk = mocked_service_desk_class.return_value
        self.backend.manager = self.mocked_service_desk

        # Create real marketplace order for integration test
        self.order = marketplace_factories.OrderFactory()

        # Create issue connected to the order
        self.issue = self.fixture.issue
        self.issue.key = "TEST-123"
        self.issue.backend_id = "TEST-123"
        self.issue.resource_content_type = ContentType.objects.get_for_model(Order)
        self.issue.resource_object_id = self.order.id
        self.issue.save()

        # Mock backend issue data
        self.backend_issue = {
            "issueKey": "TEST-123",
            "issueId": "12345",
            "requestFieldValues": [
                {"fieldId": "summary", "value": "Test Issue"},
                {"fieldId": "description", "value": "Test Description"},
            ],
            "currentStatus": {"status": "Open"},
            "_links": {"agent": "http://example.com/TEST-123"},
            "summary": "Test Issue",
            "assignee": {},
            "reporter": {},
        }
        self.mocked_service_desk.get_customer_request.return_value = self.backend_issue

        # Mock the get method to return custom field data
        def get_side_effect(path, **kwargs):
            if "/rest/api/2/field" in path:
                return load_json_resource(
                    "jira_fields.json", "waldur_mastermind.support.tests"
                )
            elif "/rest/api/2/issue/" in path and "?fields=" not in path:
                return {
                    "fields": {
                        "summary": "Test Issue",
                        "customfield_10200": "integration-test-backend-id",
                        "status": {"name": "Open"},
                    }
                }
            elif "/rest/api/3/issue/" in path and "fields=resolution" in path:
                return {"fields": {"resolution": None}}
            else:
                return {}

        self.mocked_service_desk.get.side_effect = get_side_effect

        # Mock field lookup
        self.backend.get_field_id_by_name = mock.Mock(return_value="customfield_10200")

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def test_resource_backend_id_updated_during_issue_sync(self):
        """Test that resource backend_id is updated during issue synchronization."""
        # Store original backend_id
        original_backend_id = self.order.backend_id

        # Call _backend_issue_to_issue which should trigger the update
        self.backend._backend_issue_to_issue(self.backend_issue, self.issue)

        # Check that the order's backend_id was updated
        self.order.refresh_from_db()
        self.assertEqual(self.order.backend_id, "integration-test-backend-id")
        self.assertNotEqual(self.order.backend_id, original_backend_id)

    def test_update_resource_backend_id_method_called_during_sync(self):
        """Test that _update_resource_backend_id_from_custom_fields is called during sync."""
        # Mock the resource update method to verify it's called
        with mock.patch.object(
            self.backend, "_update_resource_backend_id_from_custom_fields"
        ) as mock_update:
            # Call _backend_issue_to_issue
            self.backend._backend_issue_to_issue(self.backend_issue, self.issue)

            # Verify that our resource update method was called
            mock_update.assert_called_once_with(self.issue)
