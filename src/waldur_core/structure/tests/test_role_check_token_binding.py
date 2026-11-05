"""Role checks hold a personal access token to its bindings, as permission
checks do: its user's role passes only on what the token is bound to."""

from types import SimpleNamespace

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework import exceptions

from waldur_core.core.tests.helpers import create_pat
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure import permissions
from waldur_core.structure.tests import factories


class RoleCheckTokenBindingTest(TestCase):
    def setUp(self):
        self.owner = factories.UserFactory()
        self.customer = factories.CustomerFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)
        self.project_a = factories.ProjectFactory(customer=self.customer)
        self.project_b = factories.ProjectFactory(customer=self.customer)

    def _request(self, *bound_to, user=None, scopes=None):
        pat, _ = create_pat(
            user or self.owner,
            scopes=scopes or [PermissionEnum.LIST_PROJECTS.value],
            allowed_scopes=[
                {
                    "content_type_id": ContentType.objects.get_for_model(scope).id,
                    "object_id": scope.id,
                }
                for scope in bound_to
            ],
        )
        return SimpleNamespace(user=pat.user, auth=pat)

    def _passes(self, check, request, obj):
        try:
            check(request, None, obj=obj)
        except exceptions.PermissionDenied:
            return False
        return True

    def test_project_binding_passes_on_its_project_only(self):
        request = self._request(self.project_a)

        self.assertTrue(self._passes(permissions.is_owner, request, self.project_a))
        self.assertFalse(self._passes(permissions.is_owner, request, self.project_b))

    def test_project_binding_does_not_reach_its_organization(self):
        request = self._request(self.project_a)

        self.assertFalse(self._passes(permissions.is_owner, request, self.customer))

    def test_organization_binding_reaches_every_project(self):
        request = self._request(self.customer)

        self.assertTrue(self._passes(permissions.is_owner, request, self.customer))
        self.assertTrue(self._passes(permissions.is_owner, request, self.project_b))

    def test_unbound_token_passes_wherever_its_user_does(self):
        request = self._request()

        self.assertTrue(self._passes(permissions.is_owner, request, self.project_b))

    def test_binding_does_not_grant_a_role_the_user_lacks(self):
        stranger = factories.UserFactory()
        request = self._request(self.customer, user=stranger)

        self.assertFalse(self._passes(permissions.is_owner, request, self.project_a))

    def test_project_role_check_tests_the_project(self):
        admin = factories.UserFactory()
        self.project_a.add_user(admin, ProjectRole.ADMIN)
        self.project_b.add_user(admin, ProjectRole.ADMIN)
        request = self._request(self.project_a, user=admin)

        self.assertTrue(
            self._passes(permissions.is_administrator, request, self.project_a)
        )
        self.assertFalse(
            self._passes(permissions.is_administrator, request, self.project_b)
        )
