"""Organization readers see, but cannot change, their organization.

CUSTOMER.READER used to ship with CUSTOMER.VIEW_TEAM only, so a reader could
not list the organization's users, projects, resources, orders or
invitations.
"""

import io
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.tests.test_system_role_descriptions import (
    permissions_yaml_rows,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.utils import client_add_user, client_list_users
from waldur_core.users.tests import factories as users_factories
from waldur_mastermind.marketplace.tests import factories, fixtures

READER_PERMISSIONS = (
    PermissionEnum.LIST_CUSTOMER_USERS,
    PermissionEnum.LIST_PROJECTS,
    PermissionEnum.LIST_RESOURCES,
    PermissionEnum.LIST_ORDERS,
    PermissionEnum.LIST_INVITATIONS,
)


class ShippedReaderRoleTest(test.APITestCase):
    def test_shipped_reader_role_carries_read_permissions(self):
        rows = permissions_yaml_rows()
        if rows is None:
            self.skipTest("permissions.yaml is not part of this checkout")
        reader = next(row for row in rows if row["role"] == "CUSTOMER.READER")
        for permission in READER_PERMISSIONS:
            self.assertIn(permission.value, reader["permissions"])


class CustomerReaderAccessTest(test.APITestCase):
    def setUp(self):
        # Tests run without permissions.yaml, so load the shipped roles.
        call_command(
            "import_roles",
            str(Path(settings.BASE_DIR) / "docker/rootfs/etc/waldur/permissions.yaml"),
            stdout=io.StringIO(),
        )
        self.fixture = fixtures.MarketplaceFixture()
        self.order = self.fixture.order
        self.reader = structure_factories.UserFactory()
        self.fixture.customer.add_user(self.reader, CustomerRole.READER)
        self.client.force_authenticate(self.reader)

    def list_uuids(self, url, **params):
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {item["uuid"] for item in response.data}

    def test_reader_lists_organization_projects(self):
        uuids = self.list_uuids(structure_factories.ProjectFactory.get_list_url())
        self.assertIn(self.fixture.project.uuid.hex, uuids)

    def test_reader_lists_organization_resources(self):
        uuids = self.list_uuids(factories.ResourceFactory.get_list_url())
        self.assertIn(self.fixture.resource.uuid.hex, uuids)

    def test_reader_lists_organization_orders(self):
        uuids = self.list_uuids(factories.OrderFactory.get_list_url())
        self.assertIn(self.order.uuid.hex, uuids)

    def test_reader_lists_organization_users(self):
        owner = self.fixture.owner
        url = (
            structure_factories.CustomerFactory.get_url(self.fixture.customer)
            + "users/"
        )
        uuids = self.list_uuids(url)
        self.assertIn(owner.uuid.hex, uuids)

    def test_reader_sees_every_project_team_of_organization(self):
        first_project = self.fixture.project
        second_project = structure_factories.ProjectFactory(
            customer=self.fixture.customer
        )
        first_member = self.fixture.member
        second_manager = structure_factories.UserFactory()
        second_project.add_user(second_manager, ProjectRole.MANAGER)

        url = (
            structure_factories.CustomerFactory.get_url(self.fixture.customer)
            + "users/"
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        projects_by_user = {
            item["uuid"]: {(p["uuid"], p["role_name"]) for p in item["projects"]}
            for item in response.data
        }
        self.assertEqual(
            projects_by_user[first_member.uuid.hex],
            {(str(first_project.uuid), ProjectRole.MEMBER.name)},
        )
        self.assertEqual(
            projects_by_user[second_manager.uuid.hex],
            {(str(second_project.uuid), ProjectRole.MANAGER.name)},
        )

    def test_reader_lists_project_members(self):
        member = self.fixture.member
        response = client_list_users(self.client, self.reader, self.fixture.project)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn(
            str(member.uuid), {str(item["user_uuid"]) for item in response.data}
        )

    def test_reader_cannot_add_project_member(self):
        response = client_add_user(
            self.client,
            self.reader,
            structure_factories.UserFactory(),
            self.fixture.project,
            ProjectRole.MEMBER,
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_reader_lists_organization_invitations(self):
        invitation = users_factories.ProjectInvitationFactory(
            scope=self.fixture.project
        )
        uuids = self.list_uuids(users_factories.InvitationBaseFactory.get_list_url())
        self.assertIn(invitation.uuid.hex, uuids)

    def test_reader_cannot_cancel_invitation(self):
        invitation = users_factories.ProjectInvitationFactory(
            scope=self.fixture.project
        )
        response = self.client.post(
            users_factories.InvitationBaseFactory.get_url(invitation, "cancel")
        )
        # Managing an invitation needs more than seeing it; cancel answers 404.
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_reader_of_another_organization_sees_nothing(self):
        other = structure_factories.CustomerFactory()
        stranger = structure_factories.UserFactory()
        other.add_user(stranger, CustomerRole.READER)
        self.client.force_authenticate(stranger)
        self.assertEqual(
            self.list_uuids(structure_factories.ProjectFactory.get_list_url()), set()
        )
        self.assertEqual(
            self.list_uuids(factories.ResourceFactory.get_list_url()), set()
        )
        self.assertEqual(self.list_uuids(factories.OrderFactory.get_list_url()), set())

    def test_reader_cannot_create_project(self):
        response = self.client.post(
            structure_factories.ProjectFactory.get_list_url(),
            {
                "name": "New project",
                "customer": structure_factories.CustomerFactory.get_url(
                    self.fixture.customer
                ),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_reader_cannot_update_project(self):
        response = self.client.patch(
            structure_factories.ProjectFactory.get_url(self.fixture.project),
            {"name": "Renamed"},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
