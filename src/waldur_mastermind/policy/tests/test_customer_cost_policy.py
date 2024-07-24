from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.billing import models as billing_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.models import CustomerEstimatedCostPolicy
from waldur_mastermind.policy.tests import factories


class ActionsFunctionsTest(test.APITransactionTestCase):
    def setUp(self):
        self.notify_organization_owners_mock = mock.MagicMock()
        self.notify_organization_owners_mock.one_time_action = True
        self.notify_organization_owners_mock.__name__ = "notify_organization_owners"

        self.block_creation_of_new_resources_mock = mock.MagicMock()
        self.block_creation_of_new_resources_mock.one_time_action = False
        self.block_creation_of_new_resources_mock.__name__ = (
            "block_creation_of_new_resources"
        )

        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.policy = factories.CustomerEstimatedCostPolicyFactory(scope=self.customer)
        self.estimate = billing_models.PriceEstimate.objects.get(scope=self.customer)

    def tearDown(self):
        mock.patch.stopall()

    def test_calling_of_one_time_actions(self):
        with mock.patch.object(
            CustomerEstimatedCostPolicy,
            "get_all_actions",
            return_value=[
                self.notify_organization_owners_mock,
                self.block_creation_of_new_resources_mock,
            ],
        ):
            self.estimate.total = self.policy.limit_cost + 1
            self.estimate.save()
            self.notify_organization_owners_mock.assert_called_once()
            self.block_creation_of_new_resources_mock.assert_not_called()
            self.notify_organization_owners_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

            self.estimate.total = self.policy.limit_cost + 2
            self.estimate.save()
            self.notify_organization_owners_mock.assert_not_called()
            self.block_creation_of_new_resources_mock.assert_not_called()
            self.notify_organization_owners_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

            self.estimate.total = self.policy.limit_cost - 1
            self.estimate.save()
            self.notify_organization_owners_mock.assert_not_called()
            self.block_creation_of_new_resources_mock.assert_not_called()
            self.notify_organization_owners_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

            self.estimate.total = self.policy.limit_cost + 1
            self.estimate.save()
            self.notify_organization_owners_mock.assert_called_once()
            self.block_creation_of_new_resources_mock.assert_not_called()
            self.notify_organization_owners_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

    def test_calling_of_not_one_time_actions(self):
        with mock.patch.object(
            CustomerEstimatedCostPolicy,
            "get_all_actions",
            return_value=[
                self.notify_organization_owners_mock,
                self.block_creation_of_new_resources_mock,
            ],
        ):
            self.estimate.total = self.policy.limit_cost + 1
            self.estimate.save()

            self.notify_organization_owners_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

            order = marketplace_factories.OrderFactory(
                project=self.fixture.project,
                offering=self.fixture.offering,
                attributes={"name": "item_name", "description": "Description"},
                plan=self.fixture.plan,
                state=marketplace_models.Order.States.EXECUTING,
            )
            marketplace_utils.process_order(order, self.fixture.staff)

            self.notify_organization_owners_mock.assert_not_called()
            self.block_creation_of_new_resources_mock.assert_called()

    def test_has_fired(self):
        self.estimate.total = self.policy.limit_cost + 1
        self.estimate.save()
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)

        self.estimate.total = 0
        self.estimate.save()
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, False)
        self.assertTrue(self.policy.fired_datetime)

    def test_several_policies(self):
        with mock.patch.object(
            CustomerEstimatedCostPolicy,
            "get_all_actions",
            return_value=[
                self.notify_organization_owners_mock,
                self.block_creation_of_new_resources_mock,
            ],
        ):
            policy_2 = factories.CustomerEstimatedCostPolicyFactory(scope=self.customer)
            self.estimate.total = self.policy.limit_cost + 100
            self.estimate.save()
            self.policy.refresh_from_db()
            policy_2.refresh_from_db()
            self.assertEqual(self.policy.has_fired, True)
            self.assertEqual(policy_2.has_fired, True)


@ddt
class GetPolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.policy = factories.CustomerEstimatedCostPolicyFactory(scope=self.customer)
        self.url = factories.CustomerEstimatedCostPolicyFactory.get_list_url()

    @data("staff", "owner", "customer_support")
    def test_user_can_get_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    @data("user", "offering_owner", "admin", "manager")
    def test_user_can_not_get_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


@ddt
class CreatePolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.url = factories.CustomerEstimatedCostPolicyFactory.get_list_url()

    def _create_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {
            "limit_cost": 100,
            "actions": "notify_organization_owners,block_modification_of_existing_resources",
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
        }
        return self.client.post(self.url, payload)

    @data("staff")
    def test_user_can_create_policy(self, user):
        response = self._create_policy(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        policy = CustomerEstimatedCostPolicy.objects.get(uuid=response.data["uuid"])
        self.assertEqual(policy.has_fired, False)

    @data("owner", "customer_support", "user")
    def test_user_can_not_create_policy(self, user):
        response = self._create_policy(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validate_actions(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "limit_cost": 100,
            "actions": "notify_organization_owners,non_existent_method",
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_some_policies_for_one_customer(self):
        response = self._create_policy("staff")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self._create_policy("staff")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_policies_should_be_triggered_after_creation_if_cost_limit_has_been_reached(
        self,
    ):
        notify_organization_owners_mock = mock.MagicMock()
        notify_organization_owners_mock.one_time_action = True
        notify_organization_owners_mock.__name__ = "notify_organization_owners"

        block_creation_of_new_resources_mock = mock.MagicMock()
        block_creation_of_new_resources_mock.one_time_action = False
        block_creation_of_new_resources_mock.__name__ = (
            "block_creation_of_new_resources"
        )

        with mock.patch.object(
            CustomerEstimatedCostPolicy,
            "get_all_actions",
            return_value=[
                notify_organization_owners_mock,
                block_creation_of_new_resources_mock,
            ],
        ):
            estimate = billing_models.PriceEstimate.objects.get(scope=self.customer)
            estimate.total = 1000
            estimate.save()

            response = self._create_policy("staff")
            policy = CustomerEstimatedCostPolicy.objects.get(uuid=response.data["uuid"])
            notify_organization_owners_mock.assert_called_once()
            block_creation_of_new_resources_mock.assert_not_called()
            self.assertEqual(policy.has_fired, True)


@ddt
class DeletePolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.policy = factories.CustomerEstimatedCostPolicyFactory(scope=self.customer)
        self.url = factories.CustomerEstimatedCostPolicyFactory.get_url(self.policy)

    def _delete_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.delete(self.url)

    @data("staff")
    def test_user_can_delete_policy(self, user):
        response = self._delete_policy(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data("owner", "customer_support")
    def test_user_can_not_delete_policy(self, user):
        response = self._delete_policy(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class UpdatePolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.policy = factories.CustomerEstimatedCostPolicyFactory(scope=self.customer)
        self.url = factories.CustomerEstimatedCostPolicyFactory.get_url(self.policy)

    def _update_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.patch(self.url, {"actions": "notify_organization_owners"})

    @data("staff")
    def test_user_can_update_policy(self, user):
        response = self._update_policy(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("owner", "customer_support")
    def test_user_can_not_update_policy(self, user):
        response = self._update_policy(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
