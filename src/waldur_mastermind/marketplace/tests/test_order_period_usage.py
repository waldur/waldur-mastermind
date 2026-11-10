import datetime

from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    LimitPeriods,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories


@freeze_time("2026-10-15")
class OrderPeriodUsageTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            plugin_options={"show_order_period_totals": True},
        )
        plan = factories.PlanFactory(offering=self.offering)
        self.cpu = factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
        )
        self.gpu = factories.OfferingComponentFactory(
            offering=self.offering,
            type="gpu",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
        )
        self.storage = factories.OfferingComponentFactory(
            offering=self.offering,
            type="storage",
            billing_type=BillingTypes.USAGE,
        )
        for component in (self.cpu, self.gpu, self.storage):
            factories.PlanComponentFactory(plan=plan, component=component)
        self.resource = models.Resource.objects.create(
            offering=self.offering,
            plan=plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
            limits={"cpu": 500, "gpu": 10},
            end_date=datetime.date(2027, 6, 30),
        )
        self.plan_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource, plan=plan
        )

    def _usage(self, component, month, usage, day=20, plan_period=None):
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=plan_period or self.plan_period,
            component=component,
            usage=usage,
            billing_period=datetime.date(2026, month, 1),
            date=datetime.datetime(2026, month, day, tzinfo=datetime.UTC),
        )

    def _get(self, user, url=None):
        self.client.force_authenticate(user)
        return self.client.get(
            url
            or factories.ResourceFactory.get_url(self.resource, "order_period_usage")
        )

    def test_limit_component_sums_each_month_peak(self):
        # A plan period that starts mid-month reports that month again: the
        # month counts once, with its higher figure.
        second_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource, plan=self.resource.plan
        )
        self._usage(self.cpu, 8, 100, day=10)
        self._usage(self.cpu, 8, 120, day=25, plan_period=second_period)
        self._usage(self.cpu, 9, 80)
        self._usage(self.cpu, 10, 30)

        response = self._get(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [{"type": "cpu", "usage": 230.0}])

    def test_prepaid_and_usage_components_are_left_out(self):
        self._usage(self.gpu, 9, 4)
        self._usage(self.storage, 9, 50)

        response = self._get(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [{"type": "cpu", "usage": 0.0}])

    def test_prepaid_limit_component_is_left_out(self):
        # A limit component can be prepaid too; it is not part of the
        # order-period view, only the plain limit components are.
        ram = factories.OfferingComponentFactory(
            offering=self.offering,
            type="ram",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
            is_prepaid=True,
        )
        factories.PlanComponentFactory(plan=self.resource.plan, component=ram)
        self._usage(ram, 9, 64)
        self._usage(self.cpu, 9, 80)

        response = self._get(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [{"type": "cpu", "usage": 80.0}])

    def test_limit_components_without_a_monthly_period_are_left_out(self):
        # The UI multiplies the limit by months: right for a monthly limit only.
        quarterly = factories.OfferingComponentFactory(
            offering=self.offering,
            type="node_hours",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.QUARTERLY,
        )
        total = factories.OfferingComponentFactory(
            offering=self.offering,
            type="storage_hours",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
        )
        for component in (quarterly, total):
            factories.PlanComponentFactory(plan=self.resource.plan, component=component)
            self._usage(component, 9, 300)
        self._usage(self.cpu, 9, 80)

        response = self._get(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [{"type": "cpu", "usage": 80.0}])

    def test_owner_of_a_separate_provider_reads_it_on_the_provider_route_only(self):
        provider = structure_factories.CustomerFactory()
        provider_owner = structure_factories.UserFactory()
        provider.add_user(provider_owner, CustomerRole.OWNER)
        self.offering.customer = provider
        self.offering.save(update_fields=["customer"])
        self._usage(self.cpu, 9, 80)

        provider_response = self._get(
            provider_owner,
            factories.ResourceFactory.get_provider_resource_url(
                self.resource, "order_period_usage"
            ),
        )
        consumer_response = self._get(provider_owner)

        self.assertEqual(provider_response.status_code, status.HTTP_200_OK)
        self.assertEqual(provider_response.data, [{"type": "cpu", "usage": 80.0}])
        self.assertEqual(consumer_response.status_code, status.HTTP_404_NOT_FOUND)

    def test_offering_without_option_is_refused(self):
        self.offering.plugin_options = {}
        self.offering.save(update_fields=["plugin_options"])

        response = self._get(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_provider_route_returns_the_same_totals(self):
        self._usage(self.cpu, 9, 80)

        response = self._get(
            self.fixture.owner,
            factories.ResourceFactory.get_provider_resource_url(
                self.resource, "order_period_usage"
            ),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [{"type": "cpu", "usage": 80.0}])

    def test_project_member_can_read(self):
        response = self._get(self.fixture.member)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_without_access_gets_not_found(self):
        response = self._get(self.fixture.user)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
