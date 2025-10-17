import datetime

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.models import (
    CustomerComponentUsagePolicy,
    CustomerUsagePolicyComponent,
)
from waldur_mastermind.policy.tests import factories as policy_factories


@ddt
class CustomerComponentUsagePolicyCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.staff = self.fixture.staff
        self.owner = self.fixture.owner
        self.user = self.fixture.user

        self.component_usage_policy_list_url = (
            policy_factories.CustomerComponentUsagePolicyFactory.get_list_url()
        )

    @data("owner", "user")
    def test_create_is_forbidden_for_non_staff(self, role):
        self.client.force_authenticate(getattr(self.fixture, role))

        payload = {
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "actions": "notify_organization_owners",
            "component_limits_set": [],
            "options": {},
        }

        response = self.client.post(self.component_usage_policy_list_url, payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_is_allowed_for_staff(self):
        self.client.force_authenticate(self.staff)
        payload = {
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "actions": "notify_organization_owners",
            "component_limits_set": [],
            "options": {},
        }
        response = self.client.post(self.component_usage_policy_list_url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


@ddt
class CustomerComponentUsagePolicyUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.staff = self.fixture.staff
        self.owner = self.fixture.owner
        self.user = self.fixture.user

    @data("owner", "user")
    def test_update_is_forbidden_for_non_staff(self, role):
        policy = policy_factories.CustomerComponentUsagePolicyFactory(
            scope=self.customer
        )
        url = policy_factories.CustomerComponentUsagePolicyFactory.get_url(policy)

        self.client.force_authenticate(getattr(self.fixture, role))
        response = self.client.patch(url, {"actions": "notify_external_user"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_is_allowed_for_staff(self):
        policy = policy_factories.CustomerComponentUsagePolicyFactory(
            scope=self.customer
        )
        url = policy_factories.CustomerComponentUsagePolicyFactory.get_url(policy)

        self.client.force_authenticate(self.staff)
        response = self.client.patch(url, {"actions": "notify_external_user"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)


@ddt
class CustomerComponentUsagePolicyDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.staff = self.fixture.staff
        self.owner = self.fixture.owner
        self.user = self.fixture.user

    @data("owner", "user")
    def test_delete_is_forbidden_for_non_staff(self, role):
        policy = policy_factories.CustomerComponentUsagePolicyFactory(
            scope=self.customer
        )
        url = policy_factories.CustomerComponentUsagePolicyFactory.get_url(policy)

        self.client.force_authenticate(getattr(self.fixture, role))
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_delete_is_allowed_for_staff(self):
        policy = policy_factories.CustomerComponentUsagePolicyFactory(
            scope=self.customer
        )
        url = policy_factories.CustomerComponentUsagePolicyFactory.get_url(policy)

        self.client.force_authenticate(self.staff)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)


@ddt
class CustomerComponentUsagePolicyValidationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.staff = self.fixture.staff
        self.url = policy_factories.CustomerComponentUsagePolicyFactory.get_list_url()

    def _payload_with_component(self, billing_type):
        component = marketplace_factories.OfferingComponentFactory(
            offering=self.fixture.offering,
            billing_type=billing_type,
            type="ram",
            name="RAM",
        )
        return {
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "actions": "notify_organization_owners",
            "options": {},
            "component_limits_set": [
                {
                    "component": component.uuid.hex,
                    "limit": 10,
                    "period": 1,
                }
            ],
        }

    @data(BillingTypes.USAGE, BillingTypes.LIMIT)
    def test_create_with_valid_billing_type(self, billing_type):
        self.client.force_authenticate(self.staff)
        payload = self._payload_with_component(billing_type)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data(BillingTypes.FIXED)
    def test_create_with_invalid_billing_type(self, billing_type):
        self.client.force_authenticate(self.staff)
        payload = self._payload_with_component(billing_type)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            CustomerComponentUsagePolicy.objects.filter(
                scope=self.customer, actions="notify_organization_owners"
            ).exists()
        )


class CustomerComponentUsagePolicyTriggerTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.offering = self.fixture.offering
        self.resource = self.fixture.resource

        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.USAGE,
            type="ram",
            name="RAM",
        )

        self.policy = policy_factories.CustomerComponentUsagePolicyFactory(
            scope=self.customer, actions="notify_organization_owners"
        )

        CustomerUsagePolicyComponent.objects.create(
            policy=self.policy, component=self.component, limit=100, period=1
        )

    def test_policy_triggers_when_usage_exceeds_limit(self):
        marketplace_models.ComponentUsage.objects.create(
            resource=self.resource,
            component=self.component,
            usage=150,
            billing_period=core_utils.month_start(datetime.date.today()),
            date=datetime.datetime.now(),
        )

        self.policy.refresh_from_db()
        self.assertTrue(self.policy.is_triggered())

        self.assertTrue(self.policy.has_fired)
        self.assertIsNotNone(self.policy.fired_datetime)

    def test_policy_does_not_trigger_when_usage_within_limit(self):
        marketplace_models.ComponentUsage.objects.create(
            resource=self.resource,
            component=self.component,
            usage=50,
            billing_period=core_utils.month_start(datetime.date.today()),
            date=datetime.datetime.now(),
        )

        self.policy.refresh_from_db()
        self.assertFalse(self.policy.is_triggered())

        self.assertFalse(self.policy.has_fired)
        self.assertIsNone(self.policy.fired_datetime)
