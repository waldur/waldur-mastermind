from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.test import override_settings
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import (
    Role,
    RoleAvailability,
    UserRole,
)
from waldur_core.permissions.utils import has_permission
from waldur_core.structure.models import Customer, Project
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class RoleNameUniquenessTest(test.APITestCase):
    def test_two_roles_with_same_name_different_content_type_are_allowed(self):
        customer_ct = ContentType.objects.get_for_model(Customer)
        project_ct = ContentType.objects.get_for_model(Project)

        role1 = Role.objects.create(
            name="Admin", content_type=customer_ct, is_system_role=False
        )
        role2 = Role.objects.create(
            name="Admin", content_type=project_ct, is_system_role=False
        )

        self.assertNotEqual(role1.pk, role2.pk)
        self.assertEqual(role1.name, role2.name)

    def test_resource_roles_with_same_name_are_allowed(self):
        """Different offerings can define roles with the same name for resources."""
        resource_ct = ContentType.objects.get_for_model(marketplace_models.Resource)

        role1 = Role.objects.create(
            name="Admin", content_type=resource_ct, is_system_role=False
        )
        role2 = Role.objects.create(
            name="Admin", content_type=resource_ct, is_system_role=False
        )
        self.assertNotEqual(role1.pk, role2.pk)

    def test_resource_project_roles_with_same_name_are_allowed(self):
        """Different offerings can define roles with the same name for resource projects."""
        rp_ct = ContentType.objects.get_for_model(marketplace_models.ResourceProject)

        role1 = Role.objects.create(
            name="Member", content_type=rp_ct, is_system_role=False
        )
        role2 = Role.objects.create(
            name="Member", content_type=rp_ct, is_system_role=False
        )
        self.assertNotEqual(role1.pk, role2.pk)

    def test_non_resource_roles_with_same_name_are_rejected(self):
        """Customer/Project/Offering scoped roles must have unique names."""
        customer_ct = ContentType.objects.get_for_model(Customer)

        Role.objects.create(
            name="CustomRole", content_type=customer_ct, is_system_role=False
        )
        with self.assertRaises(DjangoValidationError):
            Role.objects.create(
                name="CustomRole", content_type=customer_ct, is_system_role=False
            )

    def test_system_roles_still_work(self):
        project_ct = ContentType.objects.get_for_model(Project)
        role = Role.objects.get_system_role("PROJECT.ADMIN", project_ct)
        self.assertTrue(role.is_system_role)
        self.assertEqual(role.name, "PROJECT.ADMIN")

    def test_dynamic_role_creation_for_resource_scope(self):
        resource_ct = ContentType.objects.get_for_model(marketplace_models.Resource)
        role = Role.objects.create(
            name="Cluster Admin",
            content_type=resource_ct,
            is_system_role=False,
        )
        self.assertEqual(role.content_type, resource_ct)


class RoleAvailabilityScopingTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.resource_ct = ContentType.objects.get_for_model(
            marketplace_models.Resource
        )
        self.offering_a = self.fixture.offering
        self.offering_b = marketplace_factories.OfferingFactory(
            customer=structure_factories.CustomerFactory()
        )
        self.role = Role.objects.create(
            name="Test Resource Role",
            content_type=self.resource_ct,
            is_system_role=False,
        )

    def test_role_with_no_availability_is_available_everywhere(self):
        self.assertFalse(self.role.availability.exists())
        # No restrictions — role can be used anywhere

    def test_role_with_availability_for_one_offering(self):
        offering_ct = ContentType.objects.get_for_model(marketplace_models.Offering)
        RoleAvailability.objects.create(
            role=self.role,
            content_type=offering_ct,
            object_id=self.offering_a.id,
        )

        self.assertTrue(
            self.role.availability.filter(
                content_type=offering_ct, object_id=self.offering_a.id
            ).exists()
        )
        self.assertFalse(
            self.role.availability.filter(
                content_type=offering_ct, object_id=self.offering_b.id
            ).exists()
        )

    def test_role_with_availability_for_multiple_offerings(self):
        offering_ct = ContentType.objects.get_for_model(marketplace_models.Offering)
        RoleAvailability.objects.create(
            role=self.role,
            content_type=offering_ct,
            object_id=self.offering_a.id,
        )
        RoleAvailability.objects.create(
            role=self.role,
            content_type=offering_ct,
            object_id=self.offering_b.id,
        )

        self.assertEqual(self.role.availability.count(), 2)

    def test_role_with_availability_for_customer(self):
        customer = self.fixture.customer
        customer_ct = ContentType.objects.get_for_model(Customer)
        project_ct = ContentType.objects.get_for_model(Project)

        custom_role = Role.objects.create(
            name="Billing Viewer",
            content_type=project_ct,
            is_system_role=False,
        )
        RoleAvailability.objects.create(
            role=custom_role,
            content_type=customer_ct,
            object_id=customer.id,
        )

        self.assertTrue(
            custom_role.availability.filter(
                content_type=customer_ct, object_id=customer.id
            ).exists()
        )

    def test_duplicate_availability_is_rejected(self):
        offering_ct = ContentType.objects.get_for_model(marketplace_models.Offering)
        RoleAvailability.objects.create(
            role=self.role,
            content_type=offering_ct,
            object_id=self.offering_a.id,
        )
        with self.assertRaises(IntegrityError):
            RoleAvailability.objects.create(
                role=self.role,
                content_type=offering_ct,
                object_id=self.offering_a.id,
            )


class ResourceProjectRoleTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.user = structure_factories.UserFactory()

        self.resource_project_ct = ContentType.objects.get_for_model(
            marketplace_models.ResourceProject
        )
        self.resource_project_a = marketplace_models.ResourceProject.objects.create(
            resource=self.resource, name="Project A"
        )
        self.resource_project_b = marketplace_models.ResourceProject.objects.create(
            resource=self.resource, name="Project B"
        )

        self.admin_role = Role.objects.create(
            name="Namespace Admin",
            content_type=self.resource_project_ct,
            is_system_role=False,
        )
        self.viewer_role = Role.objects.create(
            name="Namespace Viewer",
            content_type=self.resource_project_ct,
            is_system_role=False,
        )

        # Add a permission to the admin role
        self.admin_role.add_permission(PermissionEnum.UPDATE_RESOURCE)

    def test_user_role_scoped_to_resource_project(self):
        UserRole.objects.create(
            user=self.user,
            role=self.admin_role,
            content_type=self.resource_project_ct,
            object_id=self.resource_project_a.id,
        )

        self.assertTrue(
            UserRole.objects.filter(
                user=self.user,
                role=self.admin_role,
                content_type=self.resource_project_ct,
                object_id=self.resource_project_a.id,
                is_active=True,
            ).exists()
        )

    def test_permission_granted_on_project_a_does_not_leak_to_project_b(self):
        UserRole.objects.create(
            user=self.user,
            role=self.admin_role,
            content_type=self.resource_project_ct,
            object_id=self.resource_project_a.id,
        )

        request = type("Request", (), {"user": self.user})()
        self.assertTrue(
            has_permission(
                request, PermissionEnum.UPDATE_RESOURCE, self.resource_project_a
            )
        )
        self.assertFalse(
            has_permission(
                request, PermissionEnum.UPDATE_RESOURCE, self.resource_project_b
            )
        )

    def test_different_roles_in_different_projects(self):
        UserRole.objects.create(
            user=self.user,
            role=self.admin_role,
            content_type=self.resource_project_ct,
            object_id=self.resource_project_a.id,
        )
        UserRole.objects.create(
            user=self.user,
            role=self.viewer_role,
            content_type=self.resource_project_ct,
            object_id=self.resource_project_b.id,
        )

        request = type("Request", (), {"user": self.user})()

        # Admin permission in project A
        self.assertTrue(
            has_permission(
                request, PermissionEnum.UPDATE_RESOURCE, self.resource_project_a
            )
        )
        # No admin permission in project B (only viewer)
        self.assertFalse(
            has_permission(
                request, PermissionEnum.UPDATE_RESOURCE, self.resource_project_b
            )
        )

    def test_user_without_role_has_no_access(self):
        other_user = structure_factories.UserFactory()
        request = type("Request", (), {"user": other_user})()
        self.assertFalse(
            has_permission(
                request, PermissionEnum.UPDATE_RESOURCE, self.resource_project_a
            )
        )

    def test_staff_bypass_works_for_resource_project(self):
        staff = structure_factories.UserFactory(is_staff=True)
        request = type("Request", (), {"user": staff})()
        self.assertTrue(
            has_permission(
                request, PermissionEnum.UPDATE_RESOURCE, self.resource_project_a
            )
        )

    def test_different_offerings_have_different_resource_project_roles(self):
        offering_ct = ContentType.objects.get_for_model(marketplace_models.Offering)

        rancher_role = Role.objects.create(
            name="Rancher Project Admin",
            content_type=self.resource_project_ct,
            is_system_role=False,
        )
        RoleAvailability.objects.create(
            role=rancher_role,
            content_type=offering_ct,
            object_id=self.fixture.offering.id,
        )

        other_offering = marketplace_factories.OfferingFactory(
            customer=structure_factories.CustomerFactory()
        )
        slurm_role = Role.objects.create(
            name="SLURM Account User",
            content_type=self.resource_project_ct,
            is_system_role=False,
        )
        RoleAvailability.objects.create(
            role=slurm_role,
            content_type=offering_ct,
            object_id=other_offering.id,
        )

        # Rancher role is available only for the first offering
        self.assertTrue(
            rancher_role.availability.filter(
                content_type=offering_ct, object_id=self.fixture.offering.id
            ).exists()
        )
        self.assertFalse(
            rancher_role.availability.filter(
                content_type=offering_ct, object_id=other_offering.id
            ).exists()
        )

        # SLURM role is available only for the second offering
        self.assertFalse(
            slurm_role.availability.filter(
                content_type=offering_ct, object_id=self.fixture.offering.id
            ).exists()
        )
        self.assertTrue(
            slurm_role.availability.filter(
                content_type=offering_ct, object_id=other_offering.id
            ).exists()
        )


class ResourceLevelPermissionTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.user = structure_factories.UserFactory()
        self.resource_ct = ContentType.objects.get_for_model(
            marketplace_models.Resource
        )
        self.role = Role.objects.create(
            name="Resource Manager",
            content_type=self.resource_ct,
            is_system_role=False,
        )
        self.role.add_permission(PermissionEnum.UPDATE_RESOURCE)

    def test_has_permission_on_resource_scope(self):
        UserRole.objects.create(
            user=self.user,
            role=self.role,
            content_type=self.resource_ct,
            object_id=self.resource.id,
        )

        request = type("Request", (), {"user": self.user})()
        self.assertTrue(
            has_permission(request, PermissionEnum.UPDATE_RESOURCE, self.resource)
        )

    def test_no_permission_on_different_resource(self):
        UserRole.objects.create(
            user=self.user,
            role=self.role,
            content_type=self.resource_ct,
            object_id=self.resource.id,
        )

        other_resource = marketplace_factories.ResourceFactory(
            offering=self.fixture.offering
        )
        request = type("Request", (), {"user": self.user})()
        self.assertFalse(
            has_permission(request, PermissionEnum.UPDATE_RESOURCE, other_resource)
        )


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class RoleAvailabilityRemovalCascadeTest(test.APITestCase):
    """Active UserRoles are revoked when their underlying RoleAvailability is removed."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.other_offering = marketplace_factories.OfferingFactory()
        self.resource = self.fixture.resource
        self.other_resource = marketplace_factories.ResourceFactory(
            offering=self.other_offering
        )
        self.user = structure_factories.UserFactory()

        self.resource_ct = ContentType.objects.get_for_model(
            marketplace_models.Resource
        )
        self.offering_ct = ContentType.objects.get_for_model(
            marketplace_models.Offering
        )
        self.role = Role.objects.create(
            name="Cluster Admin",
            content_type=self.resource_ct,
            is_system_role=False,
        )

    def _grant(self, scope):
        return UserRole.objects.create(
            user=self.user,
            role=self.role,
            content_type=self.resource_ct,
            object_id=scope.id,
        )

    def test_remove_only_availability_does_not_revoke(self):
        """Per RoleAvailability docstring, no rows = available everywhere.

        Removing the last row makes the role globally valid; existing grants
        stay active.
        """
        availability = RoleAvailability.objects.create(
            role=self.role,
            content_type=self.offering_ct,
            object_id=self.offering.id,
        )
        user_role = self._grant(self.resource)

        with self.captureOnCommitCallbacks(execute=True):
            availability.delete()

        user_role.refresh_from_db()
        self.assertTrue(user_role.is_active)

    def test_remove_matching_availability_revokes_dependent_user_role(self):
        a1 = RoleAvailability.objects.create(
            role=self.role,
            content_type=self.offering_ct,
            object_id=self.offering.id,
        )
        # Second availability for an unrelated offering keeps the role
        # constrained but not via offering A — so removing A revokes
        # the grant whose ancestor was offering A.
        RoleAvailability.objects.create(
            role=self.role,
            content_type=self.offering_ct,
            object_id=self.other_offering.id,
        )
        user_role = self._grant(self.resource)

        with self.captureOnCommitCallbacks(execute=True):
            a1.delete()

        user_role.refresh_from_db()
        self.assertFalse(user_role.is_active)

    def test_remove_unrelated_availability_keeps_user_role(self):
        RoleAvailability.objects.create(
            role=self.role,
            content_type=self.offering_ct,
            object_id=self.offering.id,
        )
        unrelated = RoleAvailability.objects.create(
            role=self.role,
            content_type=self.offering_ct,
            object_id=self.other_offering.id,
        )
        user_role = self._grant(self.resource)

        with self.captureOnCommitCallbacks(execute=True):
            unrelated.delete()

        user_role.refresh_from_db()
        self.assertTrue(user_role.is_active)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class RoleAvailabilityAdminEndpointTest(test.APITestCase):
    """Staff-only audit endpoint at /api/role-availabilities/."""

    def setUp(self):
        from rest_framework.reverse import reverse as rev

        self.staff = structure_factories.UserFactory(is_staff=True)
        self.regular = structure_factories.UserFactory()

        rp_ct = ContentType.objects.get_for_model(marketplace_models.ResourceProject)
        self.role = Role.objects.create(
            name="Cluster Admin", content_type=rp_ct, is_system_role=False
        )
        self.offering = marketplace_factories.OfferingFactory()
        self.offering_ct = ContentType.objects.get_for_model(
            marketplace_models.Offering
        )
        self.availability = RoleAvailability.objects.create(
            role=self.role,
            content_type=self.offering_ct,
            object_id=self.offering.id,
        )
        self.list_url = "http://testserver" + rev("role-availability-list")
        self.detail_url = "http://testserver" + rev(
            "role-availability-detail", kwargs={"uuid": self.availability.uuid.hex}
        )

    def test_staff_can_list(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertIn(self.availability.uuid.hex, uuids)

    def test_non_staff_sees_empty_list(self):
        self.client.force_authenticate(self.regular)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_filter_by_role_uuid(self):
        other_role = Role.objects.create(
            name="Other",
            content_type=ContentType.objects.get_for_model(marketplace_models.Resource),
            is_system_role=False,
        )
        RoleAvailability.objects.create(
            role=other_role,
            content_type=self.offering_ct,
            object_id=self.offering.id,
        )
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.list_url, {"role_uuid": self.role.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {row["role_name"] for row in response.data}
        self.assertEqual(names, {self.role.name})

    def test_staff_can_delete(self):
        self.client.force_authenticate(self.staff)
        response = self.client.delete(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            RoleAvailability.objects.filter(uuid=self.availability.uuid).exists()
        )

    def test_non_staff_cannot_delete(self):
        self.client.force_authenticate(self.regular)
        response = self.client.delete(self.detail_url)
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_is_profile_managed_flag(self):
        from waldur_mastermind.marketplace import models as mp_models

        profile = mp_models.OfferingProfile.objects.create(name="rancher")
        profile.roles.add(self.role)
        with self.captureOnCommitCallbacks(execute=True):
            self.offering.profile = profile
            self.offering.save()
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.list_url, {"role_uuid": self.role.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        flags = {row["uuid"]: row["is_profile_managed"] for row in response.data}
        self.assertTrue(flags[self.availability.uuid.hex])
