import datetime

from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.common.utils import parse_datetime
from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories


@ddt
@freeze_time('2017-01-10 00:00:00')
class SubmitUsageTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.service_provider = factories.ServiceProviderFactory()
        self.secret_code = self.service_provider.api_secret_code
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.plan = factories.PlanFactory(unit=UnitPriceMixin.Units.PER_DAY, offering=self.offering)
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=models.OfferingComponent.BillingTypes.USAGE
        )
        self.component = factories.PlanComponentFactory(
            plan=self.plan,
            component=self.offering_component
        )
        self.resource = models.Resource.objects.create(
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
        )

        factories.OrderItemFactory(
            resource=self.resource,
            type=models.RequestTypeMixin.Types.CREATE,
            state=models.OrderItem.States.EXECUTING,
            plan=self.plan
        )
        callbacks.resource_creation_succeeded(self.resource)
        self.plan_period = models.ResourcePlanPeriod.objects.get(resource=self.resource)

    def test_valid_signature(self):
        payload = self.get_valid_payload()
        response = self.client.post('/api/marketplace-public-api/check_signature/', payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_invalid_signature(self):
        response = self.submit_usage(data='wrong_signature')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_usage(self):
        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.ComponentUsage.objects.filter(resource=self.resource,
                                                             component=self.offering_component,
                                                             date=datetime.date.today()).exists())

    def test_submit_usage_with_description(self):
        description = 'My first usage report'
        response = self.submit_usage(**self.get_valid_payload(description=description))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        report = models.ComponentUsage.objects.get(resource=self.resource)
        self.assertEqual(report.description, description)

    def test_plan_period_linking(self):
        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        usage = models.ComponentUsage.objects.get(resource=self.resource,
                                                  component=self.offering_component,
                                                  date=datetime.date.today())
        plan_period = models.ResourcePlanPeriod.objects.get(resource=self.resource,
                                                            start=datetime.date(2017, 1, 10),
                                                            end__isnull=True)
        self.assertEqual(usage.plan_period, plan_period)

    @data('staff', 'owner')
    def test_authenticated_user_can_submit_usage_via_api(self, role):
        self.client.force_authenticate(getattr(self.fixture, role))
        response = self.client.post('/api/marketplace-component-usages/set_usage/', self.get_usage_data())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.ComponentUsage.objects.filter(resource=self.resource,
                                                             component=self.offering_component,
                                                             date=datetime.date.today()).exists())

    @data('admin', 'manager', 'user')
    def test_other_user_can_not_submit_usage_via_api(self, role):
        self.client.force_authenticate(getattr(self.fixture, role))
        response = self.client.post('/api/marketplace-component-usages/set_usage/', self.get_usage_data())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.ComponentUsage.objects.filter(resource=self.resource,
                                                              component=self.offering_component,
                                                              date=datetime.date.today()).exists())

    @data(models.Resource.States.CREATING, models.Resource.States.TERMINATED)
    def test_it_should_not_be_possible_to_submit_usage_for_pending_resource(self, state):
        self.resource.state = state
        self.resource.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post('/api/marketplace-component-usages/set_usage/', self.get_usage_data())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_total_amount_exceeds_month_limit(self):
        self.offering_component.limit_period = models.OfferingComponent.LimitPeriods.MONTH
        self.offering_component.limit_amount = 1
        self.offering_component.save()
        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_total_amount_does_not_exceed_month_limit(self):
        self.offering_component.limit_period = models.OfferingComponent.LimitPeriods.MONTH
        self.offering_component.limit_amount = 10
        self.offering_component.save()
        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_total_amount_exceeds_total_limit(self):
        self.offering_component.limit_period = models.OfferingComponent.LimitPeriods.TOTAL
        self.offering_component.limit_amount = 7
        self.offering_component.save()

        self.submit_usage()
        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_total_amount_does_not_exceed_total_limit(self):
        self.offering_component.limit_period = models.OfferingComponent.LimitPeriods.TOTAL
        self.offering_component.limit_amount = 15
        self.offering_component.save()

        self.submit_usage()
        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_not_create_usage_if_component_not_exists(self):
        response = self.submit_usage(**self.get_valid_payload(component_type='ram'))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_usage_if_usage_exists(self):
        self.submit_usage()
        response = self.submit_usage(**self.get_valid_payload(amount=15))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        usage = models.ComponentUsage.objects.first()
        self.assertEqual(usage.usage, 15)

    def test_usage_is_not_updated_if_billing_period_is_closed(self):
        self.plan_period.end = parse_datetime('2016-01-10')
        self.plan_period.save()
        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_dry_run_mode(self):
        response = self.submit_usage(dry_run=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(models.ComponentUsage.objects.filter().exists())

    def test_usage_is_not_updated_if_resource_is_terminated(self):
        self.resource.set_state_terminated()
        self.resource.save()
        response = self.submit_usage()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def submit_usage(self, **extra):
        payload = self.get_valid_payload()
        payload.update(extra)
        return self.client.post('/api/marketplace-public-api/set_usage/', payload)

    def get_valid_payload(self, **kwargs):
        data = self.get_usage_data(**kwargs)
        payload = dict(
            customer=self.service_provider.customer.uuid,
            data=core_utils.encode_jwt_token(data, self.secret_code)
        )
        return payload

    def get_usage_data(self, component_type='cpu', amount=5, description=''):
        return {
            'plan_period': self.plan_period.uuid,
            'usages': [{
                'type': component_type,
                'amount': amount,
                'description': description,
            }]
        }
