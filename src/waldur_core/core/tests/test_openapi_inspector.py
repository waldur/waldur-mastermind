import unittest
from unittest.mock import MagicMock, patch

from drf_spectacular.openapi import AutoSchema
from rest_framework import viewsets

from waldur_core.core.openapi_inspector import WaldurOpenApiInspector
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
