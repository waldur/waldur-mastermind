"""Listing resources and orders costs a fixed number of queries per page.

Every row below lives in its own offering and its own project, and carries
everything the serializers render: plan components of each billing type, an
endpoint, a creation order with a support issue, a pending update order, an
offering account and an outdated Terms of Service consent for the viewer, and
component usages from this month and from a year ago. A per-row or
per-offering lookup anywhere in the serializers therefore shows up as growth
between a one-row and a four-row page.
"""

from dateutil.relativedelta import relativedelta
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core import utils as core_utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import add_user
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    BillingTypes,
    LimitPeriods,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.support.tests import factories as support_factories


@override_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
class ListQueryCountTest(test.APITestCase):
    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_ORDERS)
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_RESOURCES)
        OfferingRole.MANAGER.add_permission(PermissionEnum.LIST_ORDERS)

        self.provider = factories.ServiceProviderFactory()
        self.provider_customer = self.provider.customer
        self.consumer_customer = structure_factories.CustomerFactory()
        self.category = factories.CategoryFactory()

        self.provider_owner = structure_factories.UserFactory()
        self.provider_customer.add_user(self.provider_owner, CustomerRole.OWNER)
        self.consumer_owner = structure_factories.UserFactory()
        self.consumer_customer.add_user(self.consumer_owner, CustomerRole.OWNER)
        self.offering_manager = structure_factories.UserFactory()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.viewers = (
            self.provider_owner,
            self.consumer_owner,
            self.offering_manager,
            self.staff,
        )

    def make_row(self, pending_update=True):
        offering = factories.OfferingFactory(
            type=BASIC_OFFERING,
            customer=self.provider_customer,
            category=self.category,
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        offering.add_user(self.offering_manager, OfferingRole.MANAGER)
        plan = factories.PlanFactory(offering=offering)
        components = {}
        for component_type, billing_type, limit_period in (
            ("cpu", BillingTypes.LIMIT, LimitPeriods.MONTH),
            ("gpu", BillingTypes.LIMIT, LimitPeriods.ANNUAL),
            ("ram", BillingTypes.USAGE, None),
            ("license", BillingTypes.FIXED, None),
            ("setup", BillingTypes.ONE_TIME, None),
        ):
            components[component_type] = factories.OfferingComponentFactory(
                offering=offering,
                type=component_type,
                name=component_type,
                billing_type=billing_type,
                limit_period=limit_period,
            )
            factories.PlanComponentFactory(
                plan=plan, component=components[component_type]
            )

        project = structure_factories.ProjectFactory(customer=self.consumer_customer)
        resource = factories.ResourceFactory(
            offering=offering,
            plan=plan,
            project=project,
            state=ResourceStates.OK,
            limits={"cpu": 2},
            current_usages={"ram": 3},
        )
        this_month = core_utils.month_start(timezone.now()).date()
        for component_type, usage in (("cpu", 5), ("gpu", 7), ("ram", 11)):
            for billing_period in (this_month, this_month - relativedelta(years=1)):
                factories.ComponentUsageFactory(
                    resource=resource,
                    component=components[component_type],
                    billing_period=billing_period,
                    date=timezone.now(),
                    usage=usage,
                )
        models.ResourceAccessEndpoint.objects.create(
            resource=resource, name="console", url="https://console.example.com"
        )
        create_order = factories.OrderFactory(
            type=OrderTypes.CREATE,
            state=OrderStates.DONE,
            resource=resource,
            offering=offering,
            plan=plan,
            project=project,
            created_by=self.consumer_owner,
        )
        support_factories.IssueFactory(resource=create_order)
        if pending_update:
            factories.OrderFactory(
                type=OrderTypes.UPDATE,
                state=OrderStates.PENDING_PROVIDER,
                resource=resource,
                offering=offering,
                plan=plan,
                project=project,
                created_by=self.consumer_owner,
            )
        models.OfferingTermsOfService.objects.create(
            offering=offering,
            terms_of_service="Terms",
            version="2.0",
            is_active=True,
            requires_reconsent=True,
        )
        for user in self.viewers:
            factories.OfferingUserFactory(offering=offering, user=user)
            models.UserOfferingConsent.objects.create(
                user=user, offering=offering, version="1.0"
            )
        return resource

    def get(self, user, url, **params):
        self.client.force_authenticate(user)
        with CaptureQueriesContext(connection) as context:
            response = self.client.get(url, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.json(), len(context.captured_queries)

    def lists(self):
        resources = factories.ResourceFactory.get_provider_resource_list_url()
        consumer_resources = factories.ResourceFactory.get_list_url()
        orders = factories.OrderFactory.get_list_url()
        provider = {"provider_uuid": self.provider_customer.uuid.hex}
        return {
            "provider resources as provider owner": (
                self.provider_owner,
                resources,
                {},
            ),
            "provider resources as offering manager": (
                self.offering_manager,
                resources,
                {},
            ),
            "provider resources as staff": (self.staff, resources, {}),
            "consumer resources as consumer owner": (
                self.consumer_owner,
                consumer_resources,
                {},
            ),
            "provider orders as provider owner": (
                self.provider_owner,
                orders,
                provider,
            ),
            "provider orders as offering manager": (
                self.offering_manager,
                orders,
                provider,
            ),
        }

    def counts(self, expected_rows):
        result = {}
        for name, (user, url, params) in self.lists().items():
            # The first request warms per-process caches (content types, roles).
            self.get(user, url, **params)
            rows, count = self.get(user, url, **params)
            self.assertGreaterEqual(len(rows), expected_rows, name)
            result[name] = count
        return result

    def test_query_count_does_not_grow_with_rows(self):
        self.make_row()
        one_row = self.counts(1)
        for _ in range(3):
            self.make_row()
        four_rows = self.counts(4)
        for name, count in one_row.items():
            with self.subTest(name):
                self.assertEqual(
                    four_rows[name],
                    count,
                    f"{name}: {count} queries for one row, {four_rows[name]} for four",
                )

    def test_listed_resource_matches_its_detail_view(self):
        """The batched list and the per-object detail view render each resource
        identically, for every viewer and both workspaces."""
        resources = [self.make_row(), self.make_row(pending_update=False)]
        cases = (
            (
                self.provider_owner,
                factories.ResourceFactory.get_provider_resource_list_url(),
                factories.ResourceFactory.get_provider_resource_url,
            ),
            (
                self.offering_manager,
                factories.ResourceFactory.get_provider_resource_list_url(),
                factories.ResourceFactory.get_provider_resource_url,
            ),
            (
                self.consumer_owner,
                factories.ResourceFactory.get_list_url(),
                factories.ResourceFactory.get_url,
            ),
            (
                self.staff,
                factories.ResourceFactory.get_list_url(),
                factories.ResourceFactory.get_url,
            ),
        )
        for user, list_url, detail_url in cases:
            rows, _ = self.get(user, list_url)
            listed = {row["uuid"]: row for row in rows}
            for resource in resources:
                with self.subTest(user=user.username, url=list_url):
                    detail, _ = self.get(user, detail_url(resource))
                    self.assertEqual(listed[resource.uuid.hex], detail)

    def test_resource_scoped_viewers_see_what_the_detail_view_shows(self):
        """Viewers whose only role is on a resource or one of its resource
        projects get the restricted rendering, in the list as in detail."""
        resource_project_resource = self.make_row()
        directly_shared_resource = self.make_row(pending_update=False)

        resource_project = models.ResourceProject.objects.create(
            resource=resource_project_resource, name="Shared"
        )
        resource_project_viewer = structure_factories.UserFactory()
        add_user(
            resource_project,
            resource_project_viewer,
            Role.objects.create(
                name="shared project member",
                content_type=ContentType.objects.get_for_model(models.ResourceProject),
            ),
        )
        resource_viewer = structure_factories.UserFactory()
        add_user(
            directly_shared_resource,
            resource_viewer,
            Role.objects.create(
                name="resource member",
                content_type=ContentType.objects.get_for_model(models.Resource),
            ),
        )

        for user, resource in (
            (resource_project_viewer, resource_project_resource),
            (resource_viewer, directly_shared_resource),
        ):
            rows, _ = self.get(user, factories.ResourceFactory.get_list_url())
            self.assertEqual([row["uuid"] for row in rows], [resource.uuid.hex])
            detail, _ = self.get(user, factories.ResourceFactory.get_url(resource))
            self.assertEqual(rows[0], detail)
            self.assertIsNone(detail["creation_order"])
            self.assertNotIn("project_description", detail)

        rows, _ = self.get(
            resource_project_viewer, factories.ResourceFactory.get_list_url()
        )
        self.assertNotIn("limits", rows[0])
        rows, _ = self.get(resource_viewer, factories.ResourceFactory.get_list_url())
        self.assertIn("limits", rows[0])

    def test_listed_order_matches_its_detail_view(self):
        self.make_row()
        self.make_row(pending_update=False)
        url = factories.OrderFactory.get_list_url()
        for user in (self.provider_owner, self.offering_manager, self.consumer_owner):
            rows, _ = self.get(user, url)
            self.assertTrue(rows)
            for row in rows:
                with self.subTest(user=user.username, order=row["uuid"]):
                    detail, _ = self.get(user, row["url"])
                    self.assertEqual(row, detail)
