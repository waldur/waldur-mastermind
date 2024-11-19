from unittest import mock

from rest_framework import test

from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.tests import factories


class CreditHasBeenChangedTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer_policy = factories.CustomerEstimatedCostPolicyFactory(
            scope=self.fixture.customer
        )

    def tearDown(self):
        mock.patch.stopall()

    @mock.patch("waldur_mastermind.policy.handlers.utils.evaluate_policies")
    def test_customer_credit_has_been_changed(self, mock_evaluate_policies):
        self.customer_credit = invoices_factories.CustomerCreditFactory(
            customer=self.fixture.customer
        )
        mock_evaluate_policies.assert_called_once()
        self.assertEqual(
            list(mock_evaluate_policies.call_args_list[0][0][0]), [self.customer_policy]
        )

        mock_evaluate_policies.reset_mock()

        self.customer_credit.offerings.add(marketplace_factories.OfferingFactory())
        mock_evaluate_policies.assert_called_once()
        self.assertEqual(
            list(mock_evaluate_policies.call_args_list[0][0][0]), [self.customer_policy]
        )

    @mock.patch("waldur_mastermind.policy.handlers.utils.evaluate_policies")
    def test_project_credit_has_been_changed(self, mock_evaluate_policies):
        invoices_factories.CustomerCreditFactory(customer=self.fixture.customer)
        self.project_policy = factories.ProjectEstimatedCostPolicyFactory(
            scope=self.fixture.project
        )
        mock_evaluate_policies.reset_mock()
        self.project_credit = invoices_factories.ProjectCreditFactory(
            project=self.fixture.project
        )
        mock_evaluate_policies.assert_called_once()
        self.assertEqual(
            list(mock_evaluate_policies.call_args_list[0][0][0]), [self.project_policy]
        )
