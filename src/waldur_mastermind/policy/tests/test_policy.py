from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.billing import models as billing_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.models import ProjectEstimatedCostPolicy
from waldur_mastermind.policy.tests import factories


class ActionsFunctionsTest(test.APITransactionTestCase):
    def setUp(self):
        self.notify_project_team_mock = mock.MagicMock()
        self.notify_project_team_mock.one_time_action = True
        self.notify_project_team_mock.__name__ = 'notify_project_team'

        self.block_creation_of_new_resources_mock = mock.MagicMock()
        self.block_creation_of_new_resources_mock.one_time_action = False
        self.block_creation_of_new_resources_mock.__name__ = (
            'block_creation_of_new_resources'
        )

        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.policy = factories.ProjectEstimatedCostPolicyFactory(project=self.project)
        self.estimate = billing_models.PriceEstimate.objects.get(scope=self.project)

    def tearDown(self):
        mock.patch.stopall()

    def test_calling_of_one_time_actions(self):
        with mock.patch.object(
            ProjectEstimatedCostPolicy,
            'get_all_actions',
            return_value=[
                self.notify_project_team_mock,
                self.block_creation_of_new_resources_mock,
            ],
        ):
            self.estimate.total = self.policy.limit_cost + 1
            self.estimate.save()
            self.notify_project_team_mock.assert_called_once()
            self.block_creation_of_new_resources_mock.assert_not_called()
            self.notify_project_team_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

            self.estimate.total = self.policy.limit_cost + 2
            self.estimate.save()
            self.notify_project_team_mock.assert_not_called()
            self.block_creation_of_new_resources_mock.assert_not_called()
            self.notify_project_team_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

            self.estimate.total = self.policy.limit_cost - 1
            self.estimate.save()
            self.notify_project_team_mock.assert_not_called()
            self.block_creation_of_new_resources_mock.assert_not_called()
            self.notify_project_team_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

            self.estimate.total = self.policy.limit_cost + 1
            self.estimate.save()
            self.notify_project_team_mock.assert_called_once()
            self.block_creation_of_new_resources_mock.assert_not_called()
            self.notify_project_team_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

    def test_calling_of_not_one_time_actions(self):
        with mock.patch.object(
            ProjectEstimatedCostPolicy,
            'get_all_actions',
            return_value=[
                self.notify_project_team_mock,
                self.block_creation_of_new_resources_mock,
            ],
        ):
            self.estimate.total = self.policy.limit_cost + 1
            self.estimate.save()

            self.notify_project_team_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

            order = marketplace_factories.OrderFactory(project=self.project)
            order_item = marketplace_factories.OrderItemFactory(
                order=order,
                offering=self.fixture.offering,
                attributes={'name': 'item_name', 'description': 'Description'},
                plan=self.fixture.plan,
            )
            marketplace_utils.process_order_item(order_item, self.fixture.staff)

            self.notify_project_team_mock.assert_not_called()
            self.block_creation_of_new_resources_mock.assert_called_once()

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


@ddt
class GetPolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.policy = factories.ProjectEstimatedCostPolicyFactory(project=self.project)
        self.url = factories.ProjectEstimatedCostPolicyFactory.get_list_url()

    @data('staff', 'owner', 'customer_support')
    def test_user_can_get_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    @data('user', 'offering_owner', 'admin', 'manager')
    def test_user_can_not_get_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


@ddt
class CreatePolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.url = factories.ProjectEstimatedCostPolicyFactory.get_list_url()

    def _create_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {
            'limit_cost': 100,
            'actions': 'notify_organization_owners,block_modification_of_existing_resources',
            'project': structure_factories.ProjectFactory.get_url(self.project),
        }
        return self.client.post(self.url, payload)

    @data('staff', 'owner')
    def test_user_can_create_policy(self, user):
        response = self._create_policy(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data('admin', 'manager', 'user', 'offering_owner')
    def test_user_can_not_create_policy(self, user):
        response = self._create_policy(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validate_actions(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            'limit_cost': 100,
            'actions': 'notify_organization_owners,non_existent_method',
            'project': structure_factories.ProjectFactory.get_url(self.project),
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_some_policies_for_one_project(self):
        response = self._create_policy('staff')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self._create_policy('staff')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


@ddt
class DeletePolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.policy = factories.ProjectEstimatedCostPolicyFactory(project=self.project)
        self.url = factories.ProjectEstimatedCostPolicyFactory.get_url(self.policy)

    def _delete_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.delete(self.url)

    @data('staff', 'owner', 'customer_support')
    def test_user_can_delete_policy(self, user):
        response = self._delete_policy(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data('admin', 'manager', 'user', 'offering_owner')
    def test_user_can_not_delete_policy(self, user):
        response = self._delete_policy(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class UpdatePolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.policy = factories.ProjectEstimatedCostPolicyFactory(project=self.project)
        self.url = factories.ProjectEstimatedCostPolicyFactory.get_url(self.policy)

    def _update_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.patch(self.url, {'actions': 'notify_organization_owners'})

    @data('staff', 'owner', 'customer_support')
    def test_user_can_update_policy(self, user):
        response = self._update_policy(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('admin', 'manager', 'user', 'offering_owner')
    def test_user_can_not_update_policy(self, user):
        response = self._update_policy(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
