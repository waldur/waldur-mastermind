from unittest import mock

from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_keycloak import models, tasks
from waldur_keycloak.tests import factories, fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class ResourceUserToKeycloakSyncTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()

    def test_creating_resource_user_creates_keycloak_membership(self):
        """When a ResourceUser is created for a keycloak-enabled offering,
        a corresponding OfferingKeycloakMembership should be auto-created."""
        resource = self.fixture.resource
        user = self.fixture.owner

        marketplace_models.ResourceUser.objects.create(
            resource=resource,
            user=user,
            role=self.fixture.offering_role,
        )

        membership = models.OfferingKeycloakMembership.objects.filter(
            user=user,
            group__offering=self.fixture.offering,
            group__role=self.fixture.offering_role,
            group__resource=resource,
        )
        self.assertTrue(membership.exists())

    def test_creating_resource_user_does_not_create_membership_if_keycloak_disabled(
        self,
    ):
        """If keycloak is not enabled, no membership should be created."""
        offering = marketplace_factories.OfferingFactory()
        resource = marketplace_factories.ResourceFactory(
            offering=offering,
            project=self.fixture.project,
        )
        role = factories.OfferingUserRoleFactory(offering=offering)

        marketplace_models.ResourceUser.objects.create(
            resource=resource,
            user=self.fixture.owner,
            role=role,
        )

        self.assertFalse(
            models.OfferingKeycloakMembership.objects.filter(
                user=self.fixture.owner,
                group__offering=offering,
            ).exists()
        )


class ResourceUserDeleteSyncTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()

    def test_deleting_resource_user_deletes_keycloak_membership(self):
        """When a ResourceUser is deleted, the corresponding membership should be removed."""
        resource = self.fixture.resource
        user = self.fixture.owner

        resource_user = marketplace_models.ResourceUser.objects.create(
            resource=resource,
            user=user,
            role=self.fixture.offering_role,
        )

        # Verify membership was created
        self.assertTrue(
            models.OfferingKeycloakMembership.objects.filter(
                user=user,
                group__resource=resource,
                group__role=self.fixture.offering_role,
            ).exists()
        )

        resource_user.delete()

        # Verify membership was removed
        self.assertFalse(
            models.OfferingKeycloakMembership.objects.filter(
                user=user,
                group__resource=resource,
                group__role=self.fixture.offering_role,
            ).exists()
        )


class KeycloakGroupDeleteHandlerTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()

    @mock.patch("waldur_keycloak.utils.get_keycloak_client_for_offering")
    def test_group_delete_calls_keycloak_backend(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.get_group.return_value = {
            "id": self.fixture.keycloak_group.backend_id,
            "name": self.fixture.keycloak_group.name,
        }

        self.fixture.keycloak_group.delete()
        mock_keycloak.delete_group.assert_called_once()


class KeycloakMembershipDeleteHandlerTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()

    @mock.patch("waldur_keycloak.utils.get_keycloak_client_for_offering")
    def test_membership_delete_calls_keycloak_backend(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.find_user_by_username.return_value = {
            "id": "kc-user-id",
        }
        mock_keycloak.get_group.return_value = {
            "id": self.fixture.keycloak_group.backend_id,
        }

        self.fixture.keycloak_membership.delete()
        mock_keycloak.remove_user_from_group.assert_called_once()


class EmptyGroupCleanupTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()

    @mock.patch("waldur_keycloak.utils.get_keycloak_client_for_offering")
    def test_deleting_last_membership_deletes_empty_group(self, mock_get_client):
        """When the last membership in a group is deleted,
        the group itself should also be deleted."""
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.find_user_by_username.return_value = {
            "id": "kc-user-id",
        }
        mock_keycloak.get_group.return_value = {
            "id": self.fixture.keycloak_group.backend_id,
        }

        group_pk = self.fixture.keycloak_group.pk

        # The fixture creates exactly one membership in the group.
        # Deleting it should trigger group deletion.
        self.fixture.keycloak_membership.delete()

        self.assertFalse(
            models.OfferingKeycloakGroup.objects.filter(pk=group_pk).exists()
        )


class ResourceDeletionCascadeTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()

    @mock.patch("waldur_keycloak.utils.get_keycloak_client_for_offering")
    def test_resource_deletion_deletes_keycloak_groups(self, mock_get_client):
        """When a marketplace Resource is deleted, all its Keycloak groups
        should also be deleted via the pre_delete handler."""
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.get_group.return_value = {
            "id": "backend-group-id",
        }

        resource = self.fixture.resource
        # Create a new role to avoid unique_together conflict with the fixture group
        role = factories.OfferingUserRoleFactory(
            offering=self.fixture.offering,
            name="ResourceRole",
        )
        # Create a group linked to this resource
        group = factories.OfferingKeycloakGroupFactory(
            offering=self.fixture.offering,
            role=role,
            resource=resource,
        )
        group_pk = group.pk

        resource.delete()

        self.assertFalse(
            models.OfferingKeycloakGroup.objects.filter(pk=group_pk).exists()
        )


class UserDeactivationCleanupTest(TestCase):
    """Gap #1: When a user is deactivated, their Keycloak memberships
    and ResourceUser records should be removed."""

    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()

    def _deactivate_user(self, user):
        """Deactivate user via queryset.update() to avoid triggering
        signal handlers — we test the task function directly."""
        from waldur_core.core.models import User

        User.objects.filter(pk=user.pk).update(is_active=False)

    def test_deactivated_user_memberships_are_removed(self):
        user = self.fixture.owner
        membership = self.fixture.keycloak_membership
        membership.user = user
        membership.save()

        self._deactivate_user(user)
        tasks.cleanup_keycloak_for_deactivated_user(user.uuid.hex)

        self.assertFalse(
            models.OfferingKeycloakMembership.objects.filter(pk=membership.pk).exists()
        )

    def test_deactivated_user_resource_users_are_removed(self):
        user = self.fixture.owner
        resource = self.fixture.resource

        resource_user = marketplace_models.ResourceUser.objects.create(
            resource=resource,
            user=user,
            role=self.fixture.offering_role,
        )

        self._deactivate_user(user)
        tasks.cleanup_keycloak_for_deactivated_user(user.uuid.hex)

        self.assertFalse(
            marketplace_models.ResourceUser.objects.filter(pk=resource_user.pk).exists()
        )

    def test_active_user_keeps_memberships(self):
        user = self.fixture.owner
        membership = self.fixture.keycloak_membership
        membership.user = user
        membership.save()

        # User is active — task should be a no-op
        tasks.cleanup_keycloak_for_deactivated_user(user.uuid.hex)

        self.assertTrue(
            models.OfferingKeycloakMembership.objects.filter(pk=membership.pk).exists()
        )


class ProjectRoleRevocationCleanupTest(TestCase):
    """Gap #3: When a user's project role is revoked and they no longer
    have access, their ResourceUser and Keycloak memberships for that
    project's resources should be removed."""

    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()

    def test_losing_project_access_removes_resource_users_and_memberships(self):
        # Create a standalone user with no project/customer roles
        user = structure_factories.UserFactory()
        resource = self.fixture.resource

        # Create ResourceUser (which auto-creates keycloak membership)
        marketplace_models.ResourceUser.objects.create(
            resource=resource,
            user=user,
            role=self.fixture.offering_role,
        )

        self.assertTrue(
            marketplace_models.ResourceUser.objects.filter(
                resource=resource, user=user
            ).exists()
        )
        self.assertTrue(
            models.OfferingKeycloakMembership.objects.filter(
                user=user, group__resource=resource
            ).exists()
        )

        # User has no remaining project/customer access → everything cleaned up
        tasks.cleanup_keycloak_for_lost_project_access(
            user.uuid.hex, self.fixture.project.uuid.hex
        )

        self.assertFalse(
            marketplace_models.ResourceUser.objects.filter(
                resource=resource, user=user
            ).exists()
        )
        self.assertFalse(
            models.OfferingKeycloakMembership.objects.filter(
                user=user, group__resource=resource
            ).exists()
        )

    def test_user_with_remaining_project_role_keeps_access(self):
        """If user still has another role on the project, nothing is cleaned up."""
        user = self.fixture.admin  # Has ADMIN role
        resource = self.fixture.resource

        marketplace_models.ResourceUser.objects.create(
            resource=resource,
            user=user,
            role=self.fixture.offering_role,
        )

        # User still has an active project role → task should be a no-op
        tasks.cleanup_keycloak_for_lost_project_access(
            user.uuid.hex, self.fixture.project.uuid.hex
        )

        self.assertTrue(
            marketplace_models.ResourceUser.objects.filter(
                resource=resource, user=user
            ).exists()
        )

    def test_customer_owner_keeps_access_after_project_role_revocation(self):
        """Customer owner retains access even without a direct project role."""
        user = self.fixture.owner  # Has customer OWNER role
        resource = self.fixture.resource

        marketplace_models.ResourceUser.objects.create(
            resource=resource,
            user=user,
            role=self.fixture.offering_role,
        )

        # Owner has customer-level access → task should be a no-op
        tasks.cleanup_keycloak_for_lost_project_access(
            user.uuid.hex, self.fixture.project.uuid.hex
        )

        self.assertTrue(
            marketplace_models.ResourceUser.objects.filter(
                resource=resource, user=user
            ).exists()
        )

    def test_staff_user_is_not_affected(self):
        """Staff users should never have their access cleaned up."""
        user = self.fixture.staff
        resource = self.fixture.resource

        resource_user = marketplace_models.ResourceUser.objects.create(
            resource=resource,
            user=user,
            role=self.fixture.offering_role,
        )

        tasks.cleanup_keycloak_for_lost_project_access(
            user.uuid.hex, self.fixture.project.uuid.hex
        )

        self.assertTrue(
            marketplace_models.ResourceUser.objects.filter(pk=resource_user.pk).exists()
        )

    def test_non_keycloak_offerings_are_not_affected(self):
        """Resources in offerings without keycloak_enabled should not be touched."""
        user = structure_factories.UserFactory()
        offering = marketplace_factories.OfferingFactory()  # No keycloak_enabled
        resource = marketplace_factories.ResourceFactory(
            offering=offering,
            project=self.fixture.project,
        )
        role = factories.OfferingUserRoleFactory(offering=offering)

        resource_user = marketplace_models.ResourceUser.objects.create(
            resource=resource,
            user=user,
            role=role,
        )

        tasks.cleanup_keycloak_for_lost_project_access(
            user.uuid.hex, self.fixture.project.uuid.hex
        )

        self.assertTrue(
            marketplace_models.ResourceUser.objects.filter(pk=resource_user.pk).exists()
        )
