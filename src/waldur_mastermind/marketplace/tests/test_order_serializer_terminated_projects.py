from rest_framework import serializers
from rest_framework.test import APITestCase

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.serializers import OrderCreateSerializer
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class OrderSerializerTerminatedProjectTest(APITestCase):
    """Test OrderCreateSerializer handling of terminated projects"""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.offering = marketplace_factories.OfferingFactory()

    def test_validate_project_rejects_terminated_project(self):
        """Test that validate_project method rejects terminated projects"""
        # Soft delete the project
        self.project.delete()

        # Create serializer instance
        serializer = OrderCreateSerializer()

        # Test that validate_project raises ValidationError for terminated project
        with self.assertRaises(serializers.ValidationError) as cm:
            serializer.validate_project(self.project)

        error_message = str(cm.exception.detail[0]).lower()
        self.assertIn("cannot create orders", error_message)
        self.assertIn("terminated projects", error_message)

    def test_validate_project_accepts_active_project(self):
        """Test that validate_project accepts active projects"""
        # Create serializer instance
        serializer = OrderCreateSerializer()

        # Test that validate_project returns the project for active projects
        result = serializer.validate_project(self.project)
        self.assertEqual(result, self.project)

    def test_serializer_context_handling(self):
        """Test that serializer properly handles context"""
        # Test that serializer can be instantiated with context
        context = {
            "request": None,
            "view": None,
        }
        serializer = OrderCreateSerializer(context=context)
        self.assertEqual(serializer.context, context)

    def test_serializer_field_configuration(self):
        """Test that project field is properly configured"""
        # Create a mock request for the context
        from unittest.mock import Mock

        mock_request = Mock()
        mock_request.method = "POST"
        mock_view = Mock()
        mock_view.request = mock_request

        context = {
            "request": mock_request,
            "view": mock_view,
        }
        serializer = OrderCreateSerializer(context=context)

        # Verify the project field exists and has correct configuration
        self.assertIn("project", serializer.fields)
        project_field = serializer.fields["project"]

        # Should be configured to use project-detail view and uuid lookup
        self.assertEqual(project_field.view_name, "project-detail")
        self.assertEqual(project_field.lookup_field, "uuid")
