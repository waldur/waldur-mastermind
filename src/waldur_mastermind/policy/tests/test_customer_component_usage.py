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
        self.policy = policy_factories.CustomerComponentUsagePolicyFactory(
            scope=self.customer
        )
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.fixture.offering,
            billing_type=BillingTypes.USAGE,
            type="cores",
            name="CPU cores",
        )
        CustomerUsagePolicyComponent.objects.create(
            policy=self.policy, component=self.component, limit=100, period=1
        )
        self.policy_url = policy_factories.CustomerComponentUsagePolicyFactory.get_url(
            self.policy
        )

    @data("owner", "user")
    def test_update_is_forbidden_for_non_staff(self, role):
        self.client.force_authenticate(getattr(self.fixture, role))
        response = self.client.patch(
            self.policy_url, {"actions": "notify_external_user"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_is_allowed_for_staff(self):
        self.client.force_authenticate(self.staff)
        response = self.client.patch(
            self.policy_url, {"actions": "notify_external_user"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_create_update_and_add_component_limits(self):
        self.client.force_authenticate(self.staff)

        # Initially, the policy has one limit (cores, period=1, limit=100)
        self.assertEqual(self.policy.component_limits_set.count(), 1)

        # Update the existing limit: change period to 2 and verify period_name
        update_payload = {
            "component_limits_set": [
                {
                    "component": self.component.uuid.hex,
                    "limit": 10,
                    "period": 2,
                }
            ]
        }
        update_response = self.client.patch(self.policy_url, update_payload)
        self.assertEqual(update_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(update_response.data["component_limits_set"]), 1)
        updated_limit = update_response.data["component_limits_set"][0]
        self.assertEqual(updated_limit.get("period"), 2)
        self.assertEqual(updated_limit.get("period_name"), "1 month")

        # Add a second limit for the same component with a different period (3)
        # Expect two limits in the response and correct periods and limits
        add_payload = {
            "component_limits_set": [
                {
                    "component": self.component.uuid.hex,
                    "limit": 2,
                    "period": 2,
                },
                {
                    "component": self.component.uuid.hex,
                    "limit": 5,
                    "period": 3,
                },
            ]
        }
        add_response = self.client.patch(self.policy_url, add_payload)
        self.assertEqual(add_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(add_response.data["component_limits_set"]), 2)
        periods = sorted(
            [i.get("period") for i in add_response.data["component_limits_set"]]
        )
        limits = sorted(
            [i.get("limit") for i in add_response.data["component_limits_set"]]
        )
        self.assertEqual(periods, [2, 3])
        self.assertEqual(limits, [2, 5])

        # Try adding a limit for a component with invalid billing type (FIXED)
        # Expect 400 and no changes to the existing policy limits
        fixed_component = marketplace_factories.OfferingComponentFactory(
            offering=self.fixture.offering,
            billing_type=BillingTypes.FIXED,
            type="ram",
            name="RAM",
        )

        add_payload = {
            "component_limits_set": [
                {
                    "component": fixed_component.uuid.hex,
                    "limit": 2,
                    "period": 2,
                },
            ]
        }
        add_response = self.client.patch(self.policy_url, add_payload)
        self.assertEqual(add_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.component_limits_set.count(), 2)

        # Attempt to send duplicate limits for the same component and period
        # Expect 400 due to uniqueness constraint and no changes to limits
        dup_payload = {
            "component_limits_set": [
                {
                    "component": self.component.uuid.hex,
                    "limit": 10,
                    "period": 1,
                },
                {
                    "component": self.component.uuid.hex,
                    "limit": 10,
                    "period": 1,
                },
            ]
        }
        dup_response = self.client.patch(self.policy_url, dup_payload)
        self.assertEqual(dup_response.status_code, status.HTTP_400_BAD_REQUEST)


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
