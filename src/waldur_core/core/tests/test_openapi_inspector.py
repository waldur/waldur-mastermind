import unittest
from unittest.mock import MagicMock, Mock, patch

import pytest
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.utils import OpenApiParameter
from rest_framework import serializers, viewsets
from rest_framework.serializers import ListSerializer

from waldur_core.core import signals as core_signals
from waldur_core.core.openapi_inspector import WaldurOpenApiInspector
from waldur_core.core.serializers import RestrictedSerializerMixin
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import permission_factory


class WaldurOpenApiInspectorTest(unittest.TestCase):
    def setUp(self):
        self.view = MagicMock(spec=viewsets.ViewSet)

    def test_get_operation_with_permissions(self):
        """
        Verify that `x-permissions` field is added when a view action has a `_permissions` list.
        """
        # Arrange
        self.view.action = "create"
        self.view.create_permissions = [
            permission_factory(PermissionEnum.APPROVE_ORDER, ["*", "project"])
        ]
        inspector = WaldurOpenApiInspector()
        inspector.view = self.view

        # Act
        with patch.object(AutoSchema, "get_operation", return_value={}):
            operation = inspector.get_operation(
                "/api/dummies/", r"^/api/dummies/$", "/api/", "POST", MagicMock()
            )

        # Assert
        self.assertIn("x-permissions", operation)
        self.assertEqual(
            operation["x-permissions"],
            [{"permission": "ORDER.APPROVE", "scopes": ["*", "project"]}],
        )

    def test_get_operation_without_permissions(self):
        """
        Verify that `x-permissions` is not added if the action has no `_permissions` defined.
        """
        # Arrange
        self.view.action = "create"
        inspector = WaldurOpenApiInspector()
        inspector.view = self.view

        # Act
        with patch.object(AutoSchema, "get_operation", return_value={}):
            operation = inspector.get_operation(
                "/api/dummies/", r"^/api/dummies/$", "/api/", "POST", MagicMock()
            )

        # Assert
        self.assertNotIn("x-permissions", operation)


class _TestSerializer(RestrictedSerializerMixin, serializers.ModelSerializer):
    """Test serializer for OpenAPI inspector tests."""

    class Meta:
        model = Mock()
        fields = ("id", "name", "url")


class _TestSerializerWithSignalFields(
    RestrictedSerializerMixin, serializers.ModelSerializer
):
    """Test serializer that will have signal-based fields added."""

    class Meta:
        model = Mock()
        fields = ("id", "name", "url")


def add_test_signal_fields(sender, fields, **kwargs):
    """Signal handler that adds test fields."""
    fields["signal_field_1"] = serializers.SerializerMethodField()
    fields["signal_field_2"] = serializers.ReadOnlyField()


class TestWaldurOpenApiInspectorSignalFields(unittest.TestCase):
    """Test cases for WaldurOpenApiInspector signal-based field inclusion."""

    def setUp(self):
        """Set up test fixtures."""
        self.inspector = WaldurOpenApiInspector()
        self.inspector.method = "GET"
        self.inspector.path = "/api/test/"

    def test_get_override_parameters_basic_serializer(self):
        """Test that basic serializer without signals works correctly."""
        # Mock the get_response_serializers method
        serializer = _TestSerializer()
        self.inspector.get_response_serializers = Mock(return_value=serializer)

        result = self.inspector.get_override_parameters()

        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], OpenApiParameter)
        self.assertEqual(result[0].name, RestrictedSerializerMixin.FIELDS_PARAM_NAME)
        self.assertEqual(set(result[0].enum), {"id", "name", "url"})

    def test_get_override_parameters_with_signal_fields(self):
        """Test that signal-based fields are included in field options."""
        # Connect our test signal handler
        core_signals.pre_serializer_fields.connect(
            sender=_TestSerializerWithSignalFields,
            receiver=add_test_signal_fields,
        )

        try:
            # Mock the get_response_serializers method
            serializer = _TestSerializerWithSignalFields()
            self.inspector.get_response_serializers = Mock(return_value=serializer)

            result = self.inspector.get_override_parameters()

            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], OpenApiParameter)
            self.assertEqual(
                result[0].name, RestrictedSerializerMixin.FIELDS_PARAM_NAME
            )

            # Should include both Meta fields and signal-added fields
            expected_fields = {"id", "name", "url", "signal_field_1", "signal_field_2"}
            self.assertEqual(set(result[0].enum), expected_fields)

        finally:
            # Clean up signal connection
            core_signals.pre_serializer_fields.disconnect(
                sender=_TestSerializerWithSignalFields,
                receiver=add_test_signal_fields,
            )

    def test_get_override_parameters_with_list_serializer(self):
        """Test that signal fields work with ListSerializer wrapper."""
        # Connect our test signal handler
        core_signals.pre_serializer_fields.connect(
            sender=_TestSerializerWithSignalFields,
            receiver=add_test_signal_fields,
        )

        try:
            # Create a ListSerializer wrapper
            child_serializer = _TestSerializerWithSignalFields()
            list_serializer = ListSerializer(child=child_serializer)

            self.inspector.get_response_serializers = Mock(return_value=list_serializer)

            result = self.inspector.get_override_parameters()

            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], OpenApiParameter)

            # Should include both Meta fields and signal-added fields
            expected_fields = {"id", "name", "url", "signal_field_1", "signal_field_2"}
            self.assertEqual(set(result[0].enum), expected_fields)

        finally:
            # Clean up signal connection
            core_signals.pre_serializer_fields.disconnect(
                sender=_TestSerializerWithSignalFields,
                receiver=add_test_signal_fields,
            )

    def test_get_override_parameters_non_get_method(self):
        """Test that non-GET methods return empty parameters."""
        self.inspector.method = "POST"
        result = self.inspector.get_override_parameters()
        self.assertEqual(result, [])

    def test_get_override_parameters_non_restricted_serializer(self):
        """Test that non-RestrictedSerializerMixin serializers return empty parameters."""

        # Create a regular DRF serializer
        class RegularSerializer(serializers.ModelSerializer):
            class Meta:
                model = Mock()
                fields = ("id", "name")

        serializer = RegularSerializer()
        self.inspector.get_response_serializers = Mock(return_value=serializer)

        result = self.inspector.get_override_parameters()
        self.assertEqual(result, [])

    def test_get_override_parameters_fallback_to_meta_fields(self):
        """Test fallback to Meta fields when serializer instantiation fails."""

        # Mock a serializer class that raises an exception during instantiation
        class ProblematicSerializer(
            RestrictedSerializerMixin, serializers.ModelSerializer
        ):
            def __init__(self, *args, **kwargs):
                raise ValueError("Test exception")

            class Meta:
                model = Mock()
                fields = ("id", "name", "url")

        # Connect signal handler for this serializer
        core_signals.pre_serializer_fields.connect(
            sender=ProblematicSerializer,
            receiver=add_test_signal_fields,
        )

        try:
            # Mock get_response_serializers to return an instance that will trigger fallback
            mock_instance = Mock()
            mock_instance.__class__ = ProblematicSerializer
            self.inspector.get_response_serializers = Mock(return_value=mock_instance)

            result = self.inspector.get_override_parameters()

            self.assertEqual(len(result), 1)
            # Should include Meta fields + signal fields even in fallback mode
            expected_fields = {"id", "name", "url", "signal_field_1", "signal_field_2"}
            self.assertEqual(set(result[0].enum), expected_fields)

        finally:
            # Clean up signal connection
            core_signals.pre_serializer_fields.disconnect(
                sender=ProblematicSerializer,
                receiver=add_test_signal_fields,
            )

    def test_get_override_parameters_with_all_fields_meta(self):
        """Test behavior with Meta.fields = '__all__'."""

        class AllFieldsSerializer(
            RestrictedSerializerMixin, serializers.ModelSerializer
        ):
            def __init__(self, *args, **kwargs):
                raise ValueError("Test exception to trigger fallback")

            class Meta:
                model = Mock()
                fields = "__all__"

        mock_instance = Mock()
        mock_instance.__class__ = AllFieldsSerializer
        self.inspector.get_response_serializers = Mock(return_value=mock_instance)

        result = self.inspector.get_override_parameters()
        # Should return empty list for __all__ fields
        self.assertEqual(result, [])

    def test_get_override_parameters_single_field(self):
        """Test that single field serializers return empty parameters."""

        class SingleFieldSerializer(
            RestrictedSerializerMixin, serializers.ModelSerializer
        ):
            class Meta:
                model = Mock()
                fields = ("id",)

        serializer = SingleFieldSerializer()
        self.inspector.get_response_serializers = Mock(return_value=serializer)

        result = self.inspector.get_override_parameters()
        # Should return empty list for single field
        self.assertEqual(result, [])

    def test_get_override_parameters_no_fields(self):
        """Test that serializers with no fields return empty parameters."""

        class NoFieldsSerializer(
            RestrictedSerializerMixin, serializers.ModelSerializer
        ):
            class Meta:
                model = Mock()
                fields = ()

        serializer = NoFieldsSerializer()
        self.inspector.get_response_serializers = Mock(return_value=serializer)

        result = self.inspector.get_override_parameters()
        # Should return empty list for no fields
        self.assertEqual(result, [])


@pytest.mark.django_db
class TestRealWorldSerializers(unittest.TestCase):
    """Integration tests with real waldur serializers."""

    def setUp(self):
        """Set up test fixtures."""
        self.inspector = WaldurOpenApiInspector()
        self.inspector.method = "GET"
        self.inspector.path = "/api/test/"

    def test_project_serializer_includes_signal_fields(self):
        """Test that ProjectSerializer includes billing and invoice signal fields."""
        from waldur_core.structure.serializers import ProjectSerializer

        # Mock the get_response_serializers method
        serializer = ProjectSerializer()
        self.inspector.get_response_serializers = Mock(return_value=serializer)

        result = self.inspector.get_override_parameters()

        self.assertEqual(len(result), 1)
        field_enum = set(result[0].enum)

        # Check that signal-based fields are included
        self.assertIn("billing_price_estimate", field_enum)
        self.assertIn("project_credit", field_enum)

        # Check that regular Meta fields are also included
        self.assertIn("name", field_enum)
        self.assertIn("uuid", field_enum)
        self.assertIn("url", field_enum)

    def test_customer_serializer_includes_billing_fields(self):
        """Test that CustomerSerializer includes billing signal fields."""
        from waldur_core.structure.serializers import CustomerSerializer

        # Mock the get_response_serializers method
        serializer = CustomerSerializer()
        self.inspector.get_response_serializers = Mock(return_value=serializer)

        result = self.inspector.get_override_parameters()

        self.assertEqual(len(result), 1)
        field_enum = set(result[0].enum)

        # Check that billing signal-based fields are included
        self.assertIn("billing_price_estimate", field_enum)

        # Check that regular Meta fields are also included
        self.assertIn("name", field_enum)
        self.assertIn("uuid", field_enum)
        self.assertIn("url", field_enum)
