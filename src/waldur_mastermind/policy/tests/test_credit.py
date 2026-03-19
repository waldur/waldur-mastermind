from unittest import mock

from django.test import override_settings
from rest_framework import test

from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.tests import factories


@override_settings(task_always_eager=True)
class CreditHasBeenChangedTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer_policy = factories.CustomerEstimatedCostPolicyFactory(
            scope=self.fixture.customer
        )

    def tearDown(self):
        mock.patch.stopall()

    @mock.patch("waldur_mastermind.policy.utils.evaluate_policies")
    def test_customer_credit_has_been_changed(self, mock_evaluate_policies):
        self.customer_credit = invoices_factories.CustomerCreditFactory(
            customer=self.fixture.customer
        )
        # The handler now schedules two Celery tasks (customer + project policies).
        # With task_always_eager=True they execute synchronously and each calls
        # utils.evaluate_policies if matching policies exist.
        self.assertTrue(mock_evaluate_policies.called)
        # First call should be for customer policies
        first_call_policies = list(mock_evaluate_policies.call_args_list[0][0][0])
        self.assertEqual(first_call_policies, [self.customer_policy])

        mock_evaluate_policies.reset_mock()

        self.customer_credit.offerings.add(marketplace_factories.OfferingFactory())
        self.assertTrue(mock_evaluate_policies.called)
        first_call_policies = list(mock_evaluate_policies.call_args_list[0][0][0])
        self.assertEqual(first_call_policies, [self.customer_policy])

    @mock.patch("waldur_mastermind.policy.utils.evaluate_policies")
    def test_project_credit_has_been_changed(self, mock_evaluate_policies):
        invoices_factories.CustomerCreditFactory(customer=self.fixture.customer)
        self.project_policy = factories.ProjectEstimatedCostPolicyFactory(
            scope=self.fixture.project
        )
        mock_evaluate_policies.reset_mock()
        self.project_credit = invoices_factories.ProjectCreditFactory(
            project=self.fixture.project
        )
        self.assertTrue(mock_evaluate_policies.called)
        first_call_policies = list(mock_evaluate_policies.call_args_list[0][0][0])
        self.assertEqual(first_call_policies, [self.project_policy])
