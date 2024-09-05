from unittest import mock

from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.models import ProjectEstimatedCostPolicy
from waldur_mastermind.policy.tasks import check_polices
from waldur_mastermind.policy.tests import factories


@freeze_time("2024-09-01")
class ActionsFunctionsTest(test.APITransactionTestCase):
    def setUp(self):
        self.notify_project_team_mock = mock.MagicMock()
        self.notify_project_team_mock.one_time_action = True
        self.notify_project_team_mock.__name__ = "notify_project_team"

        self.block_creation_of_new_resources_mock = mock.MagicMock()
        self.block_creation_of_new_resources_mock.one_time_action = False
        self.block_creation_of_new_resources_mock.__name__ = (
            "block_creation_of_new_resources"
        )

        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.policy = factories.ProjectEstimatedCostPolicyFactory(scope=self.project)
        self.invoice = invoices_factories.InvoiceFactory(
            customer=self.fixture.customer,
            month=9,
            year=2024,
            tax_percent=0,
        )

    def tearDown(self):
        mock.patch.stopall()

    def create_or_update_invoice_item(self, unit_price):
        if self.invoice.items.first():
            invoice_item = self.invoice.items.first()
            invoice_item.unit_price = unit_price
            invoice_item.save()
        else:
            invoice_item = invoices_factories.InvoiceItemFactory(
                invoice=self.invoice,
                project=self.project,
                quantity=1,
                unit_price=unit_price,
            )
        return invoice_item

    def test_calling_of_one_time_actions(self):
        with mock.patch.object(
            ProjectEstimatedCostPolicy,
            "get_all_actions",
            return_value=[
                self.notify_project_team_mock,
                self.block_creation_of_new_resources_mock,
            ],
        ):
            self.create_or_update_invoice_item(self.policy.limit_cost + 1)
            self.notify_project_team_mock.assert_called_once()
            self.block_creation_of_new_resources_mock.assert_not_called()
            self.notify_project_team_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

            self.create_or_update_invoice_item(self.policy.limit_cost + 2)
            self.notify_project_team_mock.assert_not_called()
            self.block_creation_of_new_resources_mock.assert_not_called()
            self.notify_project_team_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

            self.create_or_update_invoice_item(self.policy.limit_cost - 1)
            self.notify_project_team_mock.assert_not_called()
            self.block_creation_of_new_resources_mock.assert_not_called()
            self.notify_project_team_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

            self.create_or_update_invoice_item(self.policy.limit_cost + 1)
            self.notify_project_team_mock.assert_called_once()
            self.block_creation_of_new_resources_mock.assert_not_called()
            self.notify_project_team_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

    def test_calling_of_not_one_time_actions(self):
        with mock.patch.object(
            ProjectEstimatedCostPolicy,
            "get_all_actions",
            return_value=[
                self.notify_project_team_mock,
                self.block_creation_of_new_resources_mock,
            ],
        ):
            self.create_or_update_invoice_item(self.policy.limit_cost + 1)

            self.notify_project_team_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

            order = marketplace_factories.OrderFactory(
                project=self.project,
                offering=self.fixture.offering,
                attributes={"name": "item_name", "description": "Description"},
                plan=self.fixture.plan,
                state=marketplace_models.Order.States.EXECUTING,
            )
            marketplace_utils.process_order(order, self.fixture.staff)

            self.notify_project_team_mock.assert_not_called()
            self.block_creation_of_new_resources_mock.assert_called()

    def test_has_fired(self):
        self.create_or_update_invoice_item(self.policy.limit_cost + 1)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)

        self.create_or_update_invoice_item(self.policy.limit_cost - 1)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, False)
        self.assertTrue(self.policy.fired_datetime)

    def test_several_policies(self):
        with mock.patch.object(
            ProjectEstimatedCostPolicy,
            "get_all_actions",
            return_value=[
                self.notify_project_team_mock,
                self.block_creation_of_new_resources_mock,
            ],
        ):
            policy_2 = factories.ProjectEstimatedCostPolicyFactory(scope=self.project)
            self.create_or_update_invoice_item(self.policy.limit_cost + 1)
            self.policy.refresh_from_db()
            policy_2.refresh_from_db()
            self.assertEqual(self.policy.has_fired, True)
            self.assertEqual(policy_2.has_fired, True)

    def test_policy_period(self):
        # period = 1 month
        invoice_item = self.create_or_update_invoice_item(self.policy.limit_cost + 1)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)

        invoice_item.invoice.month = 7
        invoice_item.invoice.save()
        invoice_item.save()  # for running of a handler
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, False)

        url = factories.ProjectEstimatedCostPolicyFactory.get_url(self.policy)
        self.client.force_authenticate(self.fixture.staff)

        # period = 3 month
        self.client.patch(url, {"period": ProjectEstimatedCostPolicy.Periods.MONTH_3})
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)

        invoice_item.invoice.month = 10
        invoice_item.invoice.year = 2023
        invoice_item.invoice.save()
        invoice_item.save()
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, False)

        # period = 12 month
        self.client.patch(url, {"period": ProjectEstimatedCostPolicy.Periods.MONTH_12})
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)

        invoice_item.invoice.month = 9
        invoice_item.invoice.year = 2023
        invoice_item.invoice.save()
        invoice_item.save()
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, False)

        # period = Total
        self.client.patch(url, {"period": ProjectEstimatedCostPolicy.Periods.TOTAL})
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)

    def test_check_polices_task(self):
        self.create_or_update_invoice_item(self.policy.limit_cost + 1)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)

        with freeze_time("2024-10-01"):
            check_polices()
            self.policy.refresh_from_db()
            self.assertEqual(self.policy.has_fired, False)


@ddt
class GetPolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.policy = factories.ProjectEstimatedCostPolicyFactory(scope=self.project)
        self.url = factories.ProjectEstimatedCostPolicyFactory.get_list_url()

    @data("staff", "owner", "customer_support", "admin", "manager")
    def test_user_can_get_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    @data("user", "offering_owner")
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
            "limit_cost": 100,
            "actions": "notify_organization_owners,block_modification_of_existing_resources",
            "scope": structure_factories.ProjectFactory.get_url(self.project),
        }
        return self.client.post(self.url, payload)

    @data("staff", "owner")
    def test_user_can_create_policy(self, user):
        response = self._create_policy(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        policy = ProjectEstimatedCostPolicy.objects.get(uuid=response.data["uuid"])
        self.assertEqual(policy.has_fired, False)

    @data("admin", "manager", "user", "offering_owner")
    def test_user_can_not_create_policy(self, user):
        response = self._create_policy(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validate_actions(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "limit_cost": 100,
            "actions": "notify_organization_owners,non_existent_method",
            "project": structure_factories.ProjectFactory.get_url(self.project),
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_some_policies_for_one_project(self):
        response = self._create_policy("staff")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self._create_policy("staff")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @freeze_time("2024-09-01")
    def test_policies_should_be_triggered_after_creation_if_cost_limit_has_been_reached(
        self,
    ):
        notify_project_team_mock = mock.MagicMock()
        notify_project_team_mock.one_time_action = True
        notify_project_team_mock.__name__ = "notify_project_team"

        block_creation_of_new_resources_mock = mock.MagicMock()
        block_creation_of_new_resources_mock.one_time_action = False
        block_creation_of_new_resources_mock.__name__ = (
            "block_creation_of_new_resources"
        )

        invoice = invoices_factories.InvoiceFactory(
            customer=self.fixture.customer,
            month=9,
            year=2024,
            tax_percent=0,
        )

        with mock.patch.object(
            ProjectEstimatedCostPolicy,
            "get_all_actions",
            return_value=[
                notify_project_team_mock,
                block_creation_of_new_resources_mock,
            ],
        ):
            invoices_factories.InvoiceItemFactory(
                invoice=invoice, project=self.project, quantity=1, unit_price=1000
            )
            response = self._create_policy("staff")
            policy = ProjectEstimatedCostPolicy.objects.get(uuid=response.data["uuid"])
            notify_project_team_mock.assert_called_once()
            block_creation_of_new_resources_mock.assert_not_called()
            self.assertEqual(policy.has_fired, True)


@ddt
class DeletePolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.policy = factories.ProjectEstimatedCostPolicyFactory(scope=self.project)
        self.url = factories.ProjectEstimatedCostPolicyFactory.get_url(self.policy)

    def _delete_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.delete(self.url)

    @data("staff", "owner")
    def test_user_can_delete_policy(self, user):
        response = self._delete_policy(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data("user", "offering_owner")
    def test_user_can_not_delete_policy(self, user):
        response = self._delete_policy(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("admin", "manager")
    def test_project_member_can_not_delete_policy(self, user):
        response = self._delete_policy(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class UpdatePolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.policy = factories.ProjectEstimatedCostPolicyFactory(scope=self.project)
        self.url = factories.ProjectEstimatedCostPolicyFactory.get_url(self.policy)

    def _update_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.patch(self.url, {"actions": "notify_organization_owners"})

    @data("staff", "owner")
    def test_user_can_update_policy(self, user):
        response = self._update_policy(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("user", "offering_owner")
    def test_user_can_not_update_policy(self, user):
        response = self._update_policy(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("admin", "manager")
    def test_project_member_can_not_update_policy(self, user):
        response = self._update_policy(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
