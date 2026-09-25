"""Organization owners see the protected calls their organization runs.

CUSTOMER.OWNER carries the call team-management permissions, so without
CALL.LIST -- which the call queryset requires of organization-level roles --
those permissions pointed at calls the owner could not even open.
"""

import importlib
import io
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, CustomerRole
from waldur_core.permissions.models import Role, RolePermission
from waldur_core.permissions.tests.test_system_role_descriptions import (
    permissions_yaml_rows,
)
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.proposal.tests import factories, fixtures


class OwnerSeesProtectedCallsTest(APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.owner = self.fixture.owner
        self.client.force_authenticate(self.owner)

    def test_shipped_owner_role_carries_list_calls(self):
        # Tests run without permissions.yaml (and CI without migrations), so
        # ProposalFixture grants CALL.LIST by hand; this pins the shipped role.
        rows = permissions_yaml_rows()
        if rows is None:
            self.skipTest("permissions.yaml is not part of this checkout")
        owner = next(row for row in rows if row["role"] == "CUSTOMER.OWNER")
        self.assertIn(PermissionEnum.LIST_CALLS.value, owner["permissions"])

    def test_owner_lists_their_organization_calls(self):
        response = self.client.get(factories.CallFactory.get_protected_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(self.call.uuid.hex, {c["uuid"] for c in response.data})

    def test_owner_retrieves_their_organization_call(self):
        response = self.client.get(factories.CallFactory.get_protected_url(self.call))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_owner_of_another_organization_does_not_see_the_call(self):
        other_owner = structure_fixtures.CustomerFixture().owner
        self.client.force_authenticate(other_owner)
        response = self.client.get(factories.CallFactory.get_protected_url(self.call))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_owner_still_cannot_update_the_call(self):
        response = self.client.patch(
            factories.CallFactory.get_protected_url(self.call),
            {"description": "changed"},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_can_manage_the_call_team(self):
        # The team-management permissions come from the shipped permissions.yaml,
        # which the test database's migrated roles do not carry.
        call_command(
            "import_roles",
            str(Path(settings.BASE_DIR) / "docker/rootfs/etc/waldur/permissions.yaml"),
            stdout=io.StringIO(),
        )
        member = structure_factories.UserFactory()
        url = factories.CallFactory.get_protected_url(self.call)
        response = self.client.post(
            url + "add_user/",
            {"user": member.uuid.hex, "role": CallRole.PANEL_MEMBER.name},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        response = self.client.post(
            url + "delete_user/",
            {"user": member.uuid.hex, "role": CallRole.PANEL_MEMBER.name},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class OwnerListCallsMigrationTest(APITestCase):
    migration = importlib.import_module(
        "waldur_core.permissions.migrations.0031_owner_list_calls"
    )

    def test_owner_clones_receive_list_calls(self):
        clone = Role.objects.create(
            name="CUSTOMER.acme.OWNER",
            content_type=ContentType.objects.get_for_model(Customer),
            template=CustomerRole.OWNER,
        )
        self.migration.add_list_calls_to_owners(apps, None)
        self.assertTrue(
            RolePermission.objects.filter(
                role=clone, permission=PermissionEnum.LIST_CALLS
            ).exists()
        )

    def test_other_customer_roles_are_left_alone(self):
        self.migration.add_list_calls_to_owners(apps, None)
        self.assertFalse(
            RolePermission.objects.filter(
                role=CustomerRole.SUPPORT, permission=PermissionEnum.LIST_CALLS
            ).exists()
        )
