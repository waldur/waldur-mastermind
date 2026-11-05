"""A service provider's project group is the provider's, though it names a
consumer project: the owner role check on it reaches a token bound to the
provider, and not one bound to the consumer project."""

from types import SimpleNamespace

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework import exceptions

from waldur_core.core.tests.helpers import create_pat
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure import permissions
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories


class ProjectGroupTokenBindingTest(TestCase):
    def setUp(self):
        # One user owns the provider and administers a consumer project in
        # another organization.
        self.user = structure_factories.UserFactory()
        self.provider_customer = structure_factories.CustomerFactory()
        self.provider_customer.add_user(self.user, CustomerRole.OWNER)
        self.service_provider = factories.ServiceProviderFactory(
            customer=self.provider_customer
        )
        self.consumer_project = structure_factories.ProjectFactory()
        self.consumer_project.add_user(self.user, ProjectRole.ADMIN)
        self.group = models.ServiceProviderProjectGroup.objects.create(
            service_provider=self.service_provider,
            project=self.consumer_project,
            name="consumer",
        )

    def _passes(self, *bound_to):
        pat, _ = create_pat(
            self.user,
            scopes=[PermissionEnum.LIST_PROJECTS.value],
            allowed_scopes=[
                {
                    "content_type_id": ContentType.objects.get_for_model(scope).id,
                    "object_id": scope.id,
                }
                for scope in bound_to
            ],
        )
        try:
            permissions.is_owner(
                SimpleNamespace(user=pat.user, auth=pat), None, obj=self.group
            )
        except exceptions.PermissionDenied:
            return False
        return True

    def test_service_provider_binding_passes(self):
        self.assertTrue(self._passes(self.service_provider))

    def test_provider_organization_binding_passes(self):
        self.assertTrue(self._passes(self.provider_customer))

    def test_consumer_project_binding_does_not_pass(self):
        self.assertFalse(self._passes(self.consumer_project))
