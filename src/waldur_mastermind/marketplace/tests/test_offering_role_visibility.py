"""Who sees an offering's custom roles at /api/marketplace-offering-roles/.

The provider manages them on the offering, and the consumers of its resources
pick one when they add or invite a user to a resource. Nobody else sees them.
"""

from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.permissions.models import Role, RoleAvailability
from waldur_core.permissions.utils import add_user
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories

LIST_URL = reverse("marketplace-offering-role-list")


def detail_url(role):
    return reverse("marketplace-offering-role-detail", kwargs={"uuid": role.uuid.hex})


def offering_role(offering, name, model=models.Resource):
    role = Role.objects.create(
        name=name,
        content_type=ContentType.objects.get_for_model(model),
        is_system_role=False,
    )
    RoleAvailability.objects.create(
        role=role,
        content_type=ContentType.objects.get_for_model(models.Offering),
        object_id=offering.id,
    )
    return role


class OfferingRoleVisibilityTest(test.APITestCase):
    def setUp(self):
        self.provider = factories.ServiceProviderFactory()
        self.offering = factories.OfferingFactory(
            customer=self.provider.customer, shared=True, name="Cluster"
        )
        self.role = offering_role(self.offering, "Cluster admin")

        self.private_offering = factories.OfferingFactory(
            shared=False, name="Private offering"
        )
        self.private_role = offering_role(self.private_offering, "Private admin")

        self.resource = factories.ResourceFactory(offering=self.offering)

    def names(self, user, **params):
        self.client.force_authenticate(user)
        response = self.client.get(LIST_URL, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return sorted(row["name"] for row in response.data)

    def test_user_without_roles_sees_no_offering_roles(self):
        user = structure_factories.UserFactory()
        self.assertEqual(self.names(user), [])

    def test_user_without_roles_cannot_retrieve_a_private_offerings_role(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.get(detail_url(self.private_role))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_member_of_another_organization_sees_no_offering_roles(self):
        user = structure_factories.UserFactory()
        structure_factories.CustomerFactory().add_user(user, CustomerRole.OWNER)
        self.assertEqual(
            self.names(user, offering_uuid=self.private_offering.uuid.hex), []
        )

    def test_provider_organization_owner_sees_its_offerings_roles(self):
        owner = structure_factories.UserFactory()
        self.provider.customer.add_user(owner, CustomerRole.OWNER)
        self.assertEqual(
            self.names(owner, offering_uuid=self.offering.uuid.hex), ["Cluster admin"]
        )

    def test_offering_manager_sees_its_offerings_roles(self):
        manager = structure_factories.UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)
        self.assertEqual(self.names(manager), ["Cluster admin"])

    def test_service_provider_manager_sees_its_offerings_roles(self):
        manager = structure_factories.UserFactory()
        self.provider.add_user(manager, ServiceProviderRole.MANAGER)
        self.assertEqual(self.names(manager), ["Cluster admin"])

    def test_project_member_with_a_resource_sees_the_offerings_roles(self):
        member = structure_factories.UserFactory()
        self.resource.project.add_user(member, ProjectRole.MEMBER)
        self.assertEqual(
            self.names(member, offering_uuid=self.offering.uuid.hex), ["Cluster admin"]
        )

    def test_the_named_offering_is_one_the_caller_sees(self):
        # A profile role available on a private offering of another
        # organization (created first, so first by id) and on the caller's.
        hidden = factories.OfferingFactory(shared=False, name="Hidden offering")
        role = offering_role(hidden, "Profile admin")
        RoleAvailability.objects.create(
            role=role,
            content_type=ContentType.objects.get_for_model(models.Offering),
            object_id=self.offering.id,
        )
        member = structure_factories.UserFactory()
        self.resource.project.add_user(member, ProjectRole.MEMBER)

        self.client.force_authenticate(member)
        rows = self.client.get(LIST_URL).data
        names = {row["name"]: row["offering_name"] for row in rows}
        self.assertEqual(names["Profile admin"], "Cluster")
        rows = self.client.get(LIST_URL, {"offering_uuid": hidden.uuid.hex}).data
        self.assertNotIn("Hidden offering", [row["offering_name"] for row in rows])

    def test_consumer_organization_owner_sees_the_offerings_roles(self):
        owner = structure_factories.UserFactory()
        self.resource.project.customer.add_user(owner, CustomerRole.OWNER)
        self.assertEqual(self.names(owner), ["Cluster admin"])

    def test_resource_role_holder_sees_the_offerings_roles(self):
        user = structure_factories.UserFactory()
        add_user(self.resource, user, self.role)
        self.assertEqual(self.names(user), ["Cluster admin"])

    def test_resource_project_role_holder_sees_the_offerings_roles(self):
        rp_role = offering_role(
            self.offering, "Namespace admin", model=models.ResourceProject
        )
        resource_project = models.ResourceProject.objects.create(
            resource=self.resource, name="Namespace"
        )
        user = structure_factories.UserFactory()
        add_user(resource_project, user, rp_role)
        self.assertEqual(self.names(user), ["Cluster admin", "Namespace admin"])

    def test_project_member_of_a_private_offering_sees_its_roles(self):
        member = structure_factories.UserFactory()
        project = structure_factories.ProjectFactory()
        self.private_offering.project = project
        self.private_offering.save()
        project.add_user(member, ProjectRole.MEMBER)
        self.assertEqual(self.names(member), ["Private admin"])

    def test_consumer_sees_the_roles_of_the_offerings_service_profile(self):
        profile = models.OfferingProfile.objects.create(name="rancher")
        profile_role = Role.objects.create(
            name="Profile admin",
            content_type=ContentType.objects.get_for_model(models.Resource),
            is_system_role=False,
        )
        profile.roles.add(profile_role)
        # Available on another offering only: the profile, not an
        # availability row, makes it a role of this offering.
        RoleAvailability.objects.create(
            role=profile_role,
            content_type=ContentType.objects.get_for_model(models.Offering),
            object_id=self.private_offering.id,
        )
        models.Offering.objects.filter(id=self.offering.id).update(profile=profile)
        member = structure_factories.UserFactory()
        self.resource.project.add_user(member, ProjectRole.MEMBER)
        self.assertEqual(
            self.names(member, offering_uuid=self.offering.uuid.hex),
            ["Profile admin"],
        )

    def test_staff_and_support_see_every_offerings_roles(self):
        for user in (
            structure_factories.UserFactory(is_staff=True),
            structure_factories.UserFactory(is_support=True),
        ):
            with self.subTest(staff=user.is_staff):
                self.assertEqual(self.names(user), ["Cluster admin", "Private admin"])

    def test_provider_owner_still_edits_its_offerings_role(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        owner = structure_factories.UserFactory()
        self.provider.customer.add_user(owner, CustomerRole.OWNER)
        self.client.force_authenticate(owner)
        response = self.client.patch(
            detail_url(self.role), {"description": "Admin of the cluster"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
