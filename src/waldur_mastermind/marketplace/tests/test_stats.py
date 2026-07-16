import datetime

from constance.test.unittest import override_config as override_constance_config
from ddt import data, ddt
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.quotas.tests import factories as quotas_factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.common.utils import parse_date, parse_datetime
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices import tasks as invoices_tasks
from waldur_mastermind.marketplace import models, tasks, utils
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_TENANT_OFFERING,
    SUPPORT_OFFERING,
    BillingTypes,
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories, fixtures


class StatsBaseTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project

        self.category = factories.CategoryFactory()
        self.category_component = factories.CategoryComponentFactory(
            category=self.category
        )

        self.offering = factories.OfferingFactory(
            category=self.category,
            type=OPENSTACK_TENANT_OFFERING,
            state=OfferingStates.ACTIVE,
        )
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            parent=self.category_component,
            type="cores",
            billing_type=BillingTypes.LIMIT,
        )


@freeze_time("2019-01-22")
class StatsTest(StatsBaseTest):
    def setUp(self):
        super().setUp()

        self.date = parse_date("2019-01-01")

        self.plan = factories.PlanFactory(offering=self.offering)
        self.plan_component = factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component, amount=10
        )

        self.resource = factories.ResourceFactory(
            project=self.project, offering=self.offering, plan=self.plan
        )

    def test_reported_usage_is_aggregated_for_project_and_customer(self):
        # Arrange
        plan_period = models.ResourcePlanPeriod.objects.create(
            start=parse_datetime("2019-01-01"),
            resource=self.resource,
            plan=self.plan,
        )

        models.ComponentUsage.objects.create(
            resource=self.resource,
            component=self.offering_component,
            date=parse_date("2019-01-10"),
            billing_period=parse_date("2019-01-01"),
            plan_period=plan_period,
            usage=100,
        )

        self.new_resource = factories.ResourceFactory(
            project=self.project, offering=self.offering, plan=self.plan
        )

        new_plan_period = models.ResourcePlanPeriod.objects.create(
            start=parse_date("2019-01-01"),
            resource=self.new_resource,
            plan=self.plan,
        )

        models.ComponentUsage.objects.create(
            resource=self.resource,
            component=self.offering_component,
            date=parse_date("2019-01-20"),
            billing_period=parse_date("2019-01-01"),
            plan_period=new_plan_period,
            usage=200,
        )

        # Act
        tasks.calculate_usage_for_current_month()

        # Assert
        project_usage = (
            models.CategoryComponentUsage.objects.filter(
                scope=self.project, component=self.category_component, date=self.date
            )
            .get()
            .reported_usage
        )
        customer_usage = (
            models.CategoryComponentUsage.objects.filter(
                scope=self.customer, component=self.category_component, date=self.date
            )
            .get()
            .reported_usage
        )

        self.assertEqual(project_usage, 300)
        self.assertEqual(customer_usage, 300)

    def test_fixed_usage_is_aggregated_for_project_and_customer(self):
        # Arrange
        models.ResourcePlanPeriod.objects.create(
            resource=self.resource,
            plan=self.plan,
            start=parse_date("2019-01-10"),
            end=parse_date("2019-01-20"),
        )

        # Act
        tasks.calculate_usage_for_current_month()

        # Assert
        project_usage = (
            models.CategoryComponentUsage.objects.filter(
                scope=self.project,
                component=self.category_component,
                date=self.date,
            )
            .get()
            .fixed_usage
        )
        customer_usage = (
            models.CategoryComponentUsage.objects.filter(
                scope=self.customer, component=self.category_component, date=self.date
            )
            .get()
            .fixed_usage
        )

        self.assertEqual(project_usage, self.plan_component.amount)
        self.assertEqual(customer_usage, self.plan_component.amount)

    def test_offering_customers_stats(self):
        url = factories.OfferingFactory.get_url(self.offering, action="customers")
        self.client.force_authenticate(self.fixture.staff)
        result = self.client.get(url)
        self.assertEqual(result.status_code, status.HTTP_200_OK)
        self.assertEqual(len(result.data), 1)
        self.assertEqual(
            result.data[0]["uuid"], self.resource.project.customer.uuid.hex
        )


@freeze_time("2020-01-01")
class CostsStatsTest(StatsBaseTest):
    def setUp(self):
        super().setUp()
        self.url = factories.OfferingFactory.get_url(self.offering, action="costs")

        self.plan = factories.PlanFactory(
            offering=self.offering,
            unit=UnitPriceMixin.Units.PER_DAY,
        )
        self.plan_component = factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component, amount=10
        )

        self.resource = factories.ResourceFactory(
            offering=self.offering,
            state=ResourceStates.OK,
            plan=self.plan,
            limits={"cores": 1},
        )
        invoices_tasks.create_monthly_invoices()

    def test_offering_costs_stats(self):
        with freeze_time("2020-03-01"):
            self._check_stats()

    def test_period_filter(self):
        self.client.force_authenticate(self.fixture.staff)

        result = self.client.get(self.url, {"other_param": ""})
        self.assertEqual(result.status_code, status.HTTP_200_OK)

        result = self.client.get(self.url, {"start": "2020-01"})
        self.assertEqual(result.status_code, status.HTTP_400_BAD_REQUEST)

    def test_offering_costs_stats_if_resource_has_been_failed(self):
        with freeze_time("2020-03-01"):
            self.resource.state = ResourceStates.ERRED
            self.resource.save()
            self._check_stats()

    def _check_stats(self):
        self.client.force_authenticate(self.fixture.staff)
        result = self.client.get(self.url, {"start": "2020-01", "end": "2020-02"})
        self.assertEqual(result.status_code, status.HTTP_200_OK)
        self.assertDictEqual(
            result.data[0],
            {
                "tax": 0,
                "total": self.plan_component.price * 31,
                "price": self.plan_component.price * 31,
                "period": "2020-01",
            },
        )

    def test_stat_methods_are_not_available_for_anonymous_users(self):
        result = self.client.get(self.url)
        self.assertEqual(result.status_code, status.HTTP_401_UNAUTHORIZED)

        customers_url = factories.OfferingFactory.get_url(
            self.offering, action="customers"
        )
        result = self.client.get(customers_url)
        self.assertEqual(result.status_code, status.HTTP_401_UNAUTHORIZED)


@freeze_time("2020-03-01")
class ComponentStatsTest(StatsBaseTest):
    def setUp(self):
        super().setUp()
        self.url = factories.OfferingFactory.get_url(
            self.offering, action="component_stats"
        )

        self.plan = factories.PlanFactory(
            offering=self.offering,
            unit=UnitPriceMixin.Units.PER_DAY,
        )
        self.plan_component = factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component, amount=10
        )

        self.resource = factories.ResourceFactory(
            offering=self.offering,
            state=ResourceStates.OK,
            plan=self.plan,
            limits={"cores": 1},
        )

    def _create_items(self):
        invoices_tasks.create_monthly_invoices()
        invoice = invoices_models.Invoice.objects.get(
            year=2020, month=3, customer=self.resource.project.customer
        )
        return invoice.items.filter(resource_id=self.resource.id)

    def test_item_details(self):
        sp = factories.ServiceProviderFactory(customer=self.resource.offering.customer)
        component = factories.OfferingComponentFactory(
            offering=self.resource.offering,
            billing_type=BillingTypes.LIMIT,
            type="storage",
        )
        factories.ComponentUsageFactory(
            resource=self.resource,
            billing_period=core_utils.month_start(timezone.now()),
            component=component,
        )
        item = self._create_items().first()
        self.assertDictEqual(
            item.details,
            {
                "resource_name": item.resource.name,
                "resource_uuid": item.resource.uuid.hex,
                "service_provider_name": self.resource.offering.customer.name,
                "service_provider_uuid": sp.uuid.hex,
                "offering_name": self.offering.name,
                "offering_type": OPENSTACK_TENANT_OFFERING,
                "offering_uuid": self.offering.uuid.hex,
                "plan_name": self.resource.plan.name,
                "plan_uuid": self.resource.plan.uuid.hex,
                "plan_component_id": self.plan_component.id,
                "offering_component_type": self.plan_component.component.type,
                "offering_component_name": self.plan_component.component.name,
                "discount_usage": 1.0,
                "resource_limit_periods": [
                    {
                        "end": "2020-03-31T23:59:59.999999+00:00",
                        "start": "2020-03-01T00:00:00+00:00",
                        "total": "31",
                        "quantity": 1,
                        "billing_periods": 31,
                    }
                ],
            },
        )

    def test_component_stats_if_invoice_item_details_includes_plan_component_data(
        self,
    ):
        self.resource.offering.type = SUPPORT_OFFERING
        self.resource.offering.save()
        self.offering_component.billing_type = BillingTypes.FIXED
        self.offering_component.save()

        self._create_items()
        self.client.force_authenticate(self.fixture.staff)
        result = self.client.get(self.url, {"start": "2020-03", "end": "2020-03"})
        self.assertEqual(
            result.data,
            [
                {
                    "description": self.offering_component.description,
                    "measured_unit": self.offering_component.measured_unit,
                    "name": self.offering_component.name,
                    "period": "2020-03",
                    "billing_period": "2020-03-01",
                    "date": "2020-03-31T00:00:00+00:00",
                    "type": self.offering_component.type,
                    "usage": 31,
                }
            ],
        )

    def test_handler(self):
        self.resource.offering.type = SUPPORT_OFFERING
        self.resource.offering.save()

        # add usage-based component to the offering and plan
        COMPONENT_TYPE = "storage"
        new_component = factories.OfferingComponentFactory(
            offering=self.resource.offering,
            billing_type=BillingTypes.USAGE,
            type=COMPONENT_TYPE,
        )
        factories.PlanComponentFactory(
            plan=self.plan,
            component=new_component,
        )

        self._create_items()
        factories.ComponentUsageFactory(
            resource=self.resource,
            date=timezone.now(),
            billing_period=core_utils.month_start(timezone.now()),
            component=new_component,
            usage=2,
        )
        self.client.force_authenticate(self.fixture.staff)
        result = self.client.get(self.url, {"start": "2020-03", "end": "2020-03"})
        component_cores = self.resource.offering.components.get(type="cores")
        component_storage = self.resource.offering.components.get(type="storage")
        self.assertEqual(len(result.data), 2)
        self.assertEqual(
            [r for r in result.data if r["type"] == component_cores.type][0],
            {
                "description": component_cores.description,
                "measured_unit": component_cores.measured_unit,
                "name": component_cores.name,
                "period": "2020-03",
                "billing_period": "2020-03-01",
                "date": "2020-03-31T00:00:00+00:00",
                "type": component_cores.type,
                "usage": 31,  # days in March of 1 core usage with per-day plan
            },
        )
        self.assertEqual(
            [r for r in result.data if r["type"] == component_storage.type][0],
            {
                "description": component_storage.description,
                "measured_unit": component_storage.measured_unit,
                "name": component_storage.name,
                "period": "2020-03",
                "billing_period": "2020-03-01",
                "date": "2020-03-31T00:00:00+00:00",
                "type": component_storage.type,
                "usage": 2,
            },
        )


@ddt
class CustomerStatsTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

    @data(
        "staff",
        "global_support",
    )
    def test_user_can_get_marketplace_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get("/api/marketplace-stats/customer_member_count/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_user_cannot_get_marketplace_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get("/api/marketplace-stats/customer_member_count/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_field_of_count_if_several_quotas_exist(self):
        customer = self.fixture.customer
        quota_1 = quotas_factories.QuotaFactory(
            object_id=customer.id,
            content_type=ContentType.objects.get_for_model(customer.__class__),
            name="nc_user_count",
            delta=10,
        )
        quota_2 = quotas_factories.QuotaFactory(
            object_id=customer.id,
            content_type=ContentType.objects.get_for_model(customer.__class__),
            name="nc_user_count",
            delta=5,
        )
        user = self.fixture.staff
        self.client.force_authenticate(user)
        response = self.client.get("/api/marketplace-stats/customer_member_count/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["count"], quota_1.delta + quota_2.delta)

    def _find_customer_data(self, response_data, customer):
        """Helper to find customer data in response by UUID."""
        customer_uuid = str(customer.uuid)
        return next((c for c in response_data if str(c["uuid"]) == customer_uuid), None)

    def test_has_resources_is_true_when_customer_has_active_resources(self):
        customer = self.fixture.customer
        factories.ResourceFactory(
            project=self.fixture.project,
            state=ResourceStates.OK,
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get("/api/marketplace-stats/customer_member_count/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        customer_data = self._find_customer_data(response.data, customer)
        self.assertIsNotNone(customer_data)
        self.assertTrue(customer_data["has_resources"])

    def test_has_resources_is_true_when_customer_has_updating_resources(self):
        customer = self.fixture.customer
        factories.ResourceFactory(
            project=self.fixture.project,
            state=ResourceStates.UPDATING,
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get("/api/marketplace-stats/customer_member_count/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        customer_data = self._find_customer_data(response.data, customer)
        self.assertIsNotNone(customer_data)
        self.assertTrue(customer_data["has_resources"])

    def test_has_resources_is_false_when_customer_has_no_resources(self):
        customer = self.fixture.customer
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get("/api/marketplace-stats/customer_member_count/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        customer_data = self._find_customer_data(response.data, customer)
        self.assertIsNotNone(customer_data)
        self.assertFalse(customer_data["has_resources"])

    def test_has_resources_is_false_when_customer_has_only_terminated_resources(self):
        customer = self.fixture.customer
        factories.ResourceFactory(
            project=self.fixture.project,
            state=ResourceStates.TERMINATED,
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get("/api/marketplace-stats/customer_member_count/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        customer_data = self._find_customer_data(response.data, customer)
        self.assertIsNotNone(customer_data)
        self.assertFalse(customer_data["has_resources"])

    def test_count_is_none_when_customer_has_no_quotas(self):
        customer = self.fixture.customer
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get("/api/marketplace-stats/customer_member_count/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        customer_data = self._find_customer_data(response.data, customer)
        self.assertIsNotNone(customer_data)
        self.assertIsNone(customer_data["count"])

    def test_response_includes_all_required_fields(self):
        customer = self.fixture.customer
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get("/api/marketplace-stats/customer_member_count/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        customer_data = self._find_customer_data(response.data, customer)
        self.assertIsNotNone(customer_data)
        self.assertIn("uuid", customer_data)
        self.assertIn("name", customer_data)
        self.assertIn("abbreviation", customer_data)
        self.assertIn("count", customer_data)
        self.assertIn("has_resources", customer_data)

    def test_multiple_customers_with_different_states(self):
        # Create a second customer with resources and quotas
        fixture2 = structure_fixtures.ProjectFixture()
        customer2 = fixture2.customer
        quotas_factories.QuotaFactory(
            object_id=customer2.id,
            content_type=ContentType.objects.get_for_model(customer2.__class__),
            name="nc_user_count",
            delta=7,
        )
        factories.ResourceFactory(
            project=fixture2.project,
            state=ResourceStates.OK,
        )

        # First customer has no resources and no quotas
        customer1 = self.fixture.customer

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get("/api/marketplace-stats/customer_member_count/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        customer1_data = self._find_customer_data(response.data, customer1)
        customer2_data = self._find_customer_data(response.data, customer2)

        self.assertIsNotNone(customer1_data)
        self.assertIsNotNone(customer2_data)

        # Customer 1: no resources, no quotas
        self.assertFalse(customer1_data["has_resources"])
        self.assertIsNone(customer1_data["count"])

        # Customer 2: has resources, has quotas
        self.assertTrue(customer2_data["has_resources"])
        self.assertEqual(customer2_data["count"], 7)


@ddt
class LimitsStatsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource_1 = factories.ResourceFactory(
            limits={"cpu": 5}, state=ResourceStates.OK
        )
        factories.ResourceFactory(
            limits={"cpu": 2},
            state=ResourceStates.OK,
            offering=self.resource_1.offering,
        )
        self.resource_2 = factories.ResourceFactory(
            limits={"cpu": 10, "ram": 1}, state=ResourceStates.OK
        )
        self.url = "/api/marketplace-stats/resources_limits/"

        self.organization_group_1 = structure_factories.OrganizationGroupFactory()
        self.organization_group_2 = structure_factories.OrganizationGroupFactory()
        self.resource_1.offering.organization_groups.add(
            self.organization_group_1, self.organization_group_2
        )

        self.resource_1.offering.country = "EE"
        self.resource_1.offering.save()

        self.resource_2.offering.customer.country = "FI"
        self.resource_2.offering.customer.save()

    @data(
        # skipping because it is not stable now 'staff',
        "global_support",
    )
    def test_user_can_get_marketplace_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            len(response.data),
            4,
        )
        self.assertTrue(
            {
                "offering_uuid": self.resource_1.offering.uuid,
                "name": "cpu",
                "value": 7,
                "offering_country": "EE",
                "organization_group_name": self.organization_group_1.name,
                "organization_group_uuid": self.organization_group_1.uuid.hex,
            }
            in response.data,
        )
        self.assertTrue(
            {
                "offering_uuid": self.resource_1.offering.uuid,
                "name": "cpu",
                "value": 7,
                "offering_country": "EE",
                "organization_group_name": self.organization_group_2.name,
                "organization_group_uuid": self.organization_group_2.uuid.hex,
            }
            in response.data,
        )
        self.assertTrue(
            {
                "offering_uuid": self.resource_2.offering.uuid,
                "name": "cpu",
                "value": 10,
                "offering_country": "FI",
                "organization_group_name": "",
                "organization_group_uuid": "",
            }
            in response.data,
        )
        self.assertTrue(
            {
                "offering_uuid": self.resource_2.offering.uuid,
                "name": "ram",
                "value": 1,
                "offering_country": "FI",
                "organization_group_name": "",
                "organization_group_uuid": "",
            }
            in response.data,
        )

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_user_cannot_get_marketplace_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
@override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
class CountUsersOfServiceProviderTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = "/api/marketplace-stats/count_users_of_service_providers/"
        self.service_provider = self.fixture.service_provider
        organization_group = structure_factories.OrganizationGroupFactory()
        self.service_provider.customer.organization_groups.add(organization_group)
        self.organization_groups = list(
            self.service_provider.customer.organization_groups.all()
        )

    @data(
        "staff",
        "global_support",
    )
    def test_user_can_get_marketplace_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), len(self.organization_groups))

        for record in response.data:
            self.assertIn("count", record)
            self.assertIn("customer_organization_group_uuid", record)
            self.assertIn("customer_organization_group_name", record)
            self.assertEqual(
                record["service_provider_uuid"], self.service_provider.uuid.hex
            )

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_user_cannot_get_marketplace_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_count_filters_by_tos_consent(self):
        """Test that user count is filtered by ToS consent. Test no consent, consent, and revoke consent."""
        models.OfferingTermsOfService.objects.create(
            offering=self.fixture.offering,
            terms_of_service="Test ToS",
            version="1.0",
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for record in response.data:
            if record["service_provider_uuid"] == self.service_provider.uuid.hex:
                self.assertEqual(record["count"], 0)

        models.UserOfferingConsent.objects.create(
            user=self.fixture.admin,
            offering=self.fixture.offering,
            version="1.0",
        )

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for record in response.data:
            if record["service_provider_uuid"] == self.service_provider.uuid.hex:
                self.assertEqual(record["count"], 1)

        consent = models.UserOfferingConsent.objects.get(
            user=self.fixture.admin, offering=self.fixture.offering
        )
        consent.revoke()

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for record in response.data:
            if record["service_provider_uuid"] == self.service_provider.uuid.hex:
                self.assertEqual(record["count"], 0)

    @override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=False)
    def test_count_ignores_tos_consent_when_disabled(self):
        """Test that user count ignores ToS consent when ENFORCE_USER_CONSENT_FOR_OFFERINGS is False."""
        models.OfferingTermsOfService.objects.create(
            offering=self.fixture.offering,
            terms_of_service="Test ToS",
            version="1.0",
        )

        user = structure_factories.UserFactory()
        self.fixture.project.add_user(user, ProjectRole.MANAGER)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should count users regardless of consent status when enforcement is disabled
        for record in response.data:
            if record["service_provider_uuid"] == self.service_provider.uuid.hex:
                self.assertEqual(record["count"], 1)

        # Create consent - count should remain the same since enforcement is disabled
        models.UserOfferingConsent.objects.create(
            user=user,
            offering=self.fixture.offering,
            version="1.0",
        )

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for record in response.data:
            if record["service_provider_uuid"] == self.service_provider.uuid.hex:
                self.assertEqual(record["count"], 1)


@ddt
class CountProjectsGroupedByOecdOfServiceProviderTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = "/api/marketplace-stats/count_projects_of_service_providers_grouped_by_oecd/"
        self.service_provider = self.fixture.service_provider
        organization_group = structure_factories.OrganizationGroupFactory()
        self.service_provider.customer.organization_groups.add(organization_group)
        self.organization_groups = list(
            self.service_provider.customer.organization_groups.all()
        )

    @data(
        "staff",
        "global_support",
    )
    def test_user_can_get_marketplace_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), len(self.organization_groups))

        for record in response.data:
            self.assertIn("count", record)
            self.assertIn("customer_organization_group_uuid", record)
            self.assertIn("customer_organization_group_name", record)
            self.assertEqual(
                record["service_provider_uuid"], self.service_provider.uuid.hex
            )

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_user_cannot_get_marketplace_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class CountUniqueUsersConnectedWithActiveResourcesOfServiceProviderTest(
    test.APITestCase
):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = "/api/marketplace-stats/count_unique_users_connected_with_active_resources_of_service_provider/"

    @data(
        "staff",
        "global_support",
    )
    def test_user_can_get_marketplace_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

        self.fixture.resource.state = ResourceStates.OK
        self.fixture.resource.save()

        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["count_users"], 0)

        self.fixture.admin
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["count_users"], 1)

        self.fixture.member
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["count_users"], 2)

        self.fixture.manager
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["count_users"], 3)

    def test_do_not_count_users_twice(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        self.fixture.resource.state = ResourceStates.OK
        self.fixture.resource.save()

        # Have one user
        self.fixture.manager
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["count_users"], 1)

        # the number of users has not increased
        new_resource = factories.ResourceFactory(
            offering=self.fixture.offering, state=ResourceStates.OK
        )
        new_resource.project.add_user(self.fixture.manager, ProjectRole.MANAGER)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["count_users"], 1)

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_user_cannot_get_marketplace_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class CountCustomersTest(test.APITestCase):
    @freeze_time("2020-01-01")
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.service_provider = self.fixture.service_provider
        self.fixture.resource.set_state_terminated()
        self.fixture.resource.save()

    def _create_resource(self, project=None):
        project = project or structure_factories.ProjectFactory()
        resource = factories.ResourceFactory(
            offering=self.fixture.offering,
            project=project,
        )
        factories.OrderFactory(
            offering=self.fixture.offering,
            project=project,
            resource=resource,
            type=OrderTypes.CREATE,
            state=OrderStates.DONE,
        )
        return resource

    def _terminate_resource(self, resource):
        factories.OrderFactory(
            offering=self.fixture.offering,
            state=OrderStates.DONE,
            resource=resource,
            type=OrderTypes.TERMINATE,
        )
        resource.state = ResourceStates.TERMINATED
        return resource.save()

    def test_count_customers_number_change(self):
        with freeze_time("2022-01-10"):
            self.assertEqual(
                0, utils.count_customers_number_change(self.service_provider)
            )

            new_resource = self._create_resource()
            self.assertEqual(
                1, utils.count_customers_number_change(self.service_provider)
            )

            self._terminate_resource(new_resource)
            self.assertEqual(
                0, utils.count_customers_number_change(self.service_provider)
            )

            resource_1 = self._create_resource()
            resource_2 = self._create_resource()
            self.assertEqual(
                2, utils.count_customers_number_change(self.service_provider)
            )

        with freeze_time("2022-02-10"):
            self.assertEqual(
                0, utils.count_customers_number_change(self.service_provider)
            )

            self._terminate_resource(resource_1)
            self.assertEqual(
                -1, utils.count_customers_number_change(self.service_provider)
            )

            self._create_resource(project=resource_2.project)
            self.assertEqual(
                -1, utils.count_customers_number_change(self.service_provider)
            )

        with freeze_time("2022-03-10"):
            self.assertEqual(
                0, utils.count_customers_number_change(self.service_provider)
            )

            self._create_resource(project=new_resource.project)
            self.assertEqual(
                1, utils.count_customers_number_change(self.service_provider)
            )

    def test_count_resources_number_change(self):
        with freeze_time("2022-01-10"):
            self.assertEqual(
                0, utils.count_resources_number_change(self.service_provider)
            )

            new_resource = self._create_resource()
            self.assertEqual(
                1, utils.count_resources_number_change(self.service_provider)
            )

            self._terminate_resource(new_resource)
            self.assertEqual(
                0, utils.count_resources_number_change(self.service_provider)
            )

            resource_1 = self._create_resource()
            resource_2 = self._create_resource()
            self.assertEqual(
                2, utils.count_resources_number_change(self.service_provider)
            )

        with freeze_time("2022-02-10"):
            self.assertEqual(
                0, utils.count_resources_number_change(self.service_provider)
            )

            self._terminate_resource(resource_1)
            self.assertEqual(
                -1, utils.count_resources_number_change(self.service_provider)
            )

            self._create_resource(project=resource_2.project)
            self.assertEqual(
                0, utils.count_resources_number_change(self.service_provider)
            )

        with freeze_time("2022-03-10"):
            self.assertEqual(
                0, utils.count_resources_number_change(self.service_provider)
            )

            self._create_resource(project=new_resource.project)
            self.assertEqual(
                1, utils.count_resources_number_change(self.service_provider)
            )


class OfferingStatsTest(test.APITestCase):
    @freeze_time("2020-01-01")
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.url = factories.OfferingFactory.get_url(self.offering, "stats")

    def test_offering_stats(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.data["resources_count"], 1)
        self.assertEqual(response.data["customers_count"], 1)

        new_resource = factories.ResourceFactory(offering=self.offering)
        response = self.client.get(self.url)
        self.assertEqual(response.data["resources_count"], 2)
        self.assertEqual(response.data["customers_count"], 2)

        new_resource.state = ResourceStates.TERMINATED
        new_resource.save()
        response = self.client.get(self.url)
        self.assertEqual(response.data["resources_count"], 1)
        self.assertEqual(response.data["customers_count"], 1)

        factories.ResourceFactory(offering=self.offering, project=self.fixture.project)
        response = self.client.get(self.url)
        self.assertEqual(response.data["resources_count"], 2)
        self.assertEqual(response.data["customers_count"], 1)


class CountActiveResourcesByOrganizationGroupTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.url = (
            "/api/marketplace-stats/"
            "count_active_resources_grouped_by_organization_group/"
        )

        self.group_1 = structure_factories.OrganizationGroupFactory()
        self.group_2 = structure_factories.OrganizationGroupFactory()

        # customer_1 belongs to both groups -> its resources counted in both
        self.customer_1 = structure_factories.CustomerFactory()
        self.customer_1.organization_groups.add(self.group_1, self.group_2)
        self.offering_1 = factories.OfferingFactory(customer=self.customer_1)
        factories.ResourceFactory(offering=self.offering_1, state=ResourceStates.OK)
        factories.ResourceFactory(
            offering=self.offering_1, state=ResourceStates.TERMINATING
        )
        # terminated resource must be excluded
        factories.ResourceFactory(
            offering=self.offering_1, state=ResourceStates.TERMINATED
        )

        # customer_2 belongs only to group_2
        self.customer_2 = structure_factories.CustomerFactory()
        self.customer_2.organization_groups.add(self.group_2)
        self.offering_2 = factories.OfferingFactory(customer=self.customer_2)
        factories.ResourceFactory(offering=self.offering_2, state=ResourceStates.OK)

        # customer without any organization group must not appear
        self.offering_3 = factories.OfferingFactory()
        factories.ResourceFactory(offering=self.offering_3, state=ResourceStates.OK)

    def test_active_resources_are_grouped_by_organization_group(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        counts = {record["uuid"]: record["count"] for record in response.data}
        # group_1: only customer_1's active resources (2)
        self.assertEqual(counts[self.group_1.uuid.hex], 2)
        # group_2: customer_1 (2) + customer_2 (1)
        self.assertEqual(counts[self.group_2.uuid.hex], 3)
        # only the two groups with active resources are present
        self.assertEqual(len(response.data), 2)

    def test_number_of_queries_does_not_grow_with_groups(self):
        self.client.force_authenticate(self.fixture.staff)

        with CaptureQueriesContext(connection) as ctx_before:
            self.client.get(self.url)
        queries_before = len(ctx_before.captured_queries)

        # add more groups with resources; query count must stay constant
        for _ in range(3):
            group = structure_factories.OrganizationGroupFactory()
            customer = structure_factories.CustomerFactory()
            customer.organization_groups.add(group)
            offering = factories.OfferingFactory(customer=customer)
            factories.ResourceFactory(offering=offering, state=ResourceStates.OK)

        with CaptureQueriesContext(connection) as ctx_after:
            self.client.get(self.url)
        queries_after = len(ctx_after.captured_queries)

        self.assertEqual(queries_before, queries_after)


class OfferingStatsCounterTest(test.APITestCase):
    def setUp(self):
        self.provider1 = factories.structure_factories.CustomerFactory()
        self.category1 = factories.CategoryFactory()

        self.provider2 = factories.structure_factories.CustomerFactory()
        self.category2 = factories.CategoryFactory()

        self.offering1 = factories.OfferingFactory(
            customer=self.provider1,
            category=self.category1,
            state=OfferingStates.ACTIVE,
        )
        factories.PlanFactory(offering=self.offering1)

        self.offering2 = factories.OfferingFactory(
            customer=self.provider1,
            category=self.category1,
            state=OfferingStates.ACTIVE,
        )
        factories.PlanFactory(offering=self.offering2)

        self.offering3 = factories.OfferingFactory(
            customer=self.provider2,
            category=self.category2,
            state=OfferingStates.ACTIVE,
        )
        factories.PlanFactory(offering=self.offering3)

        self.url = "/api/marketplace-stats/offerings_counter_stats/"

        self.client.force_authenticate(
            factories.structure_factories.UserFactory(is_staff=True)
        )

    def get_provider_category_stats(self, data, provider_name, category_title):
        """Helper method to retreive the offerings data."""
        return next(
            (
                item
                for item in data
                if item["service_provider_name"] == provider_name
                and item["category_title"] == category_title
            ),
            None,
        )

    def delete_offerings(self, offerings):
        """Helper method to delete the offerings created during tests."""
        for offering in offerings:
            url = factories.OfferingFactory.get_url(offering)
            self.client.delete(url)

    def test_offer_counter_stats(self):
        """Test that offerings are properly grouped by service provider and category."""

        response = self.client.get(self.url)

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Unexpected status code, expected 200 got: {response.status_code}",
        )

        data = response.data

        # Assert provider1 with category1 has 2 offerings
        provider1_category1_stats = self.get_provider_category_stats(
            data, self.provider1.name, self.category1.title
        )

        # Check that there are 2 offerings with the first provider and category
        self.assertIsNotNone(
            provider1_category1_stats,
            f"Provider {self.provider1.name} and category {self.category1.title} should have stats, got None",
        )
        self.assertEqual(
            provider1_category1_stats["count"],
            2,
            f"Expected 2 offerings for {self.provider1.name} in category {self.category1.title}, but got {provider1_category1_stats['count']}",
        )

        # Check that there is 1 offering for second provider and category
        provider2_category2_stats = self.get_provider_category_stats(
            data, self.provider2.name, self.category2.title
        )
        self.assertIsNotNone(
            provider2_category2_stats,
            f"Provider {self.provider2.name} and category {self.category2.title} should have stats, got None",
        )
        self.assertEqual(
            provider2_category2_stats["count"],
            1,
            f"Expected 1 offering for {self.provider2.name} in category {self.category2.title}, but got {provider2_category2_stats['count']}",
        )

    def test_no_offerings_in_system(self):
        """Test the case when there are no offerings in the system."""

        # Clear any offerings created during setup
        self.delete_offerings([self.offering1, self.offering2, self.offering3])

        response = self.client.get(self.url)

        # Assert that response is retreived and that data is empty
        self.assertEqual(
            response.status_code, status.HTTP_200_OK, "API did not return HTTP 200 OK"
        )
        self.assertEqual(
            response.data,
            [],
            f"Expected empty list when there are no offerings, but got: {response.data}",
        )

    def test_offerings_counter_excludes_non_active_states(self):
        """Test that DRAFT and ARCHIVED state offerings are not included."""

        # Create offerings with state DRAFT and ARCHIVED
        self.offering4 = factories.OfferingFactory(
            customer=self.provider1,
            category=self.category1,
            state=OfferingStates.DRAFT,
        )

        self.offering5 = factories.OfferingFactory(
            customer=self.provider1,
            category=self.category1,
            state=OfferingStates.ARCHIVED,
        )

        response = self.client.get(self.url)
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Unexpected status code, expected 200 got: {response.status_code}",
        )

        data = response.data

        # Assert that there are still 2 offerings for provider 1 with category 1 (DRAFT is excluded)
        provider1_category1_stats = self.get_provider_category_stats(
            data, self.provider1.name, self.category1.title
        )
        self.assertIsNotNone(
            provider1_category1_stats,
            f"Provider {self.provider1.name} and category {self.category1.title} should have stats, got None",
        )
        self.assertEqual(
            provider1_category1_stats["count"],
            2,
            f"Expected 2 offerings for {self.provider1.name} in category {self.category1.title}, but got {provider1_category1_stats['count']}",
        )

    def test_no_offerins_returned_for_non_existing_provider(self):
        """Test the case where no offerings exist for a new provider/category."""
        provider3 = factories.structure_factories.CustomerFactory()
        category3 = factories.CategoryFactory()

        # Check that no offerings stats are returned for provider3 with category3
        response = self.client.get(self.url)
        self.assertEqual(
            response.status_code, status.HTTP_200_OK, "API did not return HTTP 200 OK"
        )

        data = response.data
        provider3_category3_stats = self.get_provider_category_stats(
            data, provider3.name, category3.title
        )

        # Assert that the provider without offerings is not returned
        self.assertIsNone(
            provider3_category3_stats,
            f"Expected no stats for provider {provider3.name} and category {category3.title}, but found some",
        )


class CountResourceProvisioningStatsTest(StatsBaseTest):
    def setUp(self):
        super().setUp()
        self.url = "/api/marketplace-stats/resource_provisioning_stats/"

    def test_stats_aggregation(self):
        # Create completed order
        order = factories.OrderFactory(
            offering=self.offering,
            state=OrderStates.DONE,
            type=OrderTypes.CREATE,
            created=timezone.now() - datetime.timedelta(minutes=30),
            completed_at=timezone.now() - datetime.timedelta(minutes=10),
        )

        # Mock event for execution start (20 mins ago)
        from waldur_core.logging import models as logging_models
        from waldur_core.logging.enums import EventType

        ct = ContentType.objects.get_for_model(order)
        event = logging_models.Event.objects.create(
            event_type=EventType.MARKETPLACE_ORDER_APPROVED,
            message="Order approved",
            created=timezone.now() - datetime.timedelta(minutes=20),
            context={},
        )
        logging_models.Feed.objects.create(
            content_type=ct, object_id=order.id, event=event
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        data = response.data[0]
        self.assertEqual(data["provisioning_count"], 1)
        self.assertEqual(data["provisioning_success_count"], 1)
        self.assertEqual(data["provisioning_error_count"], 0)
        self.assertEqual(data["provisioning_success_rate"], 1.0)

        # Pending: 30m ago created, 20m ago approved -> 10m (600s)
        # Provisioning: 20m ago approved, 10m ago completed -> 10m (600s)
        self.assertAlmostEqual(data["avg_pending_duration"], 600, delta=10)
        self.assertAlmostEqual(data["avg_provisioning_duration"], 600, delta=10)

    def test_failed_order(self):
        # Create failed order
        factories.OrderFactory(
            offering=self.offering,
            state=OrderStates.ERRED,
            type=OrderTypes.CREATE,
            created=timezone.now() - datetime.timedelta(minutes=30),
            completed_at=timezone.now(),
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data[0]
        self.assertEqual(data["provisioning_count"], 1)
        self.assertEqual(data["provisioning_success_count"], 0)
        self.assertEqual(data["provisioning_error_count"], 1)
        self.assertEqual(data["provisioning_success_rate"], 0.0)

    def test_invalid_param(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"last_minutes": "invalid"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_in_progress_order(self):
        factories.OrderFactory(
            offering=self.offering,
            state=OrderStates.EXECUTING,
            type=OrderTypes.CREATE,
            created=timezone.now() - datetime.timedelta(minutes=5),
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # We expect one entry now because in-progress order creates an entry
        self.assertEqual(len(response.data), 1)
        data = response.data[0]
        self.assertEqual(data["provisioning_count"], 0)
        self.assertEqual(data["provisioning_in_progress_count"], 1)


@ddt
class CountUserIdentitySourceStatsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = "/api/marketplace-stats/user_identity_source_count/"
        # Create users with different identity sources
        structure_factories.UserFactory(identity_source="google")
        structure_factories.UserFactory(identity_source="google")
        structure_factories.UserFactory(
            identity_source="keycloak", registration_method="saml2"
        )
        structure_factories.UserFactory(identity_source="")

    @data("staff", "global_support")
    def test_user_can_get_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check expected counts
        def get_count(source):
            for item in response.data:
                if item["identity_source"] == source:
                    return item["count"]
            return 0

        self.assertEqual(get_count("google"), 2)
        self.assertEqual(get_count("keycloak"), 1)

    @data("owner", "customer_support", "admin", "manager")
    def test_user_cannot_get_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ComponentUsagesStatsTest(test.APITestCase):
    """Tests for /api/marketplace-stats/component_usages/ endpoint.

    This endpoint returns component usages for the current month,
    expanded with offering country and organization group information.
    Fixes PUHURI-PORTALS-DWP (N+1 query issue).
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = "/api/marketplace-stats/component_usages/"
        self.organization_group_1 = structure_factories.OrganizationGroupFactory(
            name="Group A"
        )
        self.organization_group_2 = structure_factories.OrganizationGroupFactory(
            name="Group B"
        )

    def _create_component_usage(self, offering, component_type="cpu", usage=100):
        """Helper to create a component usage for the current month."""
        now = timezone.now()
        plan = factories.PlanFactory(offering=offering)
        resource = factories.ResourceFactory(offering=offering, plan=plan)
        # Get or create component to avoid unique constraint violation
        component, _ = models.OfferingComponent.objects.get_or_create(
            offering=offering, type=component_type
        )
        plan_period = models.ResourcePlanPeriod.objects.create(
            start=now, resource=resource, plan=plan
        )
        return models.ComponentUsage.objects.create(
            resource=resource,
            component=component,
            billing_period=now.replace(day=1).date(),
            plan_period=plan_period,
            usage=usage,
            date=now,
        )

    @data("staff", "global_support")
    def test_user_can_get_component_usages(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        self._create_component_usage(self.fixture.offering)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("owner", "customer_support", "admin", "manager")
    def test_user_cannot_get_component_usages(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_component_usages_include_offering_country(self):
        """Test that offering country is included in the response."""
        offering = factories.OfferingFactory(country="FI")
        self._create_component_usage(offering)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["offering_country"], "FI")

    def test_component_usages_fallback_to_customer_country(self):
        """Test that customer country is used when offering country is not set."""
        customer = structure_factories.CustomerFactory(country="EE")
        offering = factories.OfferingFactory(country="", customer=customer)
        self._create_component_usage(offering)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["offering_country"], "EE")

    def test_component_usages_expanded_by_organization_groups(self):
        """Test that usages are expanded per organization group."""
        offering = factories.OfferingFactory()
        offering.organization_groups.add(
            self.organization_group_1, self.organization_group_2
        )
        self._create_component_usage(offering)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should have 2 entries - one per organization group
        self.assertEqual(len(response.data), 2)
        group_names = {item["organization_group_name"] for item in response.data}
        self.assertEqual(group_names, {"Group A", "Group B"})

    def test_component_usages_no_organization_groups(self):
        """Test that usages without organization groups have empty group fields."""
        offering = factories.OfferingFactory()
        self._create_component_usage(offering)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["organization_group_name"], "")
        self.assertEqual(response.data[0]["organization_group_uuid"], "")

    def test_component_usages_aggregates_by_offering_and_component(self):
        """Test that usages are aggregated by offering and component type."""
        offering = factories.OfferingFactory()
        # Create two usages for the same offering/component
        self._create_component_usage(offering, component_type="cpu", usage=100)
        self._create_component_usage(offering, component_type="cpu", usage=50)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should have aggregated usage (API returns usage as string)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(float(response.data[0]["usage"]), 150.0)

    def test_component_usages_multiple_offerings_with_groups(self):
        """Test handling of multiple offerings with different organization groups.

        This test ensures the N+1 query fix works correctly by using
        bulk prefetching for offerings and their organization groups.
        """
        # Offering 1 with Group A
        offering1 = factories.OfferingFactory(country="FI")
        offering1.organization_groups.add(self.organization_group_1)
        self._create_component_usage(offering1, component_type="cpu", usage=100)

        # Offering 2 with Group B
        offering2 = factories.OfferingFactory(country="SE")
        offering2.organization_groups.add(self.organization_group_2)
        self._create_component_usage(offering2, component_type="ram", usage=200)

        # Offering 3 with no groups
        offering3 = factories.OfferingFactory(country="NO")
        self._create_component_usage(offering3, component_type="storage", usage=300)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # 3 entries: one for each offering (2 with groups, 1 without)
        self.assertEqual(len(response.data), 3)

        # Verify data integrity
        countries = {item["offering_country"] for item in response.data}
        self.assertEqual(countries, {"FI", "SE", "NO"})


@ddt
class ResourcesMissingUsageTest(test.APITestCase):
    """Tests for /api/marketplace-stats/resources_missing_usage/ endpoint.

    This endpoint returns resources with usage-based billing components
    that have no usage reported for the specified billing period.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = "/api/marketplace-stats/resources_missing_usage/"

        # Create an offering with usage-based component
        self.offering_with_usage = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
        )
        self.usage_component = factories.OfferingComponentFactory(
            offering=self.offering_with_usage,
            billing_type=BillingTypes.USAGE,
            type="cpu",
        )
        self.plan = factories.PlanFactory(offering=self.offering_with_usage)
        factories.PlanComponentFactory(
            plan=self.plan,
            component=self.usage_component,
        )

    @data("staff", "global_support")
    def test_user_can_get_resources_missing_usage(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("owner", "customer_support", "admin", "manager")
    def test_user_cannot_get_resources_missing_usage(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_resource_with_no_usage_is_returned(self):
        """Test that resources without usage reports are returned."""
        resource = factories.ResourceFactory(
            offering=self.offering_with_usage,
            plan=self.plan,
            state=ResourceStates.OK,
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(resource.uuid))

    def test_resource_with_usage_is_excluded(self):
        """Test that resources with usage reports are not returned."""
        resource = factories.ResourceFactory(
            offering=self.offering_with_usage,
            plan=self.plan,
            state=ResourceStates.OK,
        )
        now = timezone.now()
        factories.ComponentUsageFactory(
            resource=resource,
            component=self.usage_component,
            billing_period=now.replace(day=1).date(),
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_billing_period_filter(self):
        """Test filtering by specific billing period."""
        resource = factories.ResourceFactory(
            offering=self.offering_with_usage,
            plan=self.plan,
            state=ResourceStates.OK,
        )
        # Add usage for current month
        now = timezone.now()
        factories.ComponentUsageFactory(
            resource=resource,
            component=self.usage_component,
            billing_period=now.replace(day=1).date(),
        )
        self.client.force_authenticate(self.fixture.staff)

        # Resource should not appear for current month
        current_period = now.strftime("%Y-%m")
        response = self.client.get(self.url, {"billing_period": current_period})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

        # Resource should appear for previous month
        previous_month = (now.replace(day=1) - datetime.timedelta(days=1)).strftime(
            "%Y-%m"
        )
        response = self.client.get(self.url, {"billing_period": previous_month})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_invalid_billing_period_format(self):
        """Test that invalid billing period format returns 400."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"billing_period": "invalid"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_provider_uuid_filter(self):
        """Test filtering by provider UUID."""
        factories.ResourceFactory(
            offering=self.offering_with_usage,
            plan=self.plan,
            state=ResourceStates.OK,
        )
        self.client.force_authenticate(self.fixture.staff)

        # Filter by correct provider
        provider_uuid = self.offering_with_usage.customer.uuid
        response = self.client.get(self.url, {"provider_uuid": str(provider_uuid)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        # Filter by different provider
        other_customer = structure_factories.CustomerFactory()
        response = self.client.get(
            self.url, {"provider_uuid": str(other_customer.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_terminated_resources_are_excluded(self):
        """Test that terminated resources are not returned."""
        factories.ResourceFactory(
            offering=self.offering_with_usage,
            plan=self.plan,
            state=ResourceStates.TERMINATED,
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_response_includes_all_required_fields(self):
        """Test that response includes all expected fields."""
        factories.ResourceFactory(
            offering=self.offering_with_usage,
            plan=self.plan,
            state=ResourceStates.OK,
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        data = response.data[0]
        expected_fields = [
            "uuid",
            "name",
            "state",
            "created",
            "offering_name",
            "offering_uuid",
            "provider_name",
            "provider_uuid",
            "customer_name",
            "customer_uuid",
            "project_name",
            "project_uuid",
            "last_usage_date",
            "days_since_last_report",
        ]
        for field in expected_fields:
            self.assertIn(field, data)


@ddt
class OrderStatsTest(test.APITestCase):
    """Tests for /api/marketplace-stats/order_stats/ endpoint.

    This endpoint returns comprehensive order statistics including
    daily breakdown, state/type aggregations, and summary stats.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = "/api/marketplace-stats/order_stats/"

    @data("staff", "global_support")
    def test_user_can_get_order_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("owner", "customer_support", "admin", "manager")
    def test_user_cannot_get_order_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_response_structure(self):
        """Test that response has the expected structure."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("summary", response.data)
        self.assertIn("by_state", response.data)
        self.assertIn("by_type", response.data)
        self.assertIn("daily", response.data)

        summary = response.data["summary"]
        expected_summary_fields = [
            "total",
            "total_cost",
            "pending",
            "executing",
            "done",
            "erred",
            "canceled",
            "rejected",
        ]
        for field in expected_summary_fields:
            self.assertIn(field, summary)

    def test_order_counts_by_state(self):
        """Test that orders are correctly counted by state."""
        # Create orders with different states
        factories.OrderFactory(state=OrderStates.DONE)
        factories.OrderFactory(state=OrderStates.DONE)
        factories.OrderFactory(state=OrderStates.ERRED)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        summary = response.data["summary"]
        # Note: fixture already creates 1 DONE order, so we expect 3 total
        self.assertGreaterEqual(summary["done"], 2)
        self.assertGreaterEqual(summary["erred"], 1)

    def test_date_range_filter(self):
        """Test filtering by date range."""
        # Create order in the past
        with freeze_time("2023-01-15"):
            factories.OrderFactory(state=OrderStates.DONE)

        # Create order today
        factories.OrderFactory(state=OrderStates.DONE)

        self.client.force_authenticate(self.fixture.staff)

        # Filter for specific date range
        response = self.client.get(
            self.url, {"start": "2023-01-01", "end": "2023-01-31"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["summary"]["total"], 1)

    def test_invalid_date_format(self):
        """Test that invalid date formats return 400."""
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(self.url, {"start": "invalid"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.get(self.url, {"end": "invalid"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_provider_uuid_filter(self):
        """Test filtering by provider UUID."""
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        factories.OrderFactory(offering=offering, state=OrderStates.DONE)

        self.client.force_authenticate(self.fixture.staff)

        # Filter by correct provider
        response = self.client.get(
            self.url, {"provider_uuid": str(offering.customer.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["summary"]["total"], 1)

        # Filter by different provider
        other_customer = structure_factories.CustomerFactory()
        response = self.client.get(
            self.url, {"provider_uuid": str(other_customer.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["summary"]["total"], 0)

    def test_customer_uuid_filter(self):
        """Test filtering by customer UUID."""
        project = structure_factories.ProjectFactory()
        factories.OrderFactory(project=project, state=OrderStates.DONE)

        self.client.force_authenticate(self.fixture.staff)

        # Filter by correct customer
        response = self.client.get(
            self.url, {"customer_uuid": str(project.customer.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["summary"]["total"], 1)

        # Filter by different customer
        other_customer = structure_factories.CustomerFactory()
        response = self.client.get(
            self.url, {"customer_uuid": str(other_customer.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["summary"]["total"], 0)

    def test_daily_breakdown(self):
        """Test that daily breakdown structure is correct."""
        # Create orders today
        factories.OrderFactory(state=OrderStates.DONE)
        factories.OrderFactory(state=OrderStates.DONE)

        self.client.force_authenticate(self.fixture.staff)
        # Use default date range (last 30 days)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        daily = response.data["daily"]
        # Should have at least one entry (today)
        self.assertGreaterEqual(len(daily), 1)

        # Check daily entry structure
        for entry in daily:
            self.assertIn("date", entry)
            self.assertIn("total", entry)
            self.assertIn("total_cost", entry)
            self.assertIn("by_state", entry)
            self.assertIn("by_type", entry)


@ddt
class ProviderResourcesStatsTest(test.APITestCase):
    """Tests for /api/marketplace-stats/provider_resources/ endpoint.

    This endpoint returns resource statistics for a service provider.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = "/api/marketplace-stats/provider_resources/"

    @data("staff", "global_support")
    def test_user_can_get_provider_resources(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(
            self.url, {"provider_uuid": str(self.fixture.service_provider.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("owner", "customer_support", "admin", "manager")
    def test_user_cannot_get_provider_resources(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(
            self.url, {"provider_uuid": str(self.fixture.service_provider.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_provider_uuid_is_required(self):
        """Test that provider_uuid is required."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_provider_uuid(self):
        """Test that invalid provider UUID returns 404."""
        self.client.force_authenticate(self.fixture.staff)
        import uuid

        response = self.client.get(self.url, {"provider_uuid": str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_response_structure(self):
        """Test that response has the expected structure."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"provider_uuid": str(self.fixture.service_provider.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("total", response.data)
        self.assertIn("by_state", response.data)
        self.assertIn("by_offering", response.data)
        self.assertIn("monthly", response.data)

    def test_resource_counts_by_state(self):
        """Test that resources are correctly counted by state."""
        # The fixture already creates resources; add more for testing
        factories.ResourceFactory(
            offering=self.fixture.offering,
            state=ResourceStates.OK,
        )
        factories.ResourceFactory(
            offering=self.fixture.offering,
            state=ResourceStates.TERMINATED,
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"provider_uuid": str(self.fixture.service_provider.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Total excludes terminated resources
        self.assertGreaterEqual(response.data["total"], 1)

    def test_resource_counts_by_state_uses_human_readable_names(self):
        """Test that by_state dictionary uses human-readable state names as keys."""
        factories.ResourceFactory(
            offering=self.fixture.offering,
            state=ResourceStates.OK,
        )
        factories.ResourceFactory(
            offering=self.fixture.offering,
            state=ResourceStates.ERRED,
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"provider_uuid": str(self.fixture.service_provider.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        by_state = response.data["by_state"]
        self.assertIn("OK", by_state)
        self.assertIn("Erred", by_state)
        # Verify they are not numeric strings (which would be '1', '2' etc.)
        self.assertNotIn(str(ResourceStates.OK), by_state)
        self.assertNotIn(str(ResourceStates.ERRED), by_state)

    def test_resource_counts_by_offering(self):
        """Test that resources are grouped by offering."""
        factories.ResourceFactory(
            offering=self.fixture.offering,
            state=ResourceStates.OK,
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"provider_uuid": str(self.fixture.service_provider.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        by_offering = response.data["by_offering"]
        self.assertGreaterEqual(len(by_offering), 1)
        self.assertIn("offering_uuid", by_offering[0])
        self.assertIn("offering_name", by_offering[0])
        self.assertIn("count", by_offering[0])


@ddt
class ProviderCustomersStatsTest(test.APITestCase):
    """Tests for /api/marketplace-stats/provider_customers/ endpoint.

    This endpoint returns customer statistics for a service provider.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = "/api/marketplace-stats/provider_customers/"

    @data("staff", "global_support")
    def test_user_can_get_provider_customers(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(
            self.url, {"provider_uuid": str(self.fixture.service_provider.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("owner", "customer_support", "admin", "manager")
    def test_user_cannot_get_provider_customers(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(
            self.url, {"provider_uuid": str(self.fixture.service_provider.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_provider_uuid_is_required(self):
        """Test that provider_uuid is required."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_provider_uuid(self):
        """Test that invalid provider UUID returns 404."""
        self.client.force_authenticate(self.fixture.staff)
        import uuid

        response = self.client.get(self.url, {"provider_uuid": str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_response_structure(self):
        """Test that response has the expected structure."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"provider_uuid": str(self.fixture.service_provider.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("total", response.data)
        self.assertIn("new_this_month", response.data)
        self.assertIn("top_by_revenue", response.data)
        self.assertIn("top_by_resources", response.data)
        self.assertIn("monthly", response.data)

    def test_customer_count_with_active_resources(self):
        """Test that customers with active resources are counted."""
        # Create a resource with a different customer
        project = structure_factories.ProjectFactory()
        factories.ResourceFactory(
            offering=self.fixture.offering,
            project=project,
            state=ResourceStates.OK,
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"provider_uuid": str(self.fixture.service_provider.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["total"], 1)


@ddt
class ProviderOfferingsStatsTest(test.APITestCase):
    """Tests for /api/marketplace-stats/provider_offerings/ endpoint.

    This endpoint returns offering performance statistics for a service provider.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = "/api/marketplace-stats/provider_offerings/"

    @data("staff", "global_support")
    def test_user_can_get_provider_offerings(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(
            self.url, {"provider_uuid": str(self.fixture.service_provider.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("owner", "customer_support", "admin", "manager")
    def test_user_cannot_get_provider_offerings(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(
            self.url, {"provider_uuid": str(self.fixture.service_provider.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_provider_uuid_is_required(self):
        """Test that provider_uuid is required."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_provider_uuid(self):
        """Test that invalid provider UUID returns 404."""
        self.client.force_authenticate(self.fixture.staff)
        import uuid

        response = self.client.get(self.url, {"provider_uuid": str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_response_structure(self):
        """Test that response has the expected structure."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"provider_uuid": str(self.fixture.service_provider.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("offerings", response.data)
        if response.data["offerings"]:
            offering = response.data["offerings"][0]
            self.assertIn("offering_uuid", offering)
            self.assertIn("offering_name", offering)
            self.assertIn("category_name", offering)
            self.assertIn("state", offering)
            self.assertIn("active_resources", offering)
            self.assertIn("total_resources", offering)
            self.assertIn("revenue", offering)
            self.assertIn("plans", offering)

            if offering["plans"]:
                plan = offering["plans"][0]
                self.assertIn("plan_uuid", plan)
                self.assertIn("plan_name", plan)
                self.assertIn("usage", plan)
                self.assertIn("limit", plan)
                self.assertIn("utilization", plan)

    def test_offering_statistics(self):
        """Test that offering statistics are correctly calculated."""
        # Create active resources for the offering
        factories.ResourceFactory(
            offering=self.fixture.offering,
            state=ResourceStates.OK,
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"provider_uuid": str(self.fixture.service_provider.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        offerings = response.data["offerings"]
        self.assertGreaterEqual(len(offerings), 1)

        # Find our offering
        offering_data = [
            o
            for o in offerings
            if o["offering_uuid"] == str(self.fixture.offering.uuid)
        ]
        self.assertEqual(len(offering_data), 1)
        offering = offering_data[0]

        expected_fields = [
            "offering_uuid",
            "offering_name",
            "state",
            "active_resources",
            "total_resources",
            "revenue",
            "plans",
        ]
        for field in expected_fields:
            self.assertIn(field, offering)


class PlanComponentSerializerTest(test.APITestCase):
    """Tests for PlanComponentSerializer fields."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        # Ensure plan component is created by accessing it
        self.plan_component = self.fixture.plan_component
        self.url = "/api/marketplace-plan-components/"

    def test_serializer_includes_offering_uuid(self):
        """Test that offering_uuid is included in serializer output."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(
            len(response.data), 0, "Expected at least one plan component"
        )
        # Check that offering_uuid field exists in response
        pc = response.data[0]
        self.assertIn("offering_uuid", pc)
        self.assertIn("offering_name", pc)

    def test_serializer_includes_plan_uuid(self):
        """Test that plan_uuid is included in serializer output."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(
            len(response.data), 0, "Expected at least one plan component"
        )
        # Check that plan_uuid field exists in response
        pc = response.data[0]
        self.assertIn("plan_uuid", pc)
        self.assertIn("plan_name", pc)


@ddt
class ResourceUsageByOrganizationTypeTest(test.APITestCase):
    """Tests for /api/marketplace-stats/resource_usage_by_organization_type/ endpoint.

    This endpoint returns component usages grouped by the organization type
    of users who are members of the resource's project.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = "/api/marketplace-stats/resource_usage_by_organization_type/"

    def _create_usage_for_resource(self, resource, component_type="cpu", usage=100):
        """Helper to create component usage for a resource."""
        now = timezone.now()
        component, _ = models.OfferingComponent.objects.get_or_create(
            offering=resource.offering, type=component_type
        )
        plan_period = models.ResourcePlanPeriod.objects.create(
            start=now, resource=resource, plan=resource.plan
        )
        return models.ComponentUsage.objects.create(
            resource=resource,
            component=component,
            billing_period=now.replace(day=1).date(),
            plan_period=plan_period,
            usage=usage,
            date=now,
        )

    @data("staff", "global_support")
    def test_user_can_get_stats(self, user):
        """Test that staff and support users can access the endpoint."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("owner", "customer_support", "admin", "manager")
    def test_user_cannot_get_stats(self, user):
        """Test that regular users cannot access the endpoint."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_usage_attributed_to_project_member_org_type(self):
        """Test that usage is attributed to project member's organization type."""
        # Set organization type for project admin
        self.fixture.admin.organization_type = (
            "urn:schac:homeOrganizationType:int:university"
        )
        self.fixture.admin.save()

        # Create usage for the resource
        self._create_usage_for_resource(self.fixture.resource)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Find entry for university org type
        university_entries = [
            r
            for r in response.data
            if r["organization_type"] == "urn:schac:homeOrganizationType:int:university"
        ]
        self.assertGreaterEqual(len(university_entries), 1)

    def test_multiple_members_different_org_types(self):
        """Test that usage appears under each org type when project has multiple members."""
        # Set different organization types for project members
        self.fixture.admin.organization_type = (
            "urn:schac:homeOrganizationType:int:university"
        )
        self.fixture.admin.save()

        self.fixture.manager.organization_type = (
            "urn:schac:homeOrganizationType:int:company"
        )
        self.fixture.manager.save()

        # Create usage for the resource
        self._create_usage_for_resource(self.fixture.resource)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        org_types = {r["organization_type"] for r in response.data}
        self.assertIn("urn:schac:homeOrganizationType:int:university", org_types)
        self.assertIn("urn:schac:homeOrganizationType:int:company", org_types)

    def test_empty_org_type_shown_when_members_have_no_org_type(self):
        """Test that empty org type is shown when project members have no organization type."""
        # Ensure project members have no organization type
        self.fixture.admin.organization_type = ""
        self.fixture.admin.save()

        # Create usage for the resource
        self._create_usage_for_resource(self.fixture.resource)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Find entry for empty org type
        empty_entries = [r for r in response.data if r["organization_type"] == ""]
        self.assertGreaterEqual(len(empty_entries), 1)

    def test_response_includes_all_required_fields(self):
        """Test that response includes all expected fields."""
        self._create_usage_for_resource(self.fixture.resource)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        if len(response.data) > 0:
            entry = response.data[0]
            self.assertIn("organization_type", entry)
            self.assertIn("component_type", entry)
            self.assertIn("usage", entry)
            self.assertIn("resource_count", entry)

    def test_usage_not_attributed_to_order_creator(self):
        """Test that usage is NOT attributed to the order creator.

        This verifies the fix for the issue where service accounts creating
        resources would have their (empty) organization type used instead
        of actual project members.
        """
        # Create a service account user who creates the order
        service_account = structure_factories.UserFactory(
            username="service-account",
            organization_type="",  # Service account has no org type
        )

        # Set organization type for project member
        self.fixture.admin.organization_type = (
            "urn:schac:homeOrganizationType:int:university"
        )
        self.fixture.admin.save()

        # Create order with service account as creator
        factories.OrderFactory(
            resource=self.fixture.resource,
            project=self.fixture.project,
            type=OrderTypes.CREATE,
            state=OrderStates.DONE,
            created_by=service_account,
        )

        # Create usage for the resource
        self._create_usage_for_resource(self.fixture.resource)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Usage should be attributed to project member (university), not service account (empty)
        org_types = {r["organization_type"] for r in response.data}
        self.assertIn("urn:schac:homeOrganizationType:int:university", org_types)


@ddt
class ProjectsLimitsGroupedByIndustryFlagTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = "/api/marketplace-stats/projects_limits_grouped_by_industry_flag/"

    @data("staff", "global_support")
    def test_user_can_get_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("limits", response.data)

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_user_cannot_get_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_limits_are_grouped_by_industry_flag(self):
        # Set up an industry project with an active resource
        self.fixture.project.is_industry = True
        self.fixture.project.save()
        self.fixture.resource.state = ResourceStates.OK
        self.fixture.resource.limits = {"cpu": 10, "ram": 20}
        self.fixture.resource.save()

        # Set up a non-industry project with an active resource
        non_industry_project = structure_factories.ProjectFactory(
            customer=self.fixture.customer, is_industry=False
        )
        factories.ResourceFactory(
            offering=self.fixture.offering,
            project=non_industry_project,
            state=ResourceStates.OK,
            limits={"cpu": 5, "ram": 8},
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        limits = response.data["limits"]
        self.assertIn("True", limits)
        self.assertIn("False", limits)
        self.assertEqual(limits["True"]["cpu"], 10)
        self.assertEqual(limits["True"]["ram"], 20)
        self.assertEqual(limits["False"]["cpu"], 5)
        self.assertEqual(limits["False"]["ram"], 8)

    def test_resources_with_empty_limits_are_excluded(self):
        self.fixture.resource.state = ResourceStates.OK
        self.fixture.resource.limits = {}
        self.fixture.resource.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["limits"], {})

    def test_non_ok_resources_are_excluded(self):
        self.fixture.resource.state = ResourceStates.CREATING
        self.fixture.resource.limits = {"cpu": 10}
        self.fixture.resource.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["limits"], {})

    def test_limits_are_summed_across_resources(self):
        self.fixture.resource.state = ResourceStates.OK
        self.fixture.resource.limits = {"cpu": 10}
        self.fixture.resource.save()

        factories.ResourceFactory(
            offering=self.fixture.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
            limits={"cpu": 5, "ram": 8},
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        limits = response.data["limits"]
        field_value = str(self.fixture.project.is_industry)
        self.assertEqual(limits[field_value]["cpu"], 15)
        self.assertEqual(limits[field_value]["ram"], 8)


@ddt
class ProjectsLimitsGroupedByOecdTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = "/api/marketplace-stats/projects_limits_grouped_by_oecd/"

    @data("staff", "global_support")
    def test_user_can_get_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("limits", response.data)

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_user_cannot_get_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class UserStatsTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.staff = self.fixture.staff
        self.support = self.fixture.global_support
        self.user = self.fixture.user

        structure_factories.UserFactory.create_batch(
            3, nationality="EE", country_of_residence="EE"
        )
        structure_factories.UserFactory.create_batch(
            2, nationality="FI", country_of_residence="FI"
        )
        structure_factories.UserFactory.create_batch(
            1, nationality="", country_of_residence="LV"
        )

    def test_staff_can_access_user_stats(self):
        self.client.force_authenticate(self.staff)

        # Test nationality
        response = self.client.get("/api/marketplace-stats/user_nationality/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            any(
                item["nationality"] == "EE" and item["count"] == 3
                for item in response.data
            )
        )

        # Test residence
        response = self.client.get("/api/marketplace-stats/user_residence_country/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            any(
                item["country_of_residence"] == "EE" and item["count"] == 3
                for item in response.data
            )
        )

    def test_support_can_access_user_stats(self):
        self.client.force_authenticate(self.support)
        response = self.client.get("/api/marketplace-stats/user_nationality/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_regular_user_cannot_access_user_stats(self):
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/marketplace-stats/user_nationality/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_user_cannot_access_user_stats(self):
        response = self.client.get("/api/marketplace-stats/user_nationality/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class CreationTrendStatsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

    @data("staff", "global_support")
    def test_staff_can_access_project_creation_trend(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get("/api/marketplace-stats/project_creation_trend/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.data) > 0)
        self.assertIn("month", response.data[0])
        self.assertIn("count", response.data[0])

    @data("staff", "global_support")
    def test_staff_can_access_resource_creation_trend(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get("/api/marketplace-stats/resource_creation_trend/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_user_cannot_access_creation_trends(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get("/api/marketplace-stats/project_creation_trend/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class TopProviderStatsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

    @data("staff", "global_support")
    def test_staff_can_access_top_service_providers_by_resources(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(
            "/api/marketplace-stats/top_service_providers_by_resources/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("staff", "global_support")
    def test_count_active_resources_grouped_by_offering_with_pagination(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(
            "/api/marketplace-stats/count_active_resources_grouped_by_offering/",
            {"page_size": 2},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.data) <= 2)
        self.assertIn("X-Result-Count", response.headers)

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_user_cannot_access_top_providers(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(
            "/api/marketplace-stats/top_service_providers_by_resources/"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
