from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from waldur_core.core.permissions import ActionsPermission
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import permission_factory


class ActionsPermissionTest(SimpleTestCase):
    def test_collection_action_does_not_resolve_detail_object(self):
        request = SimpleNamespace(method="GET")
        get_object = mock.Mock(side_effect=AssertionError("detail lookup attempted"))
        view = SimpleNamespace(
            action="list",
            detail=False,
            list_permissions=[
                permission_factory(
                    PermissionEnum.MANAGE_OFFERING_BACKEND_RESOURCES,
                    ["offering"],
                )
            ],
            get_object=get_object,
        )

        self.assertTrue(ActionsPermission().has_permission(request, view))
        get_object.assert_not_called()

    def test_detail_action_resolves_object_for_scoped_permission(self):
        request = SimpleNamespace(method="GET")
        scope = object()
        check = mock.Mock()
        check.sources = ["offering"]
        get_object = mock.Mock(return_value=scope)
        view = SimpleNamespace(
            action="retrieve",
            detail=True,
            retrieve_permissions=[check],
            get_object=get_object,
        )

        self.assertTrue(ActionsPermission().has_permission(request, view))
        get_object.assert_called_once_with()
        check.assert_called_once_with(request, view, scope)
