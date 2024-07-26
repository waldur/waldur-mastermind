from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.models import OfferingEstimatedCostPolicy
from waldur_mastermind.policy.tests import factories


@ddt
class GetPolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.policy = factories.OfferingEstimatedCostPolicyFactory(scope=self.offering)
        self.url = factories.OfferingEstimatedCostPolicyFactory.get_list_url()

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
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.url = factories.OfferingEstimatedCostPolicyFactory.get_list_url()

    def _create_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {
            "limit_cost": 100,
            "actions": "notify_organization_owners",
            "scope": marketplace_factories.OfferingFactory.get_url(self.offering),
            "organization_groups": [
                structure_factories.OrganizationGroupFactory.get_url()
            ],
        }
        return self.client.post(self.url, payload)

    @data("staff", "offering_owner")
    def test_user_can_create_policy(self, user):
        response = self._create_policy(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        policy = OfferingEstimatedCostPolicy.objects.get(uuid=response.data["uuid"])
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
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.policy = factories.OfferingEstimatedCostPolicyFactory(scope=self.offering)
        self.url = factories.OfferingEstimatedCostPolicyFactory.get_url(self.policy)

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
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.policy = factories.OfferingEstimatedCostPolicyFactory(scope=self.offering)
        self.url = factories.OfferingEstimatedCostPolicyFactory.get_url(self.policy)

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


class OfferingEstimatedCostPolicyTriggerTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.customer = self.fixture.customer
        self.resource = self.fixture.resource
        self.policy = factories.OfferingEstimatedCostPolicyFactory(
            scope=self.offering, limit_cost=10
        )
        self.organization_group = structure_factories.OrganizationGroupFactory()
        self.policy.organization_groups.add(self.organization_group)

    def test_policy_has_fired_if_limit_exceeded(self):
        invoice = invoices_factories.InvoiceFactory(customer=self.customer)
        invoice_item = invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            resource=self.resource,
            unit_price=5,
            quantity=3,
        )

        self.policy.refresh_from_db()
        self.assertFalse(self.policy.has_fired)

        self.customer.organization_group = self.organization_group
        self.customer.save()

        invoice_item.quantity = 1
        invoice_item.save()
        self.policy.refresh_from_db()
        self.assertFalse(self.policy.has_fired)

        invoice_item.quantity = 3
        invoice_item.save()
        self.policy.refresh_from_db()
        self.assertTrue(self.policy.has_fired)
