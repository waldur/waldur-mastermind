from unittest import mock

from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_keycloak import models, tasks
from waldur_keycloak.tests import factories, fixtures


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
        role = factories.RoleFactory(name="ResourceRole")
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
    """When a user is deactivated, their Keycloak memberships
    should be removed."""

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
    """When a user's project role is revoked and they no longer
    have access, their Keycloak memberships for that
    project's resources should be removed."""

    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()

    def test_losing_project_access_removes_memberships(self):
        # Create a standalone user with no project/customer roles
        user = structure_factories.UserFactory()

        # Create a keycloak group linked to the resource
        resource_group = factories.OfferingKeycloakGroupFactory(
            offering=self.fixture.offering,
            role=self.fixture.offering_role,
            resource=self.fixture.resource,
        )

        # Create keycloak membership directly
        membership = factories.OfferingKeycloakMembershipFactory(
            group=resource_group,
            user=user,
            username=user.username,
        )

        self.assertTrue(
            models.OfferingKeycloakMembership.objects.filter(pk=membership.pk).exists()
        )

        # User has no remaining project/customer access → everything cleaned up
        tasks.cleanup_keycloak_for_lost_project_access(
            user.uuid.hex, self.fixture.project.uuid.hex
        )

        self.assertFalse(
            models.OfferingKeycloakMembership.objects.filter(pk=membership.pk).exists()
        )
