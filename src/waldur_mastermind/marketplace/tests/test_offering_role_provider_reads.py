"""Provider-side reads follow what an offering role grants, not its presence.

A deployment can define offering-scoped roles that carry no permissions, to
name people for work done elsewhere (technical reviewers of proposals, say).
Holding one opens nothing on the provider side: not the offering, even an
unshared one, its plans nor its resources. The offering manager role, and any
role that carries a permission, keep their reach.
"""

from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_core.permissions.models import Role
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories


class OfferingRoleProviderReadsTest(test.APITestCase):
    def setUp(self):
        self.offering = factories.OfferingFactory(shared=False)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.resource = factories.ResourceFactory(
            offering=self.offering, plan=self.plan
        )
        offering_ct = ContentType.objects.get_for_model(models.Offering)
        self.empty_role = Role.objects.create(
            name="Technical reviewer", content_type=offering_ct
        )
        self.narrow_role = Role.objects.create(
            name="Offering editor", content_type=offering_ct
        )
        self.narrow_role.add_permission(PermissionEnum.UPDATE_OFFERING)

    def holder(self, role):
        user = structure_factories.UserFactory()
        self.offering.add_user(user, role)
        return user

    def sees(self, user):
        self.client.force_authenticate(user)
        offerings = self.client.get(factories.OfferingFactory.get_list_url())
        offering = self.client.get(factories.OfferingFactory.get_url(self.offering))
        # The catalogue shows an unshared offering, with its plans, only to
        # those connected to it.
        public = self.client.get(
            factories.OfferingFactory.get_public_url(self.offering)
        )
        resources = self.client.get(
            factories.ResourceFactory.get_provider_resource_list_url()
        )
        resource = self.client.get(
            factories.ResourceFactory.get_provider_resource_url(self.resource)
        )
        return {
            "offering list": self.offering.uuid.hex
            in {row["uuid"] for row in offerings.data},
            "offering detail": offering.status_code == status.HTTP_200_OK,
            "plans": public.status_code == status.HTTP_200_OK
            and self.plan.uuid.hex in {row["uuid"] for row in public.data["plans"]},
            "resource list": self.resource.uuid.hex
            in {row["uuid"] for row in resources.data},
            "resource detail": resource.status_code == status.HTTP_200_OK,
        }

    def test_a_role_without_permissions_opens_nothing(self):
        seen = self.sees(self.holder(self.empty_role))
        self.assertEqual(seen, dict.fromkeys(seen, False))

    def test_the_offering_manager_keeps_its_reach(self):
        seen = self.sees(self.holder(OfferingRole.MANAGER))
        self.assertEqual(seen, dict.fromkeys(seen, True))

    def test_a_custom_role_with_a_permission_keeps_its_reach(self):
        seen = self.sees(self.holder(self.narrow_role))
        self.assertEqual(seen, dict.fromkeys(seen, True))

    def test_the_provider_owner_keeps_its_reach(self):
        owner = structure_factories.UserFactory()
        self.offering.customer.add_user(owner, CustomerRole.OWNER)
        seen = self.sees(owner)
        self.assertEqual(seen, dict.fromkeys(seen, True))

    def test_the_team_of_the_offering_stays_hidden(self):
        user = self.holder(self.empty_role)
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.OfferingFactory.get_url(self.offering, action="list_users")
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
