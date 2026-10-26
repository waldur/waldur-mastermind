from unittest import mock

from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_rancher import models

from . import factories, fixtures


def role_url(role):
    return "http://testserver" + reverse(
        "rancher-role-template-detail", kwargs={"uuid": role.uuid.hex}
    )


@mock.patch("waldur_rancher.utils.send_user_membership_notification_email")
@mock.patch("waldur_rancher.backend.KeycloakBackend")
@mock.patch("waldur_rancher.backend.RancherBackend.client")
class KeycloakMembershipPermissionTest(test.APITestCase):
    """Only staff and administrators of the cluster's project manage its role groups."""

    def setUp(self):
        self.fixture = fixtures.RancherFixture()
        self.url = "http://testserver" + reverse("keycloak-user-group-membership-list")

    def configure_keycloak(self, keycloak):
        keycloak.return_value.create_group.return_value = {"id": "keycloak-group"}
        keycloak.return_value.find_user_by_username.return_value = None

    def join(self, user, role=None, scope=None):
        self.client.force_authenticate(user)
        return self.client.post(
            self.url,
            {
                "username": "new-cluster-user",
                "email": "new-cluster-user@example.com",
                "scope_uuid": (scope or self.fixture.cluster).uuid.hex,
                "role": role_url(role or self.fixture.cluster_owner_role),
            },
        )

    def make_membership(self):
        group = models.KeycloakGroup.objects.create(
            name="group",
            scope_uuid=self.fixture.cluster.uuid,
            role=self.fixture.cluster_owner_role,
        )
        membership = models.KeycloakUserGroupMembership.objects.create(
            username="existing-user", email="existing@example.com", group=group
        )
        return "http://testserver" + reverse(
            "keycloak-user-group-membership-detail",
            kwargs={"uuid": membership.uuid.hex},
        )

    def test_outsider_cannot_join_cluster_group(self, client, keycloak, notify):
        self.configure_keycloak(keycloak)
        response = self.join(structure_factories.UserFactory())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.KeycloakUserGroupMembership.objects.exists())
        keycloak.return_value.create_group.assert_not_called()

    def test_project_member_cannot_add_cluster_group_member(
        self, client, keycloak, notify
    ):
        self.configure_keycloak(keycloak)
        response = self.join(self.fixture.member)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.KeycloakUserGroupMembership.objects.exists())

    def test_project_admin_can_add_cluster_group_member(self, client, keycloak, notify):
        self.configure_keycloak(keycloak)
        response = self.join(self.fixture.admin)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_staff_can_add_cluster_group_member(self, client, keycloak, notify):
        self.configure_keycloak(keycloak)
        response = self.join(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_role_of_another_rancher_server_is_rejected(self, client, keycloak, notify):
        self.configure_keycloak(keycloak)
        foreign_role = factories.RoleTemplateFactory(name="cluster-owner")
        response = self.join(self.fixture.staff, role=foreign_role)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(models.KeycloakUserGroupMembership.objects.exists())

    def test_project_member_cannot_delete_membership(self, client, keycloak, notify):
        url = self.make_membership()
        self.client.force_authenticate(self.fixture.member)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.KeycloakUserGroupMembership.objects.exists())

    def test_memberships_cannot_be_updated(self, client, keycloak, notify):
        url = self.make_membership()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(url, {"username": "someone-else"})
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_outsider_cannot_tell_whether_a_scope_exists(
        self, client, keycloak, notify
    ):
        self.configure_keycloak(keycloak)
        missing_cluster = factories.ClusterFactory.build(settings=self.fixture.settings)
        response = self.join(structure_factories.UserFactory(), scope=missing_cluster)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_outsider_cannot_tell_whether_a_user_holds_a_role(
        self, client, keycloak, notify
    ):
        self.make_membership()
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.post(
            self.url,
            {
                "username": "existing-user",
                "email": "existing@example.com",
                "scope_uuid": self.fixture.cluster.uuid.hex,
                "role": role_url(self.fixture.cluster_owner_role),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_project_admin_can_add_rancher_project_group_member(
        self, client, keycloak, notify
    ):
        self.configure_keycloak(keycloak)
        rancher_project = factories.ProjectFactory(
            cluster=self.fixture.cluster, settings=self.fixture.settings
        )
        response = self.join(
            self.fixture.admin,
            role=self.fixture.project_owner_role,
            scope=rancher_project,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_project_member_cannot_add_rancher_project_group_member(
        self, client, keycloak, notify
    ):
        self.configure_keycloak(keycloak)
        rancher_project = factories.ProjectFactory(
            cluster=self.fixture.cluster, settings=self.fixture.settings
        )
        response = self.join(
            self.fixture.member,
            role=self.fixture.project_owner_role,
            scope=rancher_project,
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_project_admin_can_delete_membership(self, client, keycloak, notify):
        url = self.make_membership()
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)


@mock.patch("waldur_rancher.backend.KeycloakBackend")
@mock.patch("waldur_rancher.backend.RancherBackend.client")
class KeycloakGroupDeletionTest(test.APITestCase):
    def test_deleted_group_is_removed_from_keycloak_once(self, client, keycloak):
        fixture = fixtures.RancherFixture()
        keycloak.return_value.get_group.return_value = {"id": "kc", "parentId": None}
        group = models.KeycloakGroup.objects.create(
            name="group",
            scope_uuid=fixture.cluster.uuid,
            role=fixture.cluster_owner_role,
            backend_id="kc",
        )

        group.delete()

        keycloak.return_value.delete_group.assert_called_once_with("kc")
        self.assertFalse(models.KeycloakGroup.objects.exists())
