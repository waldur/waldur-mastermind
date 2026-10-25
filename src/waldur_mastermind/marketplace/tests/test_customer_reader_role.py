"""Organization readers see, but cannot change, their organization.

CUSTOMER.READER used to ship with CUSTOMER.VIEW_TEAM only, so a reader could
not list the organization's users, projects, resources, orders or
invitations.
"""

import io
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.urls import reverse
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.enums import ReviewStates
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, GlobalRole, ProjectRole
from waldur_core.permissions.tests.test_system_role_descriptions import (
    permissions_yaml_rows,
)
from waldur_core.permissions.utils import grant_global_role
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.utils import client_add_user, client_list_users
from waldur_core.users.tests import factories as users_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import OfferingStates, OrderStates
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


class GlobalRoleAccessTest(test.APITestCase):
    """A site-wide role carries whichever permissions a deployment gives it.

    LIST_* on that scope lists every row, not the rows of one organization.
    A different permission does not open the list.
    """

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(self.user)

    def assign(self, role_name, *permissions):
        role = GlobalRole.get(role_name)
        for permission in permissions:
            role.add_permission(permission)
        grant_global_role(self.user, role)
        return role

    def list_uuids(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {item["uuid"] for item in response.data}

    def test_list_orders_shows_every_order(self):
        first = factories.OrderFactory()
        second = factories.OrderFactory()
        self.assign("GLOBAL.ORDER_READER", PermissionEnum.LIST_ORDERS)
        self.assertEqual(
            self.list_uuids(factories.OrderFactory.get_list_url()),
            {first.uuid.hex, second.uuid.hex},
        )

    def test_approve_order_does_not_open_the_order_list(self):
        factories.OrderFactory()
        factories.OrderFactory()
        self.assign("GLOBAL.ORDER_APPROVER", PermissionEnum.APPROVE_ORDER)
        self.assertEqual(self.list_uuids(factories.OrderFactory.get_list_url()), set())

    def test_approve_order_does_not_open_projects_or_offerings(self):
        project = structure_factories.ProjectFactory()
        offering = factories.OfferingFactory()
        self.assign("GLOBAL.ORDER_APPROVER_SCOPE", PermissionEnum.APPROVE_ORDER)
        self.assertNotIn(
            project.uuid.hex,
            self.list_uuids(structure_factories.ProjectFactory.get_list_url()),
        )
        self.assertNotIn(
            offering.uuid.hex,
            self.list_uuids(factories.OfferingFactory.get_list_url()),
        )

    def test_list_orders_does_not_open_projects(self):
        project = structure_factories.ProjectFactory()
        self.assign("GLOBAL.ORDER_READER_SCOPE", PermissionEnum.LIST_ORDERS)
        self.assertNotIn(
            project.uuid.hex,
            self.list_uuids(structure_factories.ProjectFactory.get_list_url()),
        )

    def test_list_resources_shows_every_resource(self):
        first = factories.ResourceFactory()
        second = factories.ResourceFactory()
        self.assign("GLOBAL.RESOURCE_READER", PermissionEnum.LIST_RESOURCES)
        self.assertEqual(
            self.list_uuids(factories.ResourceFactory.get_list_url()),
            {first.uuid.hex, second.uuid.hex},
        )

    def test_list_projects_shows_every_project(self):
        first = structure_factories.ProjectFactory()
        second = structure_factories.ProjectFactory()
        self.assertEqual(
            self.list_uuids(structure_factories.ProjectFactory.get_list_url()),
            set(),
        )
        self.assign("GLOBAL.PROJECT_READER", PermissionEnum.LIST_PROJECTS)
        self.assertEqual(
            self.list_uuids(structure_factories.ProjectFactory.get_list_url()),
            {first.uuid.hex, second.uuid.hex},
        )

    def test_list_projects_and_update_can_rename_a_project_without_membership(self):
        project = structure_factories.ProjectFactory()
        self.assign(
            "GLOBAL.PROJECT_EDITOR",
            PermissionEnum.LIST_PROJECTS,
            PermissionEnum.UPDATE_PROJECT,
        )
        response = self.client.patch(
            structure_factories.ProjectFactory.get_url(project),
            {"name": "Renamed from the site role"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        project.refresh_from_db()
        self.assertEqual(project.name, "Renamed from the site role")

    def test_manage_service_account_lists_every_account(self):
        first = factories.ProjectServiceAccountFactory(username="global-account-1")
        second = factories.ProjectServiceAccountFactory(username="global-account-2")
        self.assign(
            "GLOBAL.SERVICE_ACCOUNT_MANAGER", PermissionEnum.MANAGE_SERVICE_ACCOUNT
        )
        self.assertEqual(
            self.list_uuids(factories.ProjectServiceAccountFactory.get_list_url()),
            {first.uuid.hex, second.uuid.hex},
        )

    def test_update_resource_limits_sees_every_request(self):
        change = factories.ResourceLimitChangeRequestFactory(state=ReviewStates.PENDING)
        self.assign(
            "GLOBAL.LIMIT_REVIEWER",
            PermissionEnum.UPDATE_RESOURCE_LIMITS,
        )
        self.assertIn(
            change.uuid.hex,
            self.list_uuids(factories.ResourceLimitChangeRequestFactory.get_list_url()),
        )
        response = self.client.post(
            factories.ResourceLimitChangeRequestFactory.get_url(change, "reject"),
            {"comment": "Not now"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        change.refresh_from_db()
        self.assertEqual(change.state, ReviewStates.REJECTED)

    def test_list_and_approve_order_sees_every_pending_consumer_order(self):
        first = factories.OrderFactory(state=OrderStates.PENDING_CONSUMER)
        second = factories.OrderFactory(state=OrderStates.PENDING_CONSUMER)
        unrelated = factories.OrderFactory(state=OrderStates.PENDING_PROVIDER)
        self.assign(
            "GLOBAL.ORDER_APPROVER",
            PermissionEnum.LIST_ORDERS,
            PermissionEnum.APPROVE_ORDER,
        )
        response = self.client.get(
            factories.OrderFactory.get_list_url(),
            {"can_approve_as_consumer": True},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            {item["uuid"] for item in response.data},
            {first.uuid.hex, second.uuid.hex},
        )
        self.assertNotIn(unrelated.uuid.hex, {item["uuid"] for item in response.data})

    def test_shipped_offering_roles_can_list_offerings(self):
        rows_source = permissions_yaml_rows()
        self.assertIsNotNone(rows_source)
        rows = {row["role"]: set(row["permissions"]) for row in rows_source}
        for role_name in ("CUSTOMER.OWNER", "CUSTOMER.MANAGER", "OFFERING.MANAGER"):
            self.assertIn(PermissionEnum.LIST_OFFERINGS.value, rows[role_name])

    def test_list_offerings_shows_every_offering_on_the_management_list(self):
        draft = factories.OfferingFactory(state=OfferingStates.DRAFT)
        active = factories.OfferingFactory(state=OfferingStates.ACTIVE, shared=True)
        self.assign("GLOBAL.OFFERING_READER", PermissionEnum.LIST_OFFERINGS)
        visible = self.list_uuids(factories.OfferingFactory.get_list_url())
        self.assertIn(draft.uuid.hex, visible)
        self.assertIn(active.uuid.hex, visible)

    def test_list_offerings_does_not_open_the_public_catalog(self):
        draft = factories.OfferingFactory(state=OfferingStates.DRAFT, shared=True)
        self.assign("GLOBAL.OFFERING_CATALOG", PermissionEnum.LIST_OFFERINGS)
        self.assertNotIn(
            draft.uuid.hex,
            self.list_uuids(factories.OfferingFactory.get_public_list_url()),
        )

    def test_list_and_update_offering_can_rename_a_draft(self):
        offering = factories.OfferingFactory(state=OfferingStates.DRAFT)
        factories.ServiceProviderFactory(customer=offering.customer)
        self.assign(
            "GLOBAL.OFFERING_EDITOR_WITH_LIST",
            PermissionEnum.LIST_OFFERINGS,
            PermissionEnum.UPDATE_OFFERING,
        )
        response = self.client.post(
            factories.OfferingFactory.get_url(offering, "update_overview"),
            {"name": "Renamed offering"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        offering.refresh_from_db()
        self.assertEqual(offering.name, "Renamed offering")

    def test_update_offering_without_support_does_not_open_the_offering(self):
        offering = factories.OfferingFactory(state=OfferingStates.DRAFT)
        factories.ServiceProviderFactory(customer=offering.customer)
        self.assign("GLOBAL.OFFERING_EDITOR", PermissionEnum.UPDATE_OFFERING)
        response = self.client.post(
            factories.OfferingFactory.get_url(offering, "update_overview"),
            {"name": "Renamed offering"},
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        offering.refresh_from_db()
        self.assertNotEqual(offering.name, "Renamed offering")

    def test_support_with_update_offering_can_rename_it(self):
        # Offerings have no list permission. Support remains the read that
        # loads the row; the global grant is only the write.
        offering = factories.OfferingFactory(state=OfferingStates.DRAFT)
        factories.ServiceProviderFactory(customer=offering.customer)
        support = structure_factories.UserFactory(is_support=True)
        role = GlobalRole.get("GLOBAL.OFFERING_SUPPORT_EDITOR")
        role.add_permission(PermissionEnum.UPDATE_OFFERING)
        grant_global_role(support, role)
        self.client.force_authenticate(support)
        response = self.client.post(
            factories.OfferingFactory.get_url(offering, "update_overview"),
            {"name": "Renamed offering"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        offering.refresh_from_db()
        self.assertEqual(offering.name, "Renamed offering")

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    def test_delete_offering_without_support_does_not_open_the_offering(self):
        offering = factories.OfferingFactory(state=OfferingStates.DRAFT)
        self.assign("GLOBAL.OFFERING_REMOVER", PermissionEnum.DELETE_OFFERING)
        response = self.client.delete(factories.OfferingFactory.get_url(offering))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    def test_support_with_delete_offering_can_delete_it(self):
        offering = factories.OfferingFactory(state=OfferingStates.DRAFT)
        support = structure_factories.UserFactory(is_support=True)
        role = GlobalRole.get("GLOBAL.OFFERING_SUPPORT_REMOVER")
        role.add_permission(PermissionEnum.DELETE_OFFERING)
        grant_global_role(support, role)
        self.client.force_authenticate(support)
        response = self.client.delete(factories.OfferingFactory.get_url(offering))
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )

    def test_update_resource_with_list_and_update(self):
        resource = factories.ResourceFactory()
        self.assign(
            "GLOBAL.RESOURCE_EDITOR",
            PermissionEnum.LIST_RESOURCES,
            PermissionEnum.UPDATE_RESOURCE,
        )
        response = self.client.patch(
            factories.ResourceFactory.get_url(resource),
            {"name": "Renamed resource"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        resource.refresh_from_db()
        self.assertEqual(resource.name, "Renamed resource")

    def test_list_resources_opens_resource_projects_on_both_apis(self):
        resource = factories.ResourceFactory()
        subproject = models.ResourceProject.objects.create(
            resource=resource, name="Subproject"
        )
        self.assign("GLOBAL.RESOURCE_READER", PermissionEnum.LIST_RESOURCES)
        expected = {subproject.uuid.hex}
        self.assertEqual(
            self.list_uuids(reverse("marketplace-resource-project-list")),
            expected,
        )
        self.assertEqual(
            self.list_uuids(reverse("marketplace-provider-resource-project-list")),
            expected,
        )

    def test_list_resources_opens_the_provider_resource_list(self):
        first = factories.ResourceFactory()
        second = factories.ResourceFactory()
        self.assign("GLOBAL.RESOURCE_READER", PermissionEnum.LIST_RESOURCES)
        self.assertEqual(
            self.list_uuids(reverse("marketplace-provider-resource-list")),
            {first.uuid.hex, second.uuid.hex},
        )

    def test_reject_order_with_list_and_reject(self):
        order = factories.OrderFactory(state=OrderStates.PENDING_CONSUMER)
        self.assign(
            "GLOBAL.ORDER_REJECTOR",
            PermissionEnum.LIST_ORDERS,
            PermissionEnum.REJECT_ORDER,
        )
        response = self.client.post(
            factories.OrderFactory.get_url(order, "reject_by_consumer")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.REJECTED)

    def test_manage_course_account_lists_every_account(self):
        first = factories.CourseAccountFactory()
        second = factories.CourseAccountFactory()
        self.assign("GLOBAL.COURSE_MANAGER", PermissionEnum.MANAGE_COURSE_ACCOUNT)
        self.assertEqual(
            self.list_uuids(factories.CourseAccountFactory.get_list_url()),
            {first.uuid.hex, second.uuid.hex},
        )

    def test_list_service_provider_customers_is_allowed(self):
        service_provider = factories.ServiceProviderFactory()
        url = reverse(
            "service-provider-customers-list",
            kwargs={"service_provider_uuid": service_provider.uuid.hex},
        )
        denied = self.client.get(url)
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)
        self.assign(
            "GLOBAL.PROVIDER_READER", PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMERS
        )
        allowed = self.client.get(url)
        self.assertEqual(allowed.status_code, status.HTTP_200_OK, allowed.data)

    def test_list_invitations_shows_every_invitation(self):
        first = users_factories.ProjectInvitationFactory(
            scope=structure_factories.ProjectFactory()
        )
        second = users_factories.ProjectInvitationFactory(
            scope=structure_factories.ProjectFactory()
        )
        self.assign("GLOBAL.INVITATION_READER", PermissionEnum.LIST_INVITATIONS)
        self.assertEqual(
            self.list_uuids(users_factories.InvitationBaseFactory.get_list_url()),
            {first.uuid.hex, second.uuid.hex},
        )
