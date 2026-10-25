import datetime
import io
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock
from unittest.mock import Mock

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test
from rest_framework.exceptions import ValidationError
from rest_framework.reverse import reverse

from waldur_core.permissions import enums, global_scope, hygiene, models, utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, GlobalRole, ProjectRole
from waldur_core.permissions.serializers import PermissionSerializer
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects_by_permission,
)
from waldur_core.structure.tests import factories, fixtures
from waldur_core.structure.tests.utils import client_add_user
from waldur_core.users.utils import can_manage_invitation_with
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal.tests import factories as proposal_factories


def assign_global_role(user, *permissions, role_name="GLOBAL.SUPPORT"):
    """Test helper. Production grants with GlobalRole.get and add_user."""
    role = GlobalRole.get(role_name)
    for permission in permissions:
        role.add_permission(permission)
    utils.grant_global_role(user, role)
    return role


class PersonalAccessToken:
    """Named to match the PAT auth check, which classifies by type name."""

    def __init__(self, scopes, allowed_scopes=None):
        self.scopes = scopes
        self.allowed_scopes = allowed_scopes or []


class GlobalScopePermissionTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.user = factories.UserFactory()
        self.role = assign_global_role(self.user, PermissionEnum.UPDATE_PROJECT)

    def test_permission_applies_without_a_role_on_the_project(self):
        self.assertTrue(
            utils.has_permission(self.user, PermissionEnum.UPDATE_PROJECT, self.project)
        )

    def test_permission_applies_in_another_organization(self):
        other = factories.ProjectFactory()
        self.assertTrue(
            utils.has_permission(self.user, PermissionEnum.UPDATE_PROJECT, other)
        )

    def test_a_permission_the_role_does_not_carry_is_denied(self):
        self.assertFalse(
            utils.has_permission(self.user, PermissionEnum.DELETE_PROJECT, self.project)
        )

    def test_connected_projects_include_every_project(self):
        other = factories.ProjectFactory()
        ids = set(
            get_connected_projects_by_permission(
                self.user, PermissionEnum.UPDATE_PROJECT
            )
        )
        self.assertIn(self.project.id, ids)
        self.assertIn(other.id, ids)

    def test_connected_projects_do_not_expand_a_permission_the_role_lacks(self):
        self.assertEqual(
            list(
                get_connected_projects_by_permission(
                    self.user, PermissionEnum.DELETE_PROJECT
                )
            ),
            [],
        )

    def test_a_global_role_is_not_membership_of_every_organization(self):
        self.assertEqual(list(get_connected_customers(self.user)), [])

    def test_users_with_permission_include_the_global_holder(self):
        other = factories.ProjectFactory()
        for project in (self.project, other):
            self.assertIn(
                self.user,
                utils.get_users_with_permission(project, PermissionEnum.UPDATE_PROJECT),
            )

    def test_users_with_permission_skip_a_permission_the_role_lacks(self):
        self.assertNotIn(
            self.user,
            utils.get_users_with_permission(
                self.project, PermissionEnum.DELETE_PROJECT
            ),
        )

    def test_support_flag_alone_does_not_grant_the_permission(self):
        support = factories.UserFactory(is_support=True)
        self.assertFalse(
            utils.has_permission(support, PermissionEnum.UPDATE_PROJECT, self.project)
        )

    def test_inactive_grant_is_denied(self):
        models.UserRole.objects.filter(user=self.user, role=self.role).update(
            is_active=False
        )
        self.assertFalse(
            utils.has_permission(self.user, PermissionEnum.UPDATE_PROJECT, self.project)
        )

    def test_has_any_permission_accepts_one_global_permission(self):
        self.assertTrue(
            utils.has_any_permission(
                self.user,
                [PermissionEnum.DELETE_PROJECT, PermissionEnum.UPDATE_PROJECT],
                self.project,
            )
        )
        self.assertFalse(
            utils.has_any_permission(
                self.user, [PermissionEnum.DELETE_PROJECT], self.project
            )
        )

    def test_role_index_includes_the_global_grant(self):
        index = utils.build_role_index(
            self.user, [(PermissionEnum.UPDATE_PROJECT, self.project)]
        )
        self.assertTrue(
            utils.has_permission(
                self.user,
                PermissionEnum.UPDATE_PROJECT,
                self.project,
                role_index=index,
            )
        )

    def test_role_index_stays_false_for_a_permission_the_role_lacks(self):
        index = utils.build_role_index(
            self.user, [(PermissionEnum.DELETE_PROJECT, self.project)]
        )
        self.assertFalse(
            utils.has_permission(
                self.user,
                PermissionEnum.DELETE_PROJECT,
                self.project,
                role_index=index,
            )
        )

    def test_pat_without_the_permission_cannot_use_the_grant(self):
        request = Mock(
            user=self.user,
            auth=PersonalAccessToken(scopes=[]),
        )
        self.assertFalse(
            utils.has_permission(request, PermissionEnum.UPDATE_PROJECT, self.project)
        )

    def test_pat_bound_to_another_organization_cannot_use_the_grant(self):
        other = factories.CustomerFactory()
        customer_ct = ContentType.objects.get_for_model(other)
        request = Mock(
            user=self.user,
            auth=PersonalAccessToken(
                scopes=[PermissionEnum.UPDATE_PROJECT.value],
                allowed_scopes=[
                    {"content_type_id": customer_ct.id, "object_id": other.id}
                ],
            ),
        )
        self.assertFalse(
            utils.has_permission(request, PermissionEnum.UPDATE_PROJECT, self.project)
        )

    def test_pat_that_lists_the_permission_can_use_the_grant(self):
        request = Mock(
            user=self.user,
            auth=PersonalAccessToken(scopes=[PermissionEnum.UPDATE_PROJECT.value]),
        )
        self.assertTrue(
            utils.has_permission(request, PermissionEnum.UPDATE_PROJECT, self.project)
        )

    def test_global_role_cannot_be_granted_on_a_customer(self):
        with self.assertRaises(ValidationError):
            utils.validate_role_grant(
                self.fixture.customer, factories.UserFactory(), self.role
            )

    def test_same_scope_row_is_reused(self):
        self.assertEqual(
            utils.get_global_role_scope().pk, utils.get_global_role_scope().pk
        )

    def test_grant_serializes_without_an_organization(self):
        user_role = models.UserRole.objects.get(user=self.user, role=self.role)
        data = PermissionSerializer(user_role).data
        self.assertEqual(data["scope_type"], "global")
        self.assertEqual(data["scope_name"], "Global")
        self.assertIsNone(data["customer_uuid"])

    def test_invitation_permission_applies_to_every_project(self):
        inviter = factories.UserFactory()
        assign_global_role(
            inviter,
            PermissionEnum.CREATE_PROJECT_PERMISSION,
            role_name="GLOBAL.INVITER",
        )
        request = Mock(user=inviter)
        self.assertTrue(can_manage_invitation_with(request, self.project))
        self.assertFalse(can_manage_invitation_with(request, self.fixture.customer))


class GlobalScopeApiTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project

    def test_support_user_with_update_can_rename_a_project(self):
        support = factories.UserFactory(is_support=True)
        assign_global_role(support, PermissionEnum.UPDATE_PROJECT)
        self.client.force_authenticate(support)
        response = self.client.patch(
            factories.ProjectFactory.get_url(self.project),
            {"name": "Renamed by global support"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "Renamed by global support")

    def test_changing_the_end_date_also_requires_delete_project(self):
        # The update action checks PROJECT.UPDATE. The serializer then
        # requires PROJECT.DELETE on the organization before the end date
        # may change.
        support = factories.UserFactory(is_support=True)
        assign_global_role(support, PermissionEnum.UPDATE_PROJECT)
        self.client.force_authenticate(support)
        with freeze_time("2020-01-01"):
            response = self.client.patch(
                factories.ProjectFactory.get_url(self.project),
                {"end_date": "2021-01-01"},
            )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_support_user_with_both_permissions_can_set_a_project_end_date(self):
        support = factories.UserFactory(is_support=True)
        assign_global_role(
            support, PermissionEnum.UPDATE_PROJECT, PermissionEnum.DELETE_PROJECT
        )
        self.client.force_authenticate(support)
        with freeze_time("2020-01-01"):
            response = self.client.patch(
                factories.ProjectFactory.get_url(self.project),
                {"end_date": "2021-01-01"},
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.project.refresh_from_db()
        self.assertEqual(self.project.end_date, datetime.date(2021, 1, 1))

    def test_support_user_without_the_permission_cannot_set_a_project_end_date(self):
        support = factories.UserFactory(is_support=True)
        self.client.force_authenticate(support)
        with freeze_time("2020-01-01"):
            response = self.client.patch(
                factories.ProjectFactory.get_url(self.project),
                {"end_date": "2021-01-01"},
            )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.end_date)

    def test_update_project_without_list_does_not_open_the_project(self):
        # PROJECT.UPDATE is the write check. The list stays closed without
        # PROJECT.LIST, the same as a customer role that can update but not list.
        user = factories.UserFactory()
        assign_global_role(
            user, PermissionEnum.UPDATE_PROJECT, role_name="GLOBAL.WRITER"
        )
        self.client.force_authenticate(user)
        response = self.client.patch(
            factories.ProjectFactory.get_url(self.project),
            {"name": "Should stay hidden"},
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.project.refresh_from_db()
        self.assertNotEqual(self.project.name, "Should stay hidden")

    def test_an_unrelated_permission_does_not_open_the_project(self):
        user = factories.UserFactory()
        assign_global_role(
            user, PermissionEnum.UPDATE_OFFERING, role_name="GLOBAL.OFFERING_WRITER"
        )
        self.client.force_authenticate(user)
        response = self.client.patch(
            factories.ProjectFactory.get_url(self.project),
            {"name": "Should stay hidden"},
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_owner_cannot_grant_the_role_on_their_organization(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        role = assign_global_role(
            factories.UserFactory(),
            PermissionEnum.UPDATE_PROJECT,
            role_name="GLOBAL.UNGRANTABLE",
        )
        response = client_add_user(
            self.client,
            self.fixture.owner,
            factories.UserFactory(),
            self.fixture.customer,
            role,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not valid for this scope", str(response.data))

    def test_global_role_is_absent_from_the_organization_picker(self):
        role = assign_global_role(
            factories.UserFactory(),
            PermissionEnum.UPDATE_PROJECT,
            role_name="GLOBAL.HIDDEN",
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            reverse("role-list"),
            {
                "available_for_customer": self.fixture.customer.uuid.hex,
                "page_size": 200,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn(role.uuid.hex, {row["uuid"] for row in response.data})


class GlobalScopeHygieneTest(test.APITestCase):
    def test_project_permission_on_a_global_role_is_not_inert(self):
        content_type = ContentType.objects.get_by_natural_key(
            *("permissions", "globalrolescope")
        )
        role = models.Role.objects.create(
            name="GLOBAL.HELPER",
            content_type=content_type,
            is_system_role=False,
        )
        role.add_permission(PermissionEnum.UPDATE_PROJECT)
        checks = {
            finding.check
            for finding in hygiene.collect_findings()
            if finding.role_name == role.name
        }
        self.assertNotIn("cross-scope-permission", checks)
        self.assertNotIn("global-custom-role", checks)

    def test_name_claiming_another_scope_is_reported(self):
        content_type = ContentType.objects.get_by_natural_key(
            *("permissions", "globalrolescope")
        )
        role = models.Role.objects.create(
            name="PROJECT.HELPER",
            content_type=content_type,
            is_system_role=False,
        )
        checks = {
            finding.check
            for finding in hygiene.collect_findings()
            if finding.role_name == role.name
        }
        self.assertIn("scope-prefix-mismatch", checks)


# permission, attribute on the scope bundle, content type of the objects the
# permission is exercised against. One global role carries all of them.
_SCOPE_CASES = (
    (PermissionEnum.UPDATE_CUSTOMER, "customers", ("structure", "customer")),
    (PermissionEnum.LIST_CUSTOMER_USERS, "customers", ("structure", "customer")),
    (PermissionEnum.CREATE_CUSTOMER_PERMISSION, "customers", ("structure", "customer")),
    (PermissionEnum.DELETE_CUSTOMER_PERMISSION, "customers", ("structure", "customer")),
    (PermissionEnum.CREATE_ACCESS_SUBNET, "customers", ("structure", "customer")),
    (PermissionEnum.DELETE_ACCESS_SUBNET, "customers", ("structure", "customer")),
    (PermissionEnum.LIST_INVITATIONS, "customers", ("structure", "customer")),
    (PermissionEnum.CREATE_LEXIS_LINK, "customers", ("structure", "customer")),
    (PermissionEnum.MANAGE_POSIX_ID_POOL, "customers", ("structure", "customer")),
    (PermissionEnum.CREATE_PROJECT, "customers", ("structure", "customer")),
    (PermissionEnum.LIST_PROJECTS, "projects", ("structure", "project")),
    (PermissionEnum.UPDATE_PROJECT, "projects", ("structure", "project")),
    (PermissionEnum.DELETE_PROJECT, "projects", ("structure", "project")),
    (PermissionEnum.CREATE_PROJECT_PERMISSION, "projects", ("structure", "project")),
    (PermissionEnum.MANAGE_COURSE_ACCOUNT, "projects", ("structure", "project")),
    (
        PermissionEnum.CAN_MANAGE_OPENSTACK_INSTANCE,
        "projects",
        ("structure", "project"),
    ),
    (PermissionEnum.LIST_ORDERS, "projects", ("structure", "project")),
    (PermissionEnum.CREATE_ORDER, "projects", ("structure", "project")),
    (PermissionEnum.APPROVE_ORDER, "projects", ("structure", "project")),
    (PermissionEnum.REJECT_ORDER, "projects", ("structure", "project")),
    (PermissionEnum.CANCEL_ORDER, "projects", ("structure", "project")),
    (PermissionEnum.DESTROY_ORDER, "projects", ("structure", "project")),
    (PermissionEnum.MANAGE_SERVICE_ACCOUNT, "projects", ("structure", "project")),
    (PermissionEnum.LIST_OFFERINGS, "offerings", ("marketplace", "offering")),
    (PermissionEnum.UPDATE_OFFERING, "offerings", ("marketplace", "offering")),
    (PermissionEnum.DELETE_OFFERING, "offerings", ("marketplace", "offering")),
    (PermissionEnum.PAUSE_OFFERING, "offerings", ("marketplace", "offering")),
    (PermissionEnum.ADD_OFFERING_ENDPOINT, "offerings", ("marketplace", "offering")),
    (
        PermissionEnum.CREATE_OFFERING_PERMISSION,
        "offerings",
        ("marketplace", "offering"),
    ),
    (PermissionEnum.LIST_RESOURCES, "resources", ("marketplace", "resource")),
    (PermissionEnum.UPDATE_RESOURCE, "resources", ("marketplace", "resource")),
    (PermissionEnum.TERMINATE_RESOURCE, "resources", ("marketplace", "resource")),
    (
        PermissionEnum.CREATE_RESOURCE_PERMISSION,
        "resources",
        ("marketplace", "resource"),
    ),
    (PermissionEnum.REJECT_BOOKING_REQUEST, "resources", ("marketplace", "resource")),
    (PermissionEnum.MANAGE_RESOURCE_USERS, "resources", ("marketplace", "resource")),
    (
        PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMERS,
        "service_providers",
        ("marketplace", "serviceprovider"),
    ),
    (
        PermissionEnum.MANAGE_MAINTENANCE_ANNOUNCEMENT,
        "service_providers",
        ("marketplace", "serviceprovider"),
    ),
    (PermissionEnum.LIST_CALLS, "calls", ("proposal", "call")),
    (PermissionEnum.CREATE_CALL, "calls", ("proposal", "call")),
    (PermissionEnum.UPDATE_CALL, "calls", ("proposal", "call")),
    (PermissionEnum.CREATE_CALL_PERMISSION, "calls", ("proposal", "call")),
    (PermissionEnum.APPROVE_AND_REJECT_PROPOSALS, "calls", ("proposal", "call")),
    (PermissionEnum.CLOSE_ROUNDS, "calls", ("proposal", "call")),
    (PermissionEnum.LIST_ROUNDS, "rounds", ("proposal", "round")),
    (PermissionEnum.LIST_PROPOSALS, "proposals", ("proposal", "proposal")),
    (PermissionEnum.UPDATE_PROPOSAL, "proposals", ("proposal", "proposal")),
    (PermissionEnum.MANAGE_PROPOSAL, "proposals", ("proposal", "proposal")),
    (PermissionEnum.MANAGE_PROPOSAL_REVIEW, "proposals", ("proposal", "proposal")),
)


class GlobalScopePermissionMatrixTest(test.APITestCase):
    """Every kind of permission on the global scope covers every object.

    List, create, update, delete, approve, reject and manage are the same
    mechanism: the grant is not membership, and a permission the role does
    not carry stays denied. Staff and support flags stay off. Organization team
    management is the exception: see GlobalScopeDenylistTest.
    """

    def test_each_permission_applies_to_every_object_of_its_target(self):
        user = factories.UserFactory()
        scopes = {
            "customers": [factories.CustomerFactory(), factories.CustomerFactory()],
            "projects": [factories.ProjectFactory(), factories.ProjectFactory()],
            "offerings": [
                marketplace_factories.OfferingFactory(),
                marketplace_factories.OfferingFactory(),
            ],
            "resources": [
                marketplace_factories.ResourceFactory(),
                marketplace_factories.ResourceFactory(),
            ],
            "service_providers": [
                marketplace_factories.ServiceProviderFactory(),
                marketplace_factories.ServiceProviderFactory(),
            ],
            "calls": [
                proposal_factories.CallFactory(),
                proposal_factories.CallFactory(),
            ],
            "rounds": [
                proposal_factories.RoundFactory(),
                proposal_factories.RoundFactory(),
            ],
            "proposals": [
                proposal_factories.ProposalFactory(),
                proposal_factories.ProposalFactory(),
            ],
        }
        assign_global_role(
            user,
            *(permission for permission, _, _ in _SCOPE_CASES),
            role_name="GLOBAL.MATRIX",
        )
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_support)
        self.assertEqual(list(get_connected_customers(user)), [])

        for permission, scope_key, content_type_key in _SCOPE_CASES:
            objects = scopes[scope_key]
            if permission in enums.GLOBAL_DENIED_PERMISSIONS:
                # Organization team management never applies from this scope.
                with self.subTest(permission=permission.value, denied=True):
                    for obj in objects:
                        self.assertFalse(utils.has_permission(user, permission, obj))
                continue
            with self.subTest(permission=permission.value):
                for obj in objects:
                    self.assertTrue(utils.has_permission(user, permission, obj))
                    self.assertIn(
                        user, utils.get_users_with_permission(obj, permission)
                    )
                content_type = ContentType.objects.get_by_natural_key(*content_type_key)
                visible_ids = set(
                    utils.get_scope_ids(user, content_type, permission=permission)
                )
                self.assertTrue({obj.id for obj in objects}.issubset(visible_ids))

        for permission in (
            PermissionEnum.SET_PROJECT_SLUG,
            PermissionEnum.STAFF_ACCESS,
            PermissionEnum.SUPPORT_ACCESS,
        ):
            with self.subTest(permission=permission.value, granted=False):
                self.assertFalse(
                    utils.has_permission(user, permission, scopes["projects"][0])
                )

    def test_a_single_permission_does_not_open_another(self):
        user = factories.UserFactory()
        project = factories.ProjectFactory()
        offering = marketplace_factories.OfferingFactory()
        assign_global_role(
            user, PermissionEnum.UPDATE_OFFERING, role_name="GLOBAL.OFFERING_ONLY"
        )
        self.assertTrue(
            utils.has_permission(user, PermissionEnum.UPDATE_OFFERING, offering)
        )
        self.assertFalse(
            utils.has_permission(user, PermissionEnum.DELETE_OFFERING, offering)
        )
        self.assertEqual(
            list(
                get_connected_projects_by_permission(user, PermissionEnum.LIST_PROJECTS)
            ),
            [],
        )
        self.assertFalse(
            utils.has_permission(user, PermissionEnum.UPDATE_PROJECT, project)
        )


class GlobalUserRoleApiTest(test.APITestCase):
    def setUp(self):
        self.staff = factories.UserFactory(is_staff=True)
        self.holder = factories.UserFactory()
        self.role = GlobalRole.get("GLOBAL.ORDER_DESK")
        self.role.add_permission(PermissionEnum.LIST_ORDERS)
        self.list_url = reverse("global-user-role-list")
        self.client.force_authenticate(self.staff)

    def grant(self, user=None, role=None, **extra):
        role = self.role if role is None else role
        payload = {
            "user": (user or self.holder).uuid.hex,
            "role": role if isinstance(role, str) else role.uuid.hex,
        }
        payload.update(extra)
        return self.client.post(self.list_url, payload)

    def test_staff_can_grant_and_list_a_site_role(self):
        response = self.grant()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["user_uuid"], self.holder.uuid.hex)
        self.assertEqual(response.data["role_name"], self.role.name)
        self.assertEqual(response.data["created_by_uuid"], self.staff.uuid.hex)
        self.assertTrue(
            utils.has_permission(
                self.holder,
                PermissionEnum.LIST_ORDERS,
                factories.ProjectFactory(),
            )
        )

        listed = self.client.get(self.list_url)
        self.assertEqual(listed.status_code, status.HTTP_200_OK, listed.data)
        self.assertEqual(
            {row["uuid"] for row in listed.data},
            {response.data["uuid"]},
        )

    def test_staff_can_grant_a_second_site_role_and_revoke_the_first(self):
        other = GlobalRole.get("GLOBAL.RESOURCE_OPS")
        other.add_permission(PermissionEnum.LIST_RESOURCES)
        first = self.grant()
        second = self.grant(role=other)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED, second.data)

        response = self.client.delete(
            reverse(
                "global-user-role-detail",
                kwargs={"uuid": first.data["uuid"]},
            )
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        grant = models.UserRole.objects.get(uuid=first.data["uuid"])
        self.assertFalse(grant.is_active)
        self.assertFalse(
            utils.has_permission(
                self.holder,
                PermissionEnum.LIST_ORDERS,
                factories.ProjectFactory(),
            )
        )
        self.assertTrue(
            utils.has_permission(
                self.holder,
                PermissionEnum.LIST_RESOURCES,
                factories.ProjectFactory(),
            )
        )
        listed = self.client.get(self.list_url)
        self.assertEqual(
            {row["uuid"] for row in listed.data},
            {second.data["uuid"]},
        )

    def test_grant_accepts_the_role_name_and_an_expiration(self):
        expires = timezone.now() + timedelta(days=30)
        response = self.grant(
            role=self.role.name, expiration_time=expires.date().isoformat()
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNotNone(response.data["expiration_time"])

    def test_anonymous_user_is_rejected(self):
        self.client.force_authenticate(None)
        self.assertEqual(
            self.client.get(self.list_url).status_code, status.HTTP_401_UNAUTHORIZED
        )
        self.assertEqual(self.grant().status_code, status.HTTP_401_UNAUTHORIZED)

    def test_regular_user_and_support_are_rejected(self):
        for user in (
            factories.UserFactory(),
            factories.UserFactory(is_support=True),
        ):
            self.client.force_authenticate(user)
            self.assertEqual(
                self.client.get(self.list_url).status_code,
                status.HTTP_403_FORBIDDEN,
            )
            self.assertEqual(self.grant().status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(
                self.client.delete(
                    reverse(
                        "global-user-role-detail",
                        kwargs={"uuid": factories.UserFactory().uuid.hex},
                    )
                ).status_code,
                status.HTTP_403_FORBIDDEN,
            )

    def test_staff_token_without_the_staff_scope_is_rejected(self):
        self.client.force_authenticate(
            self.staff,
            token=PersonalAccessToken(scopes=[PermissionEnum.LIST_ORDERS.value]),
        )
        self.assertEqual(self.grant().status_code, status.HTTP_403_FORBIDDEN)

    def test_organization_role_is_rejected(self):
        response = self.grant(role=CustomerRole.OWNER)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("site-wide", str(response.data))

    def test_unknown_role_and_user_are_rejected(self):
        response = self.grant(role="GLOBAL.DOES_NOT_EXIST")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not found", str(response.data).lower())
        response = self.client.post(
            self.list_url,
            {
                "user": "00000000000000000000000000000000",
                "role": self.role.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not found", str(response.data).lower())

    def test_inactive_role_and_inactive_user_are_rejected(self):
        self.role.is_active = False
        self.role.save(update_fields=["is_active"])
        response = self.grant()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not active", str(response.data).lower())

        self.role.is_active = True
        self.role.save(update_fields=["is_active"])
        self.holder.is_active = False
        self.holder.save(update_fields=["is_active"])
        response = self.grant()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not active", str(response.data).lower())

    def test_duplicate_grant_is_rejected(self):
        self.assertEqual(self.grant().status_code, status.HTTP_201_CREATED)
        response = self.grant()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already", str(response.data).lower())

    def test_expiration_in_the_past_is_rejected(self):
        response = self.grant(
            expiration_time=(timezone.now() - timedelta(days=1)).date().isoformat()
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_project_grant_cannot_be_revoked_here(self):
        member = factories.UserFactory()
        grant = fixtures.ProjectFixture().project.add_user(member, ProjectRole.ADMIN)
        response = self.client.delete(
            reverse("global-user-role-detail", kwargs={"uuid": grant.uuid.hex})
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        grant.refresh_from_db()
        self.assertTrue(grant.is_active)


class GlobalScopeDenylistTest(test.APITestCase):
    """A site-wide role must not grant roles.

    A permission that grants, changes or revokes roles, held on every object,
    lets its holder make themselves owner of any organization. The role is then
    no narrower than staff, which is what the scope exists to avoid.
    """

    def setUp(self):
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)
        self.user = factories.UserFactory(is_support=True)
        # Written straight to the database, the way an admin edit or an old
        # role definition would carry it past the definition checks below.
        self.role = assign_global_role(
            self.user,
            PermissionEnum.CREATE_CUSTOMER_PERMISSION,
            PermissionEnum.UPDATE_PROJECT,
        )

    def test_organization_team_management_is_denied(self):
        self.assertEqual(
            enums.GLOBAL_DENIED_PERMISSIONS,
            {
                PermissionEnum.CREATE_CUSTOMER_PERMISSION,
                PermissionEnum.UPDATE_CUSTOMER_PERMISSION,
                PermissionEnum.DELETE_CUSTOMER_PERMISSION,
            },
        )

    def test_team_management_below_the_organization_stays_grantable(self):
        # #762: a site-wide desk may invite people into any project.
        for permission in (
            PermissionEnum.CREATE_PROJECT_PERMISSION,
            PermissionEnum.CREATE_OFFERING_PERMISSION,
            PermissionEnum.CREATE_CALL_PERMISSION,
        ):
            self.assertNotIn(permission, enums.GLOBAL_DENIED_PERMISSIONS)

    def test_a_denied_permission_is_inert_on_the_site_scope(self):
        self.assertFalse(
            utils.has_permission(
                self.user, PermissionEnum.CREATE_CUSTOMER_PERMISSION, self.customer
            )
        )
        self.assertFalse(
            utils.holds_global_permission(
                self.user, PermissionEnum.CREATE_CUSTOMER_PERMISSION
            )
        )
        self.assertNotIn(
            self.user,
            utils.get_users_with_permission(
                self.customer, PermissionEnum.CREATE_CUSTOMER_PERMISSION
            ),
        )
        index = utils.build_role_index(
            self.user, [(PermissionEnum.CREATE_CUSTOMER_PERMISSION, self.customer)]
        )
        self.assertFalse(
            utils.has_permission(
                self.user,
                PermissionEnum.CREATE_CUSTOMER_PERMISSION,
                self.customer,
                index,
            )
        )

    def test_the_rest_of_the_role_still_applies(self):
        self.assertTrue(
            utils.has_permission(self.user, PermissionEnum.UPDATE_PROJECT, self.project)
        )

    def test_holder_cannot_make_themselves_owner_of_an_organization(self):
        response = client_add_user(
            self.client, self.user, self.user, self.customer, CustomerRole.OWNER
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(self.customer.has_user(self.user, CustomerRole.OWNER))


class GlobalRoleDefinitionTest(test.APITestCase):
    def setUp(self):
        self.staff = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)
        self.url = reverse("role-list")

    def create(self, content_type, *permissions, name="GLOBAL.DESK"):
        return self.client.post(
            self.url,
            {
                "name": name,
                "description": "Desk",
                "content_type": content_type,
                "permissions": list(permissions),
            },
            format="json",
        )

    def test_a_global_role_may_not_carry_a_role_granting_permission(self):
        response = self.create(
            "global",
            PermissionEnum.UPDATE_PROJECT,
            PermissionEnum.CREATE_CUSTOMER_PERMISSION,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(PermissionEnum.CREATE_CUSTOMER_PERMISSION, str(response.data))
        self.assertFalse(models.Role.objects.filter(name="GLOBAL.DESK").exists())

    def test_the_permission_cannot_be_added_later(self):
        response = self.create("global", PermissionEnum.UPDATE_PROJECT)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        response = self.client.put(
            reverse("role-detail", kwargs={"uuid": response.data["uuid"]}),
            {
                "name": "GLOBAL.DESK",
                "description": "Desk",
                "content_type": "global",
                "permissions": [
                    PermissionEnum.UPDATE_PROJECT,
                    PermissionEnum.DELETE_CUSTOMER_PERMISSION,
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_organization_role_may_still_carry_it(self):
        response = self.create(
            "customer",
            PermissionEnum.CREATE_CUSTOMER_PERMISSION,
            name="CUSTOMER.TEAM_ADMIN",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_import_roles_drops_a_denied_permission(self):
        roles_file = Path(tempfile.mkdtemp()) / "custom-roles.yaml"
        roles_file.write_text(
            "- role: GLOBAL.IMPORTED\n"
            "  description: Imported desk\n"
            "  scope: global\n"
            "  permissions:\n"
            "    - PROJECT.UPDATE\n"
            "    - CUSTOMER.CREATE_PERMISSION\n"
        )
        out = io.StringIO()
        call_command("import_roles", str(roles_file), stdout=out)
        role = models.Role.objects.get(name="GLOBAL.IMPORTED")
        self.assertEqual(
            set(role.permissions.values_list("permission", flat=True)),
            {PermissionEnum.UPDATE_PROJECT},
        )
        self.assertIn("CUSTOMER.CREATE_PERMISSION", out.getvalue())

    def test_hygiene_reports_a_denied_permission(self):
        role = GlobalRole.get("GLOBAL.FORCED")
        role.add_permission(PermissionEnum.UPDATE_CUSTOMER_PERMISSION)
        checks = {
            finding.check
            for finding in hygiene.collect_findings()
            if finding.role_name == "GLOBAL.FORCED"
        }
        self.assertIn("global-denied-permission", checks)


class GlobalScopeCostTest(test.APITestCase):
    """What the global scope costs a deployment, in queries.

    A deployment that never grants a global role pays nothing. One that does
    pays one query per request, however many checks the request makes.
    """

    def setUp(self):
        global_scope.reset()
        self.fixture = fixtures.ProjectFixture()
        self.user = self.fixture.admin
        self.project = self.fixture.project
        # Role permissions come from permissions.yaml, which tests do not load.
        ProjectRole.ADMIN.add_permission(PermissionEnum.UPDATE_PROJECT)

    def tearDown(self):
        global_scope.reset()

    def list_queries(self):
        self.client.force_authenticate(self.user)
        url = factories.ProjectFactory.get_list_url()
        self.client.get(url)  # warm caches unrelated to this change
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return len(ctx.captured_queries)

    def test_without_a_site_scope_a_check_costs_no_extra_query(self):
        self.assertFalse(models.GlobalRoleScope.objects.exists())
        global_scope.scope_may_exist()  # the once-a-minute probe
        with self.assertNumQueries(0):
            self.assertFalse(
                utils.holds_global_permission(self.user, PermissionEnum.LIST_ORDERS)
            )
        # The same single query the check made before the global scope existed.
        with self.assertNumQueries(1):
            self.assertTrue(
                utils.has_permission(
                    self.user, PermissionEnum.UPDATE_PROJECT, self.project
                )
            )

    def test_a_site_scope_costs_one_query_per_request(self):
        without_scope = self.list_queries()
        assign_global_role(factories.UserFactory(), PermissionEnum.LIST_PROJECTS)
        self.assertEqual(self.list_queries(), without_scope + 1)

    def test_a_holder_also_pays_one_query_per_request(self):
        without_scope = self.list_queries()
        assign_global_role(self.user, PermissionEnum.UPDATE_PROJECT)
        self.assertEqual(self.list_queries(), without_scope + 1)

    def test_a_scope_created_elsewhere_applies_after_the_recheck(self):
        # This process has looked and found no site scope.
        self.assertFalse(global_scope.scope_may_exist())
        # Another process creates it and grants a role. Nothing here hears it.
        role = models.Role.objects.create(
            name="GLOBAL.ELSEWHERE",
            content_type=ContentType.objects.get_by_natural_key(
                "permissions", "globalrolescope"
            ),
        )
        models.RolePermission.objects.bulk_create(
            [models.RolePermission(role=role, permission=PermissionEnum.UPDATE_PROJECT)]
        )
        models.GlobalRoleScope.objects.bulk_create([models.GlobalRoleScope()])
        scope = models.GlobalRoleScope.objects.get()
        models.UserRole.objects.bulk_create(
            [
                models.UserRole(
                    user=self.fixture.user,
                    role=role,
                    content_type=ContentType.objects.get_for_model(scope),
                    object_id=scope.id,
                    is_active=True,
                )
            ]
        )
        other = factories.ProjectFactory()
        # Fails closed until the next probe...
        self.assertFalse(
            utils.has_permission(
                self.fixture.user, PermissionEnum.UPDATE_PROJECT, other
            )
        )
        later = global_scope.SCOPE_RECHECK_SECONDS + 1
        with mock.patch.object(
            global_scope, "_now", return_value=global_scope._now() + later
        ):
            self.assertTrue(
                utils.has_permission(
                    self.fixture.user, PermissionEnum.UPDATE_PROJECT, other
                )
            )


class GlobalScopeRequestMemoTest(test.APITestCase):
    """The per-request memo never outlives the request that filled it."""

    def setUp(self):
        global_scope.reset()
        self.user = factories.UserFactory()
        factories.ProjectFactory()
        self.role = GlobalRole.get("GLOBAL.LISTER")
        self.role.add_permission(PermissionEnum.LIST_PROJECTS)
        utils.get_global_role_scope()
        self.client.force_authenticate(self.user)

    def tearDown(self):
        global_scope.reset()

    def count(self):
        response = self.client.get(factories.ProjectFactory.get_list_url())
        return len(response.data)

    def test_a_grant_between_requests_applies_on_the_next_one(self):
        self.assertEqual(self.count(), 0)
        grant = utils.grant_global_role(self.user, self.role)
        self.assertEqual(self.count(), 1)
        grant.revoke(self.user, reason="test")
        self.assertEqual(self.count(), 0)

    def test_a_grant_inside_a_request_drops_that_users_memo(self):
        global_scope.start_request()
        try:
            self.assertFalse(
                utils.holds_global_permission(self.user, PermissionEnum.LIST_PROJECTS)
            )
            utils.grant_global_role(self.user, self.role)
            self.assertTrue(
                utils.holds_global_permission(self.user, PermissionEnum.LIST_PROJECTS)
            )
        finally:
            global_scope.end_request()


class GlobalScopeBindingTest(test.APITestCase):
    """The site scope is granted on, never bound to or invited into."""

    def test_valid_scope_models_leave_out_the_site_scope(self):
        self.assertNotIn(models.GlobalRoleScope, utils.get_valid_models())

    def test_the_site_scope_is_not_a_bindable_type(self):
        self.assertNotIn("global", enums.BINDABLE_TYPE_MAP)
        self.assertIn("global", enums.TYPE_MAP)
