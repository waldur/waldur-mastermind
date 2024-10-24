import datetime

from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.policy.models import OfferingUsagePolicy
from waldur_mastermind.policy.tests import factories, fixtures


@ddt
class GetPolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OfferingUsagePolicyFixture()
        self.offering = self.fixture.offering
        self.policy = self.fixture.policy
        self.url = factories.OfferingUsagePolicyFactory.get_list_url()

    @data("staff", "offering_owner")
    def test_user_can_get_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    @data("user", "owner", "customer_support", "admin", "manager")
    def test_user_can_not_get_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


@ddt
class CreatePolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OfferingUsagePolicyFixture()
        self.offering = self.fixture.offering
        self.url = factories.OfferingUsagePolicyFactory.get_list_url()

    def _create_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {
            "actions": "notify_organization_owners",
            "scope": marketplace_factories.OfferingFactory.get_url(self.offering),
            "organization_groups": [
                structure_factories.OrganizationGroupFactory.get_url()
            ],
            "component_limits_set": [
                {"type": self.fixture.offering_usage_component.type, "limit": 10}
            ],
        }
        return self.client.post(self.url, payload)

    @data("staff", "offering_owner")
    def test_user_can_create_policy(self, user):
        response = self._create_policy(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        policy = OfferingUsagePolicy.objects.get(uuid=response.data["uuid"])
        self.assertEqual(policy.has_fired, False)

    @data("admin", "manager", "user", "owner")
    def test_user_can_not_create_policy(self, user):
        response = self._create_policy(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validate_actions(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "limit_cost": 100,
            "actions": "notify_organization_owners,non_existent_method",
            "scope": marketplace_factories.OfferingFactory.get_url(self.offering),
            "organization_groups": [
                structure_factories.OrganizationGroupFactory.get_url()
            ],
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_some_policies_for_one_offering(self):
        response = self._create_policy("staff")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self._create_policy("staff")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


@ddt
class DeletePolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OfferingUsagePolicyFixture()
        self.policy = self.fixture.policy
        self.url = factories.OfferingUsagePolicyFactory.get_url(self.policy)

    def _delete_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.delete(self.url)

    @data("staff", "offering_owner")
    def test_user_can_delete_policy(self, user):
        response = self._delete_policy(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data("user", "owner", "admin", "manager")
    def test_user_can_not_delete_policy(self, user):
        response = self._delete_policy(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class UpdatePolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OfferingUsagePolicyFixture()
        self.policy = self.fixture.policy
        self.url = factories.OfferingUsagePolicyFactory.get_url(self.policy)

    def _update_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.patch(self.url, {"actions": "notify_organization_owners"})

    @data("staff", "offering_owner")
    def test_user_can_update_policy(self, user):
        response = self._update_policy(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("user", "owner", "admin", "manager")
    def test_user_can_not_update_policy(self, user):
        response = self._update_policy(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class OfferingUsagePolicyTriggerTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OfferingUsagePolicyFixture()
        self.customer = self.fixture.customer
        self.resource = self.fixture.resource
        self.policy = self.fixture.policy
        self.component = self.fixture.offering_usage_component
        self.organization_group = self.fixture.organization_group

    def test_policy_has_fired_if_limit_exceeded(self):
        usage = marketplace_factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=100,
        )

        self.policy.refresh_from_db()
        self.assertFalse(self.policy.has_fired)

        self.customer.organization_group = self.organization_group
        self.customer.save()

        usage.delete()
        usage = marketplace_factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=self.fixture.component_limit.limit - 1,
        )
        self.policy.refresh_from_db()
        self.assertFalse(self.policy.has_fired)

        usage.delete()
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=self.fixture.component_limit.limit + 1,
        )
        self.policy.refresh_from_db()
        self.assertTrue(self.policy.has_fired)

    @freeze_time("2024-09-01")
    def test_policy_period(self):
        self.customer.organization_group = self.organization_group
        self.customer.save()

        # period = 1 month
        usage = marketplace_factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=self.fixture.component_limit.limit + 1,
            billing_period=core_utils.month_start(
                datetime.datetime(day=1, month=9, year=2024)
            ),
        )
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)

        usage.billing_period = core_utils.month_start(
            datetime.datetime(day=1, month=7, year=2024)
        )
        usage.save()
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, False)

        url = factories.OfferingUsagePolicyFactory.get_url(self.policy)
        self.client.force_authenticate(self.fixture.staff)

        # period = 3 month
        self.client.patch(url, {"period": OfferingUsagePolicy.Periods.MONTH_3})
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)

        usage.billing_period = core_utils.month_start(
            datetime.datetime(day=1, month=10, year=2023)
        )
        usage.save()
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, False)

        # period = 12 month
        self.client.patch(url, {"period": OfferingUsagePolicy.Periods.MONTH_12})
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)

        usage.billing_period = core_utils.month_start(
            datetime.datetime(day=1, month=9, year=2023)
        )
        usage.save()
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, False)

        # period = Total
        self.client.patch(url, {"period": OfferingUsagePolicy.Periods.TOTAL})
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)
